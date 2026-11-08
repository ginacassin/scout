"""Morning briefing for days with markets you're going to, plus a daily Eventbrite login check.

Briefing (08:00 by default, BRIEFING_TIME=HH:MM): every market that day you booked a slot for or
answered ✅, in time order, each with its Instagram account, what's on sale, hours, address and
Maps link, your notes and the drive to the next stop, then one Google Maps route for the day.
Nothing is sent on days without markets. /today and /briefing [date] show it on demand.

It replaces the 08:30 notes reminder for the markets it covers; the "1 h before your slot"
reminder still arrives.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from html import escape
from urllib.parse import quote, urlencode

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from .bot import templates as T
from .db import Account, Database, Event

log = logging.getLogger(__name__)

SENT_KEY = "briefing_sent"
LOGIN_WARNED_KEY = "eventbrite_logout_warned"


def _hm(value: str | None) -> time | None:
    try:
        h, m = (value or "").split(":")[:2]
        return time(int(h), int(m))
    except ValueError:
        return None


def _briefing_time() -> time:
    return _hm(os.environ.get("BRIEFING_TIME", "08:00")) or time(8, 0)


def sessions_on(ev: Event, day: date) -> list[dict]:
    return [s for s in ev.session_list if s.get("date") == day.isoformat()]


def stop_time(ev: Event, day: date, tz) -> datetime | None:  # noqa: ANN001
    """When you'd be there: the booked slot, else the first free-entry session, else the first session."""
    if ev.booked_start and ev.booked_start.astimezone(tz).date() == day:
        return ev.booked_start.astimezone(tz)
    sess = sessions_on(ev, day)
    free = [s for s in sess if not s.get("booking_required") and _hm(s.get("start"))]
    pick = free or [s for s in sess if _hm(s.get("start"))]
    if pick:
        return datetime.combine(day, min(_hm(s["start"]) for s in pick), tzinfo=tz)
    return None


def maps_route_url(stops: list[str]) -> str | None:
    stops = [s for s in stops if s]
    if not stops:
        return None
    params = {"api": "1", "travelmode": "driving", "destination": stops[-1]}
    if len(stops) > 1:
        params["waypoints"] = "|".join(stops[:-1])
    return "https://www.google.com/maps/dir/?" + urlencode(params, quote_via=quote)


class Briefing:
    def __init__(self, app: Application, settings, db: Database):  # noqa: ANN001
        self.app = app
        self.settings = settings
        self.db = db
        self.tz = settings.tz

    # ---------------------------------------------------------------- selection
    def markets_on(self, day: date) -> list[tuple[Event, str]]:
        with self.db.session() as s:
            handles = {a.id: a.handle for a in s.scalars(select(Account))}
            evs = list(s.scalars(select(Event).where(Event.user_decision.in_(("yes",)))))
        out = []
        for ev in evs:
            booked_today = ev.booked_start and ev.booked_start.astimezone(self.tz).date() == day
            if booked_today or (day in ev.date_list and not ev.booked_start):
                out.append((ev, handles.get(ev.account_id, "?")))
        far = datetime.max.replace(tzinfo=self.tz)
        out.sort(key=lambda r: (stop_time(r[0], day, self.tz) or far, r[0].id))
        return out

    # ------------------------------------------------------------------- text
    async def text(self, day: date) -> tuple[str, InlineKeyboardMarkup | None] | None:
        markets = self.markets_on(day)
        if not markets:
            return None
        planner = self.app.bot_data.get("planner")
        label = "Today" if day == datetime.now(self.tz).date() else day.strftime("%a %-d %b")
        n = len(markets)
        lines = [f"☀️ <b>{label}: {n} market{'s' * (n > 1)}</b>"]
        prev_pt = prev_ev = None
        for i, (ev, handle) in enumerate(markets, 1):
            when = stop_time(ev, day, self.tz)
            pt = None
            if planner is not None:
                try:
                    pt = await planner._point(ev)
                    if prev_ev is not None:
                        leg = await planner.geo.leg(prev_pt, pt)
                        km = f", {leg.km:g} km" if leg.km else ""
                        lines.append(f"   🚗 about {leg.minutes}′ drive{km}"
                                     + (" (estimate)" if leg.guessed else ""))
                except Exception:  # noqa: BLE001 - routing is a nice-to-have
                    log.warning("briefing: routing failed for event %s", ev.id, exc_info=True)
            lines.append("")
            head = when.strftime("%H:%M") if when else "Any time"
            lines.append(f"<b>{i}. {head} · {escape(ev.title)}</b>")
            lines.append(f'   <a href="https://www.instagram.com/{escape(handle)}/">@{escape(handle)}</a>'
                         + (f' · <a href="{escape(ev.source_url)}">post</a>' if ev.source_url else ""))
            if ev.booked_start and ev.booked_start.astimezone(self.tz).date() == day:
                end = f"–{ev.booked_end.astimezone(self.tz):%H:%M}" if ev.booked_end else ""
                lines.append(f"   🎟 Your slot {ev.booked_start.astimezone(self.tz):%H:%M}{end}"
                             + (" · ticket pinned in this chat" if ev.ticket_path else ""))
            hours = [f"{s.get('start') or '?'}–{s.get('end') or '?'}" + (" 🎟" if s.get("booking_required") else "")
                     for s in sorted(sessions_on(ev, day), key=lambda s: s.get("start") or "")]
            if hours:
                lines.append("   🕘 Open " + ", ".join(hours))
            where = ev.address or ev.area
            if where:
                lines.append(f'   📍 <a href="https://www.google.com/maps/search/?api=1&amp;query={quote(where)}">'
                             f"{escape(where)}</a>")
            if ev.summary:
                lines.append(f"   🪑 {escape(ev.summary)}")
            if ev.price and ev.price.lower() not in ("free", "gratis"):
                lines.append(f"   💶 {escape(ev.price)}")
            for note in ev.note_list:
                lines.append(f"   📝 {escape(note.get('text', ''))}")
            prev_pt, prev_ev = pt, ev
        route = maps_route_url([ev.address or ev.area or "" for ev, _ in markets])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗺 Route for the day", url=route)]]) if route else None
        text = "\n".join(lines)
        return (text[:4000] + "…" if len(text) > 4000 else text), kb

    # ------------------------------------------------------------------ sending
    def _suppress_note_reminders(self, day: date, markets: list[tuple[Event, str]]) -> None:
        cmds = self.app.bot_data.get("booking_cmds")
        if cmds is None:
            return
        for ev, _ in markets:
            if not ev.booked_start:  # keep "1 h before your slot" reminders
                cmds._mark_sent(f"{ev.id}:{day:%Y-%m-%d}")
        try:
            cmds.schedule_reminders()
        except Exception:  # noqa: BLE001
            log.warning("briefing: couldn't reschedule reminders", exc_info=True)

    async def send(self, day: date | None = None, force: bool = False) -> bool:
        day = day or datetime.now(self.tz).date()
        if not force and self.db.get_setting(SENT_KEY) == day.isoformat():
            return False
        built = await self.text(day)
        if built is None:
            return False
        text, kb = built
        try:
            await self.app.bot.send_message(self.settings.allowed_chat_id, text, parse_mode=ParseMode.HTML,
                                            reply_markup=kb, disable_web_page_preview=True)
        except TelegramError:
            log.exception("Couldn't send the briefing for %s", day)
            return False
        if not force:
            self.db.set_setting(SENT_KEY, day.isoformat())
            self._suppress_note_reminders(day, self.markets_on(day))
        log.info("briefing sent day=%s markets=%d", day, len(self.markets_on(day)))
        return True

    async def daily_job(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        await self.send()

    async def catch_up(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """After a restart between the briefing time and noon, send today's if it was missed."""
        now = datetime.now(self.tz)
        if _briefing_time() <= now.time() < time(12, 0):
            await self.send()

    # ----------------------------------------------------------------- commands
    async def cmd_today(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, datetime.now(self.tz).date())

    async def cmd_briefing(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        day = datetime.now(self.tz).date()
        if ctx.args:
            arg = ctx.args[0].lower()
            if arg in ("tomorrow", "mañana", "manana"):
                day += timedelta(days=1)
            else:
                from .planner import parse_day
                day = parse_day(" ".join(ctx.args), day) or day
        await self._reply(update, day)

    async def _reply(self, update: Update, day: date) -> None:
        built = await self.text(day)
        if built is None:
            await update.effective_message.reply_text(
                f"No markets on {day:%a %-d %b} yet — mark one ✅ or book a slot and it shows up here.")
            return
        text, kb = built
        await update.effective_message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)

    # ------------------------------------------------------ eventbrite login check
    async def login_check(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """Once a day: if the auto-booker's Eventbrite session has expired, say so once (until fixed)."""
        ab = self.app.bot_data.get("autobook")
        if ab is None or getattr(ab, "client", None) is None or not getattr(ab, "enabled", False):
            return
        try:
            status, data = await ab.client.check_login()
        except Exception:  # noqa: BLE001
            log.warning("Eventbrite login check failed", exc_info=True)
            return
        logged_in = data.get("logged_in") if status == 200 else None
        warned = self.db.get_setting(LOGIN_WARNED_KEY) == "1"
        if logged_in is False and not warned:
            self.db.set_setting(LOGIN_WARNED_KEY, "1")
            await self.app.bot.send_message(
                self.settings.allowed_chat_id,
                "⚠️ The auto-booker is logged out of Eventbrite, so it can't book for you.\n"
                "Fix: /autobook login, log in at the link on your home Wi-Fi, then /autobook done.")
        elif logged_in is True and warned:
            self.db.set_setting(LOGIN_WARNED_KEY, "0")
        log.info("Eventbrite login check: %s", logged_in)


def setup_briefing(app: Application, settings, db: Database) -> Briefing:  # noqa: ANN001
    b = Briefing(app, settings, db)
    app.bot_data["briefing"] = b
    only_me = filters.Chat(chat_id=settings.allowed_chat_id)
    app.add_handler(CommandHandler("today", b.cmd_today, filters=only_me))
    app.add_handler(CommandHandler("briefing", b.cmd_briefing, filters=only_me))
    if app.job_queue is not None:
        app.job_queue.run_daily(b.daily_job, _briefing_time().replace(tzinfo=settings.tz), name="briefing")
        app.job_queue.run_once(b.catch_up, when=15, name="briefing-catch-up")
        app.job_queue.run_daily(b.login_check, time(12, 17, tzinfo=settings.tz), name="eventbrite-login-check")
    return b

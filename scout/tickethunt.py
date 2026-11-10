"""Ticket hunt: on request, watch an Eventbrite market for free tickets that come back.

Two cases, same switch (/hunt <id>, or "🔎 Watch for free tickets" on the card):
- no ticket yet → tell me when any free slot is available;
- already booked → tell me when a slot *earlier* than mine is free (the first slots are the best).
An optional cut-off narrows it further: `/hunt 18 before 11:00`.

It only runs when you ask, checks every TICKET_HUNT_MINUTES (default 5 with an Eventbrite token,
10 from the public page), alerts only when a matching slot *becomes* free (not every check), and stops
at 09:00 on the market's first day. Alerts between 23:00 and 08:00 arrive silently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta
from html import escape
from typing import Any

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from . import hooks
from .db import BookingWatch, Database, Event, utcnow
from .eventbrite.client import EventbriteClient
from .eventbrite.slots import SlotInfo

log = logging.getLogger(__name__)

STOP_AT = time(9, 0)  # on the market's first day: too late to change plans after that
MAX_ALERTS_PER_DAY = 10  # per market, against a flapping page
QUIET = (time(23, 0), time(8, 0))
HHMM_RE = re.compile(r"^(\d{1,2})[:.h]?(\d{2})?$")


def _hhmm(text: str) -> str | None:
    m = HHMM_RE.match(text.strip().lower())
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2) or 0)
    return f"{h:02d}:{mm:02d}" if h < 24 and mm < 60 else None


class TicketHunt:
    def __init__(self, app: Application, settings, db: Database, client: EventbriteClient | None = None,  # noqa: ANN001
                 now=None):  # noqa: ANN001
        self.app = app
        self.settings = settings
        self.db = db
        self.tz = settings.tz
        self.chat_id = settings.allowed_chat_id
        watcher = app.bot_data.get("booking") if app is not None else None
        self.watcher = watcher
        self.client = client or (watcher.client if watcher else EventbriteClient(getattr(settings, "eventbrite_token", "")))
        self.now = now or utcnow
        default = 5 if self.client.token else 10
        self.interval = timedelta(minutes=max(2, int(os.environ.get("TICKET_HUNT_MINUTES", default))))
        self._busy = asyncio.Lock()

    # ============================================================ registration
    def register(self) -> None:
        from .bot import templates as T

        hooks.on("decision_changed", self.on_decision_changed)
        hooks.on("booking_confirmed", self.on_booking_confirmed)
        T.EXTRA_ANSWERED_BUTTONS.append(self.card_buttons)
        only_me = filters.Chat(chat_id=self.chat_id)
        self.app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^th:"))
        self.app.add_handler(CommandHandler("hunt", self.cmd_hunt, filters=only_me))
        if self.app.job_queue is not None:
            self.app.job_queue.run_repeating(self.tick, interval=60, first=45, name="ticket-hunt")

    # ================================================================ helpers
    def _load(self, event_id: int) -> tuple[Event | None, BookingWatch | None]:
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))
            return ev, w

    def _update(self, event_id: int, **fields: Any) -> None:
        with self.db.session() as s:
            w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))
            if w is not None:
                for k, v in fields.items():
                    setattr(w, k, v)

    def deadline(self, ev: Event) -> datetime | None:
        days = ev.date_list
        if not days and ev.booked_start:
            days = [ev.booked_start.astimezone(self.tz).date()]
        if not days:
            return None
        return datetime.combine(min(days), STOP_AT, tzinfo=self.tz)

    def _too_late(self, ev: Event) -> bool:
        dl = self.deadline(ev)
        return dl is not None and self.now() >= dl

    def _fmt(self, dt: datetime) -> str:
        return dt.astimezone(self.tz).strftime("%a %-d %b %H:%M")

    def _target(self, ev: Event, w: BookingWatch) -> str:
        parts = []
        if ev.booked_start:
            parts.append(f"earlier than your {ev.booked_start.astimezone(self.tz):%H:%M} slot")
        if w.hunt_before:
            parts.append(f"starting before {w.hunt_before}")
        return " and ".join(parts) if parts else "any slot"

    def _url(self, ev: Event, w: BookingWatch) -> str | None:
        if self.watcher is not None:
            return self.watcher._url_for(ev, w)
        return w.event_url or (f"https://www.eventbrite.es/e/{w.eventbrite_id}" if w.eventbrite_id else None)

    def matching(self, ev: Event, w: BookingWatch, slots: list[SlotInfo]) -> list[SlotInfo]:
        now = self.now()
        out = []
        for s in slots:
            if not s.available or s.free is False:
                continue
            if s.start and s.exact and s.start <= now:
                continue
            if s.exact and s.start:
                if ev.booked_start and s.start >= ev.booked_start:
                    continue
                if w.hunt_before and s.start.astimezone(self.tz).strftime("%H:%M") >= w.hunt_before:
                    continue
            out.append(s)
        return out

    def _alerts_today(self, w: BookingWatch) -> list[str]:
        today = self.now().astimezone(self.tz).date().isoformat()
        return [a for a in json.loads(w.hunt_alerts or "[]") if a[:10] == today]

    # ================================================================== checks
    async def check(self, event_id: int) -> tuple[str, list[SlotInfo], list[SlotInfo]] | None:
        """One look at Eventbrite. Alerts on newly free matching slots. Returns (status, matching, all)."""
        ev, w = self._load(event_id)
        if ev is None or w is None or not w.hunt_on:
            return None
        if self._too_late(ev):
            await self.stop(event_id, reason="deadline")
            return None
        if ev.user_decision == "no":
            await self.stop(event_id, reason="no")
            return None
        if not w.eventbrite_id:
            self._update(event_id, hunt_checked_at=self.now(), hunt_status="waiting for the Eventbrite link")
            return "waiting for the Eventbrite link", [], []
        slots = await self.client.slot_states(w.eventbrite_id, w.event_url)
        if slots is None:
            self._update(event_id, hunt_checked_at=self.now(), hunt_status="couldn't read Eventbrite last time")
            return "couldn't read Eventbrite just now", [], []
        found = self.matching(ev, w, slots)
        keys = sorted({s.key for s in found})
        before = set(json.loads(w.hunt_avail or "[]"))
        new = [s for s in found if s.key not in before]
        exact = [s for s in slots if s.exact]
        if found:
            status = f"{len(found)} matching slot{'s' * (len(found) > 1)} free"
        elif exact:
            free = sum(1 for s in exact if s.available)
            status = f"nothing fits ({free} of {len(exact)} slots free)" if free else f"all {len(exact)} slots taken"
        else:
            status = "sold out" if not any(s.available for s in slots) else "nothing fits"
        self._update(event_id, hunt_avail=json.dumps(keys), hunt_checked_at=self.now(), hunt_status=status[:200])
        log.info("ticket hunt event=%s slots=%d matching=%d new=%d", event_id, len(slots), len(found), len(new))
        if new:
            await self._alert(ev, w, new, found)
        return status, found, slots

    async def _alert(self, ev: Event, w: BookingWatch, new: list[SlotInfo], found: list[SlotInfo]) -> None:
        sent = self._alerts_today(w)
        if len(sent) >= MAX_ALERTS_PER_DAY:
            log.info("ticket hunt event=%s: daily alert cap reached", ev.id)
            return
        lines = ["🎟 <b>Free ticket available</b>", f"🪑 <b>{escape(ev.title)}</b>"]
        exact = [s for s in found if s.exact and s.start]
        if exact:
            head = (f"Earlier than your {ev.booked_start.astimezone(self.tz):%H:%M} slot:" if ev.booked_start
                    else "Free right now:")
            lines.append(head)
            fresh = {s.key for s in new}
            for s in exact[:8]:
                lines.append(f"• {escape(s.short(self.tz))}" + (" 🆕" if s.key in fresh and len(exact) > 1 else ""))
            if len(exact) > 8:
                lines.append(f"• …and {len(exact) - 8} more")
        else:
            lines.append("Eventbrite shows tickets available again"
                         + (f" — it doesn't say which times, so check for one earlier than your "
                            f"{ev.booked_start.astimezone(self.tz):%H:%M}." if ev.booked_start else "."))
        lines.append("")
        if ev.booked_start:
            lines.append(f"Switched? Cancel your old ticket on Eventbrite, then <code>/booked {ev.id} HH:MM</code>.")
        else:
            lines.append(f"Got one? Tell me with <code>/booked {ev.id} HH:MM</code>.")
        dl = self.deadline(ev)
        if dl:
            lines.append(f"<i>Watching until {self._fmt(dl)}.</i>")
        rows = []
        url = self._url(ev, w)
        if url:
            rows.append([InlineKeyboardButton("🎟 Open Eventbrite", url=url)])
        if w.eventbrite_id:
            rows.append([InlineKeyboardButton("⚡ Straight to checkout",
                                              url=f"https://www.eventbrite.es/checkout-external?eid={w.eventbrite_id}")])
        rows.append([InlineKeyboardButton("🛑 Stop watching", callback_data=f"th:off:{ev.id}")])
        local = self.now().astimezone(self.tz).time()
        quiet = local >= QUIET[0] or local < QUIET[1]
        try:
            await self.app.bot.send_message(self.chat_id, "\n".join(lines), parse_mode=ParseMode.HTML,
                                            reply_markup=InlineKeyboardMarkup(rows), disable_web_page_preview=True,
                                            disable_notification=quiet)
        except TelegramError:
            log.exception("Ticket hunt alert for event %s failed", ev.id)
            return
        alerts = json.loads(w.hunt_alerts or "[]")[-50:] + [self.now().astimezone(self.tz).isoformat()]
        self._update(ev.id, hunt_alerts=json.dumps(alerts))

    async def tick(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        if self._busy.locked():
            return
        async with self._busy:
            with self.db.session() as s:
                rows = list(s.scalars(select(BookingWatch).where(BookingWatch.hunt_on.is_(True))))
            now = self.now()
            for w in rows:
                ev, _ = self._load(w.event_id)
                if ev is not None and self._too_late(ev):  # stop on time, not at the next check
                    await self.stop(w.event_id, reason="deadline")
                    continue
                if w.hunt_checked_at and now - w.hunt_checked_at < self.interval - timedelta(seconds=20):
                    continue
                try:
                    await self.check(w.event_id)
                except Exception:  # noqa: BLE001
                    log.exception("Ticket hunt check for event %s failed", w.event_id)
                await asyncio.sleep(1)

    # =========================================================== start / stop
    async def start(self, event_id: int, before: str | None = None) -> str:
        ev, w = self._load(event_id)
        if ev is None:
            return f"I don't know market #{event_id}. /upcoming shows the numbers."
        if w is None or not w.eventbrite_id:
            return f"I haven't found an Eventbrite page for <b>{escape(ev.title)}</b>, so there's nothing to watch."
        dl = self.deadline(ev)
        if dl and self.now() >= dl:
            return f"Too late for <b>{escape(ev.title)}</b>: I stop watching at {STOP_AT:%H:%M} on its first day."
        self._update(event_id, hunt_on=True, hunt_before=before, hunt_avail="[]")
        ev, w = self._load(event_id)
        log.info("ticket hunt on event=%s before=%s booked=%s", event_id, before, ev.booked_start)
        result = await self.check(event_id)
        until = f" until {self._fmt(dl)}" if dl else ""
        lines = [f"🔎 Watching <b>{escape(ev.title)}</b> for free tickets: {self._target(ev, w)}.",
                 f"I check every {int(self.interval.total_seconds() // 60)} min{until}, and only message you "
                 "when a matching slot frees up."]
        if result is None:
            return "\n".join(lines)
        status, found, slots = result
        if found:
            lines.append("Right now: " + status + " — sent above.")
        else:
            lines.append(f"Right now: {status}.")
            starts = [x.start for x in slots if x.exact and x.start]
            if ev.booked_start and starts and ev.booked_start <= min(starts):
                lines.append("You already have the first slot, so nothing earlier can come up. "
                             f"I'll keep watching anyway; <code>/hunt {event_id} off</code> if you don't need it.")
                return "\n".join(lines)
        lines.append(f"Stop with <code>/hunt {event_id} off</code>.")
        return "\n".join(lines)

    async def stop(self, event_id: int, reason: str = "user") -> bool:
        ev, w = self._load(event_id)
        if w is None or not w.hunt_on:
            return False
        self._update(event_id, hunt_on=False, hunt_avail="[]", hunt_status=f"stopped ({reason})")
        log.info("ticket hunt off event=%s reason=%s", event_id, reason)
        if reason == "deadline" and ev is not None:
            try:
                await self.app.bot.send_message(
                    self.chat_id, f"🔎 Stopped watching tickets for <b>{escape(ev.title)}</b> — it starts today.",
                    parse_mode=ParseMode.HTML, disable_notification=True)
            except TelegramError:
                log.exception("Ticket hunt stop message failed")
        if ev is not None:
            await self._redraw(ev)
        return True

    # ================================================================== hooks
    async def on_decision_changed(self, event_id: int, old: str = "", new: str = "", **_: Any) -> None:
        if new == "no":
            await self.stop(event_id, reason="no")

    async def on_booking_confirmed(self, event_id: int, via: str = "", **_: Any) -> None:
        ev, w = self._load(event_id)
        if ev is None or w is None or not w.hunt_on or not ev.booked_start:
            return
        self._update(event_id, hunt_avail="[]")
        try:
            await self.app.bot.send_message(
                self.chat_id,
                f"🔎 Still watching <b>{escape(ev.title)}</b>, now for a slot {self._target(ev, w)}. "
                f"<code>/hunt {event_id} off</code> to stop.",
                parse_mode=ParseMode.HTML, disable_notification=True)
        except TelegramError:
            log.exception("Ticket hunt booking note failed")

    # =============================================================== telegram
    def card_buttons(self, ev: Event) -> list[InlineKeyboardButton]:
        try:
            _, w = self._load(ev.id)
            if w is None or not w.eventbrite_id or ev.user_decision not in ("yes", "maybe") or self._too_late(ev):
                return []
            if w.hunt_on:
                return [InlineKeyboardButton("🔎 Watching for tickets ✓", callback_data=f"th:off:{ev.id}")]
            return [InlineKeyboardButton("🔎 Watch for free tickets", callback_data=f"th:on:{ev.id}")]
        except Exception:  # noqa: BLE001
            log.exception("ticket hunt card_buttons failed")
            return []

    async def _redraw(self, ev: Event) -> None:
        if not ev.telegram_message_id or ev.user_decision == "pending":
            return
        from .bot import templates as T

        try:
            with self.db.session() as s:
                fresh = s.get(Event, ev.id)
            await self.app.bot.edit_message_reply_markup(self.chat_id, fresh.telegram_message_id,
                                                         reply_markup=T.answered_keyboard(fresh))
        except Exception as exc:  # noqa: BLE001
            log.info("Could not redraw card for event %s: %s", ev.id, exc)

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        try:
            if not (update.effective_chat and update.effective_chat.id == self.chat_id):
                await q.answer()
                return
            _, action, eid = (q.data or "").split(":")
            event_id = int(eid)
            if action == "on":
                await q.answer("Watching for tickets…")
                text = await self.start(event_id)
                ev, _ = self._load(event_id)
                if ev:
                    await self._redraw(ev)
                await q.message.reply_html(text, disable_web_page_preview=True)
            elif action == "off":
                stopped = await self.stop(event_id)
                await q.answer("Stopped watching for tickets" if stopped else "Wasn't watching this one")
        except Exception:  # noqa: BLE001
            log.exception("Ticket hunt callback %r failed", getattr(q, "data", None))
            try:
                await q.answer("Something went wrong")
            except Exception:  # noqa: BLE001
                pass

    def list_text(self) -> str:
        with self.db.session() as s:
            rows = [(w, s.get(Event, w.event_id)) for w in s.scalars(select(BookingWatch).where(BookingWatch.hunt_on.is_(True)))]
        lines = []
        for w, ev in rows:
            if ev is None:
                continue
            dl = self.deadline(ev)
            checked = f", checked {w.hunt_checked_at.astimezone(self.tz):%H:%M}" if w.hunt_checked_at else ""
            lines.append(f"• <b>#{ev.id} {escape(ev.title)}</b> — {self._target(ev, w)}"
                         f"\n   {escape(w.hunt_status or 'not checked yet')}{checked}"
                         + (f" · until {self._fmt(dl)}" if dl else ""))
        head = "🔎 <b>Watching for free tickets</b>\n" + "\n".join(lines) if lines else \
            "🔎 Not watching any market for released tickets."
        return head + (
            "\n\n<code>/hunt 18</code> — watch market 18 (any slot, or earlier than the one you booked)"
            "\n<code>/hunt 18 before 11:00</code> — only slots starting before 11:00"
            "\n<code>/hunt 18 off</code> — stop"
            "\nIt stops by itself at 09:00 on the market's first day.")

    async def cmd_hunt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        args = [a.lower() for a in (ctx.args or [])]
        try:
            ids = [int(a.lstrip("#")) for a in args if a.lstrip("#").isdigit() and len(a.lstrip("#")) <= 6]
            if not args or not ids:
                await msg.reply_html(self.list_text(), disable_web_page_preview=True)
                return
            event_id = ids[0]
            if any(a in ("off", "stop", "no") for a in args):
                ev, _ = self._load(event_id)
                ok = await self.stop(event_id)
                name = escape(ev.title) if ev else f"#{event_id}"
                await msg.reply_html(f"🛑 Stopped watching <b>{name}</b>." if ok else f"I wasn't watching <b>{name}</b>.")
                return
            rest = [a for a in args if a not in (str(event_id), f"#{event_id}", "before", "antes", "until")]
            before = None
            if rest:
                before = _hhmm(rest[0])
                if before is None:
                    await msg.reply_html("I didn't get the time. Try <code>/hunt 18 before 11:00</code>.")
                    return
            await msg.reply_html(await self.start(event_id, before), disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.exception("/hunt failed")
            await msg.reply_text("Something went wrong; check /logs.")


def setup_ticket_hunt(app: Application, settings, db: Database) -> TicketHunt:  # noqa: ANN001
    hunt = TicketHunt(app, settings, db)
    hunt.register()
    app.bot_data["ticket_hunt"] = hunt
    return hunt

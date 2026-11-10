"""Booking watcher: keeps a BookingWatch per Eventbrite market and pings the chat around the opening.

Free tickets go in seconds, sometimes before the announced time, so around the opening
(T-15 min .. T+10 min) the status is checked every 20 s and the chat hears about it at once.
Every ping key is stored in BookingWatch.pings_sent, so nothing is sent twice, even across restarts.
BookingWatch.autobook / autobook_state belong to the auto-booker (M4) and are never touched here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta
from html import escape
from typing import Any

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from .. import hooks
from ..db import Account, BookingWatch, Database, Event, utcnow
from .client import EventbriteClient, TicketStatus
from .resolver import Resolver, candidate_links, has_eventbrite_signal, is_eventbrite, is_link_page

log = logging.getLogger(__name__)

ACTIVE = ("yes", "maybe")  # pinged
TRACKED = ("pending", "yes", "maybe")  # watch row kept up to date

PINGS = (
    ("t-15", timedelta(minutes=-15), "⏳ Booking opens in 15 min — be ready"),
    ("t-1", timedelta(minutes=-1), "⏳ Booking opens in 1 min — be ready"),
    ("t0", timedelta(0), "🚨 Booking is open NOW"),
)
FAST_BEFORE = timedelta(minutes=15)
FAST_AFTER = timedelta(minutes=10)
CXL_WINDOW = timedelta(minutes=10)
RESOLVE_MIN_GAP = timedelta(seconds=90)
MAX_FLIPS = 8  # live/sold-out alerts per event, against a flapping page
SCHEDULE_KINDS = ("t-15", "t-1", "t0", "fast", "after", "slow")

STATE_LABEL = {
    "resolving": "looking for the Eventbrite link",
    "waiting": "not open yet",
    "live": "tickets available",
    "sold_out": "sold out",
    "done": "closed",
    "error": "error",
}
STATE_FROM_TICKETS = {"not_open": "waiting", "open": "live", "sold_out": "sold_out", "ended": "done"}


def checkout_url(eid: str) -> str:
    return f"https://www.eventbrite.es/checkout-external?eid={eid}"


class BookingWatcher:
    def __init__(self, settings, db: Database, app: Application, client: EventbriteClient | None = None,  # noqa: ANN001
                 now=None):  # noqa: ANN001
        self.settings = settings
        self.db = db
        self.app = app
        self.tz = settings.tz
        self.chat_id = settings.allowed_chat_id
        self.client = client or EventbriteClient(getattr(settings, "eventbrite_token", ""))
        self.resolver = Resolver(self.client)
        self.now = now or utcnow
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()
        self._last_resolve: dict[int, datetime] = {}
        self._eb_opens: dict[int, datetime] = {}  # sales_start last reported by Eventbrite
        has_token = bool(self.client.token)
        self.fast_interval = 20 if has_token else 45  # public-page fallback: keep requests rare
        self.slow_interval = 600 if has_token else 1200

    @property
    def jq(self):  # noqa: ANN201
        return self.app.job_queue

    # ============================================================ registration
    def register(self) -> None:
        from ..bot import templates as T  # read-only use of the extension registry

        hooks.on("decision_changed", self.on_decision_changed)
        hooks.on("event_created", self.on_event_created)
        hooks.on("event_updated", self.on_event_updated)
        T.EXTRA_ANSWERED_BUTTONS.append(self.card_buttons)
        self.app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^eb:"))
        self.app.add_handler(CommandHandler("bookings", self.cmd_bookings,
                                            filters=filters.Chat(chat_id=self.chat_id)))
        self.jq.run_once(self._startup_job, when=5, name="eb-startup")
        self.jq.run_daily(self._daily_job, time(hour=9, minute=13, tzinfo=self.tz), name="eb-daily")

    # ================================================================== hooks
    async def on_decision_changed(self, event_id: int, old: str = "", new: str = "", **_: Any) -> None:
        self._ensure_watch(event_id)  # synchronously, so the card redrawn right after shows the toggle
        self._spawn(self.refresh(event_id, reason="decision", force=True))

    async def on_event_created(self, event_id: int, kind: str = "", **_: Any) -> None:
        self._ensure_watch(event_id)
        self._spawn(self.refresh(event_id, reason="created", force=True))

    async def on_event_updated(self, event_id: int, changes: list[str] | None = None, **_: Any) -> None:
        changes = changes or []
        cxl = any("cancel" in (c or "").lower() for c in changes)
        self._ensure_watch(event_id)
        self._spawn(self.refresh(event_id, reason="cancellations" if cxl else "updated", force=True))
        if cxl:
            ev, _, w = self._load(event_id)
            if ev and w and ev.user_decision in ACTIVE and w.reminders_on:
                self._remove_jobs(event_id, ("cxl",))
                self.jq.run_repeating(self._cxl_job, interval=self.fast_interval * 1.5, first=self.fast_interval,
                                      last=self.now() + CXL_WINDOW, name=f"eb:{event_id}:cxl",
                                      data={"event_id": event_id})

    def _spawn(self, coro) -> None:  # noqa: ANN001
        async def safe() -> None:
            try:
                await coro
            except Exception:  # noqa: BLE001
                log.exception("Booking watcher task failed")

        task = asyncio.get_running_loop().create_task(safe())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ================================================================ storage
    def _load(self, event_id: int) -> tuple[Event | None, list[str], BookingWatch | None]:
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            if ev is None:
                return None, [], None
            acc = s.get(Account, ev.account_id)
            bio = acc.bio_links if acc else []
            w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))
            return ev, bio, w

    def get_watch(self, event_id: int) -> BookingWatch | None:
        """Public: the auto-booker (M4) reads eventbrite_id, event_url, opens_at, state from here."""
        with self.db.session() as s:
            return s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))

    def _update_watch(self, event_id: int, **fields: Any) -> BookingWatch | None:
        with self.db.session() as s:
            w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))
            if w is None:
                return None
            for k, v in fields.items():
                setattr(w, k, v)
            return w

    def _add_ping(self, event_id: int, key: str) -> None:
        with self.db.session() as s:
            w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))
            if w is not None:
                keys = json.loads(w.pings_sent or "[]")
                if key not in keys:
                    keys.append(key)
                w.pings_sent = json.dumps(keys)

    @staticmethod
    def _pings(w: BookingWatch) -> list[str]:
        return json.loads(w.pings_sent or "[]")

    def _upcoming(self, ev: Event) -> bool:
        if ev.status == "past":
            return False
        ds = ev.date_list
        return not ds or max(ds) >= self.now().astimezone(self.tz).date()

    def _trackable(self, ev: Event, bio: list[str]) -> bool:
        if ev.user_decision not in TRACKED or not self._upcoming(ev):
            return False
        # Bio links swap per market and some accounts always have one, so the bio only counts when
        # the post points there ("link en bio") or the booking is known to be Eventbrite.
        use_bio = ev.link_in_bio or ev.booking_method == "eventbrite"
        links = bio if use_bio else []
        if has_eventbrite_signal(ev.booking_method, ev.booking_url, links):
            return True
        # "link en bio" pointing at a Linktree that may hold the Eventbrite link (homemarket_lmh).
        return ev.booking_method in ("other_link", "unknown") and ev.link_in_bio and any(
            is_link_page(u) for u in [ev.booking_url or "", *links])

    def _ensure_watch(self, event_id: int) -> BookingWatch | None:
        try:
            ev, bio, w = self._load(event_id)
            if ev is None or w is not None or not self._trackable(ev, bio):
                return w
            with self.db.session() as s:
                w = BookingWatch(event_id=event_id, state="resolving", opens_at=ev.booking_opens_at)
                s.add(w)
            log.info("Booking watch created for event %s (%s)", event_id, ev.title)
            return w
        except Exception:  # noqa: BLE001
            log.exception("Could not create booking watch for event %s", event_id)
            return None

    # ================================================================ refresh
    async def refresh(self, event_id: int, reason: str = "", force: bool = False) -> BookingWatch | None:
        """Resolve if needed, check tickets, alert on changes and (re)schedule jobs. Never raises."""
        try:
            async with self._locks[event_id]:
                return await self._refresh(event_id, reason, force)
        except Exception:  # noqa: BLE001
            log.exception("Booking refresh for event %s (%s) failed", event_id, reason)
            return None

    async def _refresh(self, event_id: int, reason: str, force: bool) -> BookingWatch | None:
        ev, bio, w = self._load(event_id)
        if ev is None:
            return None
        if w is None:
            w = self._ensure_watch(event_id)
            if w is None:
                return None
            ev, bio, w = self._load(event_id)
        if not self._trackable(ev, bio):
            self._remove_jobs(event_id, SCHEDULE_KINDS + ("cxl",))
            return w
        active = ev.user_decision in ACTIVE
        before = (w.eventbrite_id, w.opens_at, w.state, w.reminders_on)
        found_now = False

        now = self.now()
        last = self._last_resolve.get(event_id)
        if not w.eventbrite_id and (reason not in ("fast", "cxlwatch") or not last or now - last >= RESOLVE_MIN_GAP):
            self._last_resolve[event_id] = now
            r = await self.resolver.resolve(ev.date_list, ev.booking_url, bio)
            if r:
                log.info("Event %s → Eventbrite %s (%s)", event_id, r.eventbrite_id, r.url)
                w = self._update_watch(event_id, eventbrite_id=r.eventbrite_id, event_url=r.url)
                found_now = True

        st: TicketStatus | None = None
        if w.eventbrite_id and (active or reason == "created"):
            st = await self.client.status(w.eventbrite_id, w.event_url)
        w = self._apply(ev, w, st)

        if st is not None and active:
            await self._alert_for_status(ev, w, st, reason)
            w = self.get_watch(event_id) or w

        if force or found_now or before != (w.eventbrite_id, w.opens_at, w.state, w.reminders_on):
            self._schedule(ev, w)
        if found_now and ev.user_decision != "pending":
            await self._redraw_card(ev)
        return w

    def _apply(self, ev: Event, w: BookingWatch, st: TicketStatus | None) -> BookingWatch:
        ig = ev.booking_opens_at
        eb = st.sales_start if st else None
        if eb and ig and abs((eb - ig).total_seconds()) > 60:
            log.info("Event %s: Instagram says booking opens %s, Eventbrite says %s — trusting Eventbrite",
                     ev.id, ig.isoformat(), eb.isoformat())
        if eb:
            self._eb_opens[ev.id] = eb
            opens = eb
        else:
            opens = self._eb_opens.get(ev.id) or ig or w.opens_at  # Eventbrite wins when it has spoken
        fields: dict[str, Any] = {"opens_at": opens}
        if st is not None:
            fields["last_checked_at"] = self.now()
            if st.is_free is not None:
                fields["is_free"] = st.is_free
            if st.url and is_eventbrite(st.url) and not w.event_url:
                fields["event_url"] = st.url
            new_state = STATE_FROM_TICKETS.get(st.state)
            if new_state:
                fields["state"] = new_state
        if "state" not in fields and w.state == "resolving" and w.eventbrite_id:
            fields["state"] = "waiting"
        if eb and ig is None:
            with self.db.session() as s:  # fill the gap so the card shows the opening time too
                row = s.get(Event, ev.id)
                if row is not None and row.booking_opens_at is None:
                    row.booking_opens_at = eb
        return self._update_watch(ev.id, **fields) or w

    # ================================================================= alerts
    @staticmethod
    def _last_flip(keys: list[str]) -> str | None:
        flips = [k for k in keys if k.startswith(("live", "soldout"))]
        if not flips:
            return None
        return "live" if flips[-1].startswith("live") else "soldout"

    async def _alert_for_status(self, ev: Event, w: BookingWatch, st: TicketStatus, reason: str) -> None:
        if not w.reminders_on:
            return
        keys = self._pings(w)
        flips = [k for k in keys if k.startswith(("live", "soldout"))]
        last = self._last_flip(keys)
        if st.state == "open":
            if reason in ("cancellations", "cxlwatch"):
                key = "cxl:" + self.now().strftime("%Y%m%d%H")
                if key not in keys:
                    await self._send(ev, w, "🔁 Cancellations — tickets available again", key)
                return
            if last != "live" and len(flips) < MAX_FLIPS:
                await self._send(ev, w, "🚨 Tickets are live", f"live{len(flips)}")
        elif st.state == "sold_out":
            if last != "soldout" and len(flips) < MAX_FLIPS:
                await self._send(ev, w, "⛔ Sold out", f"soldout{len(flips)}")

    def _url_for(self, ev: Event, w: BookingWatch | None) -> str | None:
        if w and w.event_url:
            return w.event_url
        if w and w.eventbrite_id:
            return f"https://www.eventbrite.es/e/{w.eventbrite_id}"
        if ev.booking_url and ev.booking_url.startswith(("http://", "https://")):
            return ev.booking_url
        with self.db.session() as s:
            acc = s.get(Account, ev.account_id)
            bio = acc.bio_links if acc else []
        links = candidate_links(None, bio)
        return links[0] if links else ev.source_url

    def keyboard(self, ev: Event, w: BookingWatch | None) -> InlineKeyboardMarkup | None:
        rows = []
        url = self._url_for(ev, w)
        if url:
            label = "🎟 Open Eventbrite" if is_eventbrite(url) or is_link_page(url) or (w and w.eventbrite_id) \
                else "🔗 Open link"
            rows.append([InlineKeyboardButton(label, url=url)])
        if w and w.eventbrite_id:
            rows.append([InlineKeyboardButton("⚡ Straight to checkout", url=checkout_url(w.eventbrite_id))])
        return InlineKeyboardMarkup(rows) if rows else None

    def _message(self, ev: Event, w: BookingWatch, text: str) -> str:
        lines = [escape(text), f"🪑 <b>{escape(ev.title)}</b>"]
        if ev.booking_scope:
            lines.append(f"🎟 {escape(ev.booking_scope)}")
        if w.opens_at and text.startswith("⏳"):
            lines.append(f"⏰ {w.opens_at.astimezone(self.tz).strftime('%a %-d %b %H:%M')}")
        if not w.eventbrite_id:
            lines.append("<i>Eventbrite link not found yet — check their Instagram bio.</i>")
        return "\n".join(lines)

    async def _send(self, ev: Event, w: BookingWatch, text: str, key: str) -> bool:
        try:
            await self.app.bot.send_message(self.chat_id, self._message(ev, w, text), parse_mode=ParseMode.HTML,
                                            reply_markup=self.keyboard(ev, w), disable_web_page_preview=True)
        except TelegramError:
            log.exception("Booking ping %s for event %s failed", key, ev.id)
            return False
        self._add_ping(ev.id, key)
        log.info("Booking ping %s sent for event %s", key, ev.id)
        return True

    async def _redraw_card(self, ev: Event) -> None:
        if not ev.telegram_message_id:
            return
        try:
            from ..bot import templates as T

            with self.db.session() as s:
                fresh = s.get(Event, ev.id)
            await self.app.bot.edit_message_reply_markup(self.chat_id, fresh.telegram_message_id,
                                                         reply_markup=T.answered_keyboard(fresh))
        except Exception as exc:  # noqa: BLE001
            log.info("Could not redraw card for event %s: %s", ev.id, exc)

    # ============================================================= scheduling
    def _remove_jobs(self, event_id: int, kinds: tuple[str, ...]) -> None:
        for kind in kinds:
            for job in self.jq.get_jobs_by_name(f"eb:{event_id}:{kind}"):
                job.schedule_removal()

    def _schedule(self, ev: Event, w: BookingWatch) -> None:
        eid = ev.id
        self._remove_jobs(eid, SCHEDULE_KINDS)
        if ev.user_decision not in ACTIVE or not w.reminders_on or not self._upcoming(ev) or w.state == "done":
            return
        now = self.now()
        o = w.opens_at
        data = {"event_id": eid}
        if o and now < o + FAST_AFTER:
            sent = self._pings(w)
            for key, offset, _ in PINGS:
                at = o + offset
                if key not in sent and at > now:
                    self.jq.run_once(self._ping_job, when=at, name=f"eb:{eid}:{key}", data={"event_id": eid, "key": key})
            start = max(now, o - FAST_BEFORE)
            first = (start - now).total_seconds() or float(self.fast_interval)
            self.jq.run_repeating(self._fast_job, interval=self.fast_interval, first=first, last=o + FAST_AFTER,
                                  name=f"eb:{eid}:fast", data=data)
            self.jq.run_once(self._after_job, when=o + FAST_AFTER + timedelta(seconds=5), name=f"eb:{eid}:after",
                             data=data)
            if not w.eventbrite_id and o - FAST_BEFORE > now:
                # Link not found yet: keep looking until the window starts.
                self.jq.run_repeating(self._slow_job, interval=self.slow_interval, first=60,
                                      last=o - FAST_BEFORE, name=f"eb:{eid}:slow", data=data)
            return
        if w.eventbrite_id and w.state in ("live", "sold_out"):
            return  # already told; a cancellations post re-checks via the hook
        # Opening time unknown (or passed without the tickets showing up): poll.
        self.jq.run_repeating(self._slow_job, interval=self.slow_interval, first=60, name=f"eb:{eid}:slow", data=data)

    # =================================================================== jobs
    async def _ping_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            eid, key = ctx.job.data["event_id"], ctx.job.data["key"]
            ev, _, w = self._load(eid)
            if not ev or not w or ev.user_decision not in ACTIVE or not w.reminders_on:
                return
            keys = self._pings(w)
            if key in keys or w.state in ("sold_out", "done"):
                return
            if self._last_flip(keys) == "live" and w.state == "live":
                return  # opened early and we already said so
            text = next(t for k, _, t in PINGS if k == key)
            await self._send(ev, w, text, key)
        except Exception:  # noqa: BLE001
            log.exception("Booking ping job failed")

    async def _fast_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.refresh(ctx.job.data["event_id"], reason="fast")

    async def _cxl_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.refresh(ctx.job.data["event_id"], reason="cxlwatch")

    async def _after_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.refresh(ctx.job.data["event_id"], reason="after", force=True)

    async def _slow_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        eid = ctx.job.data["event_id"]
        try:
            ev, _, _ = self._load(eid)
            if ev is None or not self._upcoming(ev) or ev.user_decision not in ACTIVE:
                ctx.job.schedule_removal()
                return
        except Exception:  # noqa: BLE001
            log.exception("Booking slow job failed")
            return
        await self.refresh(eid, reason="poll")

    async def _refresh_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.refresh(ctx.job.data["event_id"], reason=ctx.job.data.get("reason", "boot"), force=True)

    def _tracked_events(self) -> list[tuple[Event, BookingWatch | None, list[str]]]:
        out = []
        with self.db.session() as s:
            evs = list(s.scalars(select(Event).where(Event.status != "past", Event.user_decision.in_(TRACKED))))
            for ev in evs:
                acc = s.get(Account, ev.account_id)
                bio = acc.bio_links if acc else []
                w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == ev.id))
                out.append((ev, w, bio))
        return out

    async def startup(self) -> int:
        """Backfill missing watches, rebuild jobs from stored rows (no network), then refresh gently."""
        n = 0
        for ev, w, bio in self._tracked_events():
            try:
                if not self._trackable(ev, bio):
                    continue
                if w is None:
                    w = self._ensure_watch(ev.id)
                    if w is None:
                        continue
                if ev.user_decision in ACTIVE:
                    self._schedule(ev, w)
                    self.jq.run_once(self._refresh_job, when=10 + 15 * n, name=f"eb:{ev.id}:boot",
                                     data={"event_id": ev.id, "reason": "boot"})
                    n += 1
            except Exception:  # noqa: BLE001
                log.exception("Booking startup for event %s failed", ev.id)
        log.info("Booking watcher started: %d active watches", n)
        return n

    async def _startup_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.startup()
        except Exception:  # noqa: BLE001
            log.exception("Booking watcher startup failed")

    async def daily(self) -> None:
        for ev, w, bio in self._tracked_events():
            if w is None or not self._trackable(ev, bio):
                continue
            if not w.eventbrite_id or ev.user_decision in ACTIVE:
                await self.refresh(ev.id, reason="daily", force=True)

    async def _daily_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.daily()
        except Exception:  # noqa: BLE001
            log.exception("Booking daily job failed")

    # ============================================================== telegram
    def card_buttons(self, ev: Event) -> list[InlineKeyboardButton]:
        try:
            w = self.get_watch(ev.id)
        except Exception:  # noqa: BLE001
            log.exception("card_buttons failed")
            return []
        if w is None:
            return []
        row = [InlineKeyboardButton(f"🔔 Booking reminders: {'on' if w.reminders_on else 'off'}",
                                    callback_data=f"eb:toggle:{ev.id}")]
        if w.eventbrite_id:
            row.append(InlineKeyboardButton("🎟 Eventbrite", url=self._url_for(ev, w)))
        return row

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        try:
            if not (update.effective_chat and update.effective_chat.id == self.chat_id):
                await q.answer()
                return
            parts = (q.data or "").split(":")
            if len(parts) == 3 and parts[1] == "toggle":
                await self.toggle(int(parts[2]), q)
                return
            await q.answer()
        except Exception:  # noqa: BLE001
            log.exception("Booking callback %r failed", getattr(q, "data", None))
            try:
                await q.answer("Something went wrong")
            except Exception:  # noqa: BLE001
                pass

    async def toggle(self, event_id: int, q=None) -> bool | None:  # noqa: ANN001
        from ..bot import templates as T

        ev, _, w = self._load(event_id)
        if not ev or not w:
            if q:
                await q.answer("No booking watch for this market")
            return None
        on = not w.reminders_on
        w = self._update_watch(event_id, reminders_on=on)
        if on:
            self._schedule(ev, w)
        else:
            self._remove_jobs(event_id, SCHEDULE_KINDS + ("cxl",))
        if q:
            await q.answer("Booking reminders on" if on else "Booking reminders off")
            if ev.user_decision != "pending":
                try:
                    await q.edit_message_reply_markup(T.answered_keyboard(ev))
                except TelegramError as exc:
                    log.info("Could not redraw card: %s", exc)
            if on and ev.user_decision in ACTIVE:
                self._spawn(self.refresh(event_id, reason="toggle", force=True))
        return on

    def bookings_text(self) -> str:
        rows = []
        for ev, w, bio in self._tracked_events():
            if w is None or not self._upcoming(ev):
                continue
            rows.append((ev, w))
        if not rows:
            return "No Eventbrite bookings being watched. Mark an Eventbrite market ✅ or 🤔 and I'll watch it."
        far = datetime.max.replace(tzinfo=self.now().tzinfo)
        rows.sort(key=lambda r: (r[1].opens_at or far, r[0].id))
        lines = ["🎟 <b>Eventbrite bookings</b>"]
        for ev, w in rows:
            when = w.opens_at.astimezone(self.tz).strftime("%a %-d %b %H:%M") if w.opens_at else "opening time unknown"
            flag = "🔔 on" if w.reminders_on else "🔕 off"
            pending = " <i>(not answered)</i>" if ev.user_decision == "pending" else ""
            link = f' · <a href="{escape(self._url_for(ev, w) or "", quote=True)}">link</a>' if w.eventbrite_id else ""
            hunt = " · 🔎 watching for released tickets" if getattr(w, "hunt_on", False) else ""
            lines.append(f"\n🪑 <b>{escape(ev.title)}</b>{pending}\n⏰ {when} · {STATE_LABEL.get(w.state, w.state)}"
                         f" · {flag}{link}{hunt}")
        return "\n".join(lines)

    async def cmd_bookings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await update.effective_message.reply_html(self.bookings_text(), disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.exception("/bookings failed")


def setup_booking_watcher(app: Application, settings, db: Database) -> BookingWatcher:  # noqa: ANN001
    watcher = BookingWatcher(settings, db, app)
    watcher.register()
    app.bot_data["booking"] = watcher
    return watcher

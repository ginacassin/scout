"""M4 phase 2: opt-in auto-booking of FREE Eventbrite tickets through the scout-booker service.

Scout only arms/cancels jobs and shows buttons; the booker container (booker/) owns the browser,
does the attempt at the opening second and messages the user itself.

Env: BOOKER_URL (default http://scout-booker:8100, "off" disables), BOOKER_TOKEN (shared secret).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from html import escape

import httpx
from sqlalchemy import select
from telegram import InlineKeyboardButton, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from . import hooks
from .bot import templates as T
from .db import BookingWatch, Database, Event, utcnow

log = logging.getLogger(__name__)

DEFAULT_URL = "http://scout-booker:8100"
OFF_VALUES = {"off", "0", "false", "no", "none", "disabled"}
CALLBACK_RE = re.compile(r"^ab:(on|off):(\d+)$")
SYNC_EVERY_S = 600          # re-sync armed jobs (opening time may move)
HEALTH_RETRY_S = 3600       # while the booker is unreachable, look again hourly

STATE_LABEL = {
    "scheduled": "armed",
    "running": "booking now…",
    "booked": "✅ booked",
    "cancelled": "cancelled",
}


def state_label(state: str | None) -> str:
    if not state:
        return "?"
    if state.startswith("failed:"):
        return "⚠️ failed (" + state.split(":", 1)[1].replace("_", " ") + ")"
    return STATE_LABEL.get(state, state)


class BookerClient:
    def __init__(self, base_url: str, token: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Booker-Token": token} if token else {}
        self.transport = transport

    async def _call(self, method: str, path: str, timeout: float = 15, **kw) -> tuple[int, dict]:  # noqa: ANN003
        async with httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=timeout,
                                     transport=self.transport) as c:
            r = await c.request(method, path, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return r.status_code, data if isinstance(data, dict) else {}

    async def health(self) -> tuple[int, dict]:
        return await self._call("GET", "/health", timeout=10)

    async def book(self, payload: dict) -> tuple[int, dict]:
        return await self._call("POST", "/book", json=payload)

    async def cancel(self, event_id: int) -> tuple[int, dict]:
        return await self._call("DELETE", f"/book/{event_id}")

    async def jobs(self) -> tuple[int, dict]:
        return await self._call("GET", "/jobs")

    async def check_login(self) -> tuple[int, dict]:
        return await self._call("POST", "/check-login", timeout=60)

    async def login_start(self) -> tuple[int, dict]:
        return await self._call("POST", "/login/start", timeout=60)

    async def login_stop(self) -> tuple[int, dict]:
        return await self._call("POST", "/login/stop", timeout=30)


def _parse(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def eligible(ev: Event | None, w: BookingWatch | None, now: datetime | None = None) -> bool:
    """✅ event, Eventbrite id known, not known to be paid, sales not open yet."""
    now = now or utcnow()
    return bool(
        ev is not None and ev.user_decision == "yes"
        and w is not None and w.eventbrite_id
        and w.is_free is not False
        and w.opens_at is not None and w.opens_at > now
    )


class AutoBook:
    def __init__(self, settings, db: Database, client: BookerClient | None):  # noqa: ANN001
        self.settings = settings
        self.db = db
        self.client = client
        self.enabled = False
        self._warned = False
        self._last_health = 0.0

    # ---------------------------------------------------------------- health
    async def check_health(self) -> bool:
        if self.client is None:
            return False
        self._last_health = time.monotonic()
        try:
            status, data = await self.client.health()
            ok = status == 200 and bool(data.get("ok"))
            why = f"HTTP {status}"
        except httpx.HTTPError as exc:
            ok, why = False, f"{type(exc).__name__}"
        if ok and not self.enabled:
            log.info("Auto-book enabled (booker at %s)", self.client.base_url)
            self._warned = False
        elif not ok and not self._warned:
            log.warning("Auto-book disabled: booker at %s not reachable (%s); retrying hourly",
                        self.client.base_url, why)
            self._warned = True
        self.enabled = ok
        return ok

    # --------------------------------------------------------------- buttons
    def _load(self, event_id: int) -> tuple[Event | None, BookingWatch | None]:
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))
            return ev, w

    def buttons(self, ev: Event) -> list[InlineKeyboardButton]:
        """Registered in templates.EXTRA_ANSWERED_BUTTONS. Never raises: a card must always render."""
        if not self.enabled or ev.user_decision != "yes":
            return []
        try:
            _, w = self._load(ev.id)
        except Exception:  # noqa: BLE001
            log.exception("Auto-book button lookup failed for event %s", ev.id)
            return []
        if not eligible(ev, w):
            return []
        if w.autobook:
            return [InlineKeyboardButton("🤖 Auto-book: armed — cancel", callback_data=f"ab:off:{ev.id}")]
        return [InlineKeyboardButton("🤖 Auto-book at opening", callback_data=f"ab:on:{ev.id}")]

    def _set(self, event_id: int, autobook: bool | None, state: str | None) -> None:
        with self.db.session() as s:
            w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))
            if w is None:
                return
            if autobook is not None:
                w.autobook = autobook
            w.autobook_state = state

    def _fmt(self, dt: datetime) -> str:
        return dt.astimezone(self.settings.tz).strftime("%a %-d %b %H:%M")

    # ---------------------------------------------------------------- actions
    async def arm(self, event_id: int) -> tuple[str, bool]:
        """Returns (toast, show_alert)."""
        if not self.enabled or self.client is None:
            return "Auto-book is unavailable right now (the booker isn't reachable).", True
        ev, w = self._load(event_id)
        if not eligible(ev, w):
            return "Auto-book only works for free Eventbrite bookings that haven't opened yet.", True
        payload = {
            "event_id": ev.id,
            "eventbrite_id": w.eventbrite_id,
            "event_url": w.event_url or (ev.booking_url if (ev.booking_url or "").startswith("https://") else ""),
            "opens_at": w.opens_at.isoformat(),
            "title": ev.title,
        }
        try:
            status, data = await self.client.book(payload)
        except httpx.HTTPError:
            log.exception("Arming event %s failed", event_id)
            return "Couldn't reach the booker. Try again in a minute.", True
        job = data.get("job") or {}
        if status == 409:
            self._set(event_id, None, job.get("state"))
            return f"Already attempted: {state_label(job.get('state'))}.", True
        if status != 200:
            return f"The booker refused: {data.get('error') or status}.", True
        self._set(event_id, True, job.get("state", "scheduled"))
        return f"🤖 Armed — I'll try at {self._fmt(w.opens_at)}.", False

    async def disarm(self, event_id: int) -> tuple[str, bool]:
        if self.client is None:
            return "Auto-book is off.", True
        try:
            status, data = await self.client.cancel(event_id)
        except httpx.HTTPError:
            log.exception("Cancelling event %s failed", event_id)
            return "Couldn't reach the booker to cancel. Try again in a minute.", True
        job = data.get("job") or {}
        if status in (200, 404):
            self._set(event_id, False, "cancelled")
            return "Auto-book cancelled.", False
        if status == 409:
            self._set(event_id, None, job.get("state"))
            return f"Too late to cancel: {state_label(job.get('state'))}.", True
        return f"The booker refused: {data.get('error') or status}.", True

    # ---------------------------------------------------------- telegram glue
    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        chat = update.effective_chat
        m = CALLBACK_RE.match(q.data or "")
        if not chat or chat.id != self.settings.allowed_chat_id or not m:
            await q.answer()
            return
        action, event_id = m.group(1), int(m.group(2))
        toast, alert = await (self.arm(event_id) if action == "on" else self.disarm(event_id))
        await q.answer(toast, show_alert=alert)
        ev, _ = self._load(event_id)
        if ev is None:
            return
        try:
            await q.edit_message_reply_markup(T.answered_keyboard(ev))
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("Couldn't refresh keyboard for event %s: %s", event_id, exc)

    async def on_decision(self, event_id: int, old: str, new: str, **_kw) -> None:  # noqa: ANN003
        if new == "yes":
            return
        _, w = self._load(event_id)
        if w is not None and w.autobook:
            toast, _ = await self.disarm(event_id)
            log.info("Event %s no longer ✅: %s", event_id, toast)

    async def tick(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """Every 10 min: health (hourly while down) and keep armed jobs in step with the watcher's data."""
        if not self.enabled and self._last_health and time.monotonic() - self._last_health < HEALTH_RETRY_S:
            return
        if not await self.check_health():
            return
        try:
            await self.sync()
        except httpx.HTTPError:
            log.warning("Auto-book sync failed", exc_info=True)

    async def sync(self) -> None:
        status, data = await self.client.jobs()
        if status != 200:
            return
        jobs = {int(j["event_id"]): j for j in data.get("jobs", []) if "event_id" in j}
        with self.db.session() as s:
            armed = list(s.scalars(select(BookingWatch).where(BookingWatch.autobook.is_(True))))
        now = utcnow()
        for w in armed:
            job = jobs.get(w.event_id)
            ev, _ = self._load(w.event_id)
            if job and job.get("state") != "scheduled":
                self._set(w.event_id, None, job.get("state"))
                continue
            if not eligible(ev, w, now):
                if w.opens_at is None or w.opens_at <= now:
                    continue  # the attempt (or its absence) is reported by the booker
                await self.disarm(w.event_id)  # e.g. turned out to be paid
                continue
            if job is None or _parse(job.get("opens_at")) != w.opens_at:
                log.info("Re-arming event %s (booker had %s)", w.event_id, job and job.get("opens_at"))
                await self.arm(w.event_id)

    async def cmd_autobook(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if self.client is None:
            await msg.reply_text("Auto-book is turned off (BOOKER_URL=off).")
            return
        arg = (ctx.args[0].lower() if ctx.args else "")
        try:
            if arg == "login":
                status, data = await self.client.login_start()
                if status != 200:
                    await msg.reply_text(f"The booker couldn't start login mode ({data.get('error') or status}).")
                    return
                url = os.environ.get("BOOKER_VNC_URL") or data.get("vnc_url") or "http://192.168.1.100:6080/vnc.html"
                mins = int(data.get("closes_in_s", 900)) // 60
                await msg.reply_html(
                    "🔑 Login mode is on.\n"
                    f"1. On your home Wi-Fi, open {escape(url)} and press <b>Connect</b>.\n"
                    "2. Log in to Eventbrite in that browser window (your password goes only to Eventbrite).\n"
                    f"3. Send /autobook done when you're in. It closes by itself after {mins} min.",
                    disable_web_page_preview=True)
                return
            if arg == "done":
                await self.client.login_stop()
                _, data = await self.client.check_login()
                li = data.get("logged_in")
                text = {True: "✅ Logged in to Eventbrite. Auto-book is ready.",
                        False: "⚠️ Still not logged in. Try /autobook login again."}.get(
                    li, "❔ Couldn't tell whether you're logged in; I'll check again before each booking.")
                await msg.reply_text(text)
                return
            await self._show(update)
        except httpx.HTTPError:
            log.exception("/autobook failed")
            await msg.reply_text("Couldn't reach the booker. Is the scout-booker service running?")

    async def _show(self, update: Update) -> None:
        msg = update.effective_message
        await self.check_health()
        if not self.enabled:
            await msg.reply_text("🤖 Auto-book is unavailable: the booker isn't reachable. I'll look again hourly.")
            return
        await self.sync()
        _, health = await self.client.health()
        _, data = await self.client.jobs()
        li = health.get("logged_in")
        lines = ["🤖 <b>Auto-book</b> (free Eventbrite tickets only)",
                 "Eventbrite login: " + {True: "✅ logged in", False: "⚠️ logged out"}.get(li, "❔ not checked yet")]
        jobs = data.get("jobs", [])
        upcoming = [j for j in jobs if j.get("state") in ("scheduled", "running")]
        recent = [j for j in jobs if j not in upcoming and j.get("state") != "cancelled"][-5:]
        if upcoming:
            lines += ["", "<b>Armed</b>"]
            for j in upcoming:
                opens = _parse(j.get("opens_at"))
                when = self._fmt(opens) if opens else "?"
                lines.append(f"• {escape(j.get('title', '?'))} — opens {when} — {state_label(j.get('state'))}")
        else:
            lines += ["", "Nothing armed. Tap 🤖 Auto-book at opening on a ✅ free Eventbrite market."]
        if recent:
            lines += ["", "<b>Recent</b>"]
            lines += [f"• {escape(j.get('title', '?'))} — {state_label(j.get('state'))}" for j in recent]
        if li is not True:
            lines += ["", "To log in once: /autobook login, open the link on your home Wi-Fi, then /autobook done."]
        await msg.reply_html("\n".join(lines), disable_web_page_preview=True)


def setup_autobook(app: Application, settings, db: Database) -> AutoBook:  # noqa: ANN001
    url = (os.environ.get("BOOKER_URL") or DEFAULT_URL).strip()
    client = None if url.lower() in OFF_VALUES else BookerClient(url, os.environ.get("BOOKER_TOKEN", ""))
    ab = AutoBook(settings, db, client)
    app.add_handler(CommandHandler("autobook", ab.cmd_autobook, filters=filters.Chat(chat_id=settings.allowed_chat_id)))
    if client is None:
        log.info("Auto-book disabled (BOOKER_URL=off)")
        return ab
    T.EXTRA_ANSWERED_BUTTONS.append(ab.buttons)
    hooks.on("decision_changed", ab.on_decision)
    app.add_handler(CallbackQueryHandler(ab.on_callback, pattern=r"^ab:"))
    if app.job_queue is not None:
        app.job_queue.run_repeating(ab.tick, interval=SYNC_EVERY_S, first=5, name="autobook-sync")
    return ab

"""Bookings you made yourself, ticket QRs, and notes on events.

Commands: /booked <id> <HH:MM> [date] · /unbook <id> · /ticket <id> (or a photo/PDF captioned
"/ticket <id>") · /note <id> <text> · /notes <id> · /delnote <id> <n>.
Buttons on answered cards: "🎟 I booked it" (Eventbrite events, bk: callbacks) and "📝 Add note"
(nt: callbacks). Replying to an event card with plain text adds a note to that event.

Bookings go through scout.bookings.record_booking (the calendar reacts to its hook). A ticket is
always sent back as a reply to the event card and pinned, whatever happens with Google Drive.
On the market day a reminder with your notes, the address and a Google Maps link arrives at 08:30,
or one hour before a booked slot.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .. import bookings, hooks
from ..db import Account, Database, Event
from . import templates as T

log = logging.getLogger(__name__)

PENDING_TTL = {"note": timedelta(minutes=15), "time": timedelta(minutes=15), "ticket": timedelta(minutes=30),
               "file": timedelta(minutes=30)}
REMINDER_AT = time(8, 30)
REMINDER_BEFORE_SLOT = timedelta(hours=1)
SENT_KEY = "note_reminders_sent"
PIN_KEY = "ticket_pin:{}"  # setting: {"path": str, "msg": int}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
TIME_RE = re.compile(r"^(\d{1,2})(?:[:.h](\d{2}))?$")
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
            "lun": 0, "mar": 1, "mie": 2, "mié": 2, "jue": 3, "vie": 4, "sab": 5, "sáb": 5, "dom": 6}
TICKET_CAPTION_RE = re.compile(r"^/ticket(?:@\w+)?\s+#?(\d+)", re.I)
NOTE_CAPTION_RE = re.compile(r"^/note(?:@\w+)?\s+#?(\d+)\s*(.*)$", re.I | re.S)
MY_COMMANDS = [("booked", "Tell me you booked a slot"), ("note", "Add a note to a market"),
               ("notes", "Notes of a market"), ("unbook", "Clear a booking")]


def maps_url(address: str) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


def parse_time(text: str) -> time | None:
    m = TIME_RE.match(text.strip().lower())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return time(h, mi) if h < 24 and mi < 60 else None


def parse_day(text: str, ev_days: list[date], today: date) -> date | None:
    """ISO date, d/m[/y], d-m, a day number or a weekday — matched against the event's days first."""
    t = text.strip().lower().rstrip(".")
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    if t[:3] in WEEKDAYS or t in WEEKDAYS:
        wd = WEEKDAYS.get(t[:3], WEEKDAYS.get(t))
        hits = [d for d in ev_days if d.weekday() == wd]
        return hits[0] if hits else None
    m = re.fullmatch(r"(\d{1,2})(?:[/-](\d{1,2})(?:[/-](\d{2,4}))?)?", t)
    if not m:
        return None
    d, mo, y = int(m.group(1)), m.group(2), m.group(3)
    if mo is None:
        hits = [x for x in ev_days if x.day == d]
        return hits[0] if hits else None
    mo = int(mo)
    for x in ev_days:
        if x.day == d and x.month == mo and (y is None or x.year % 100 == int(y) % 100):
            return x
    year = (int(y) + 2000 if int(y) < 100 else int(y)) if y else today.year
    try:
        out = date(year, mo, d)
    except ValueError:
        return None
    if not y and out < today - timedelta(days=60):
        out = out.replace(year=year + 1)
    return out


def slot_times(ev: Event, day: date, limit: int = 16) -> list[str]:
    """Half-hour steps through that day's sessions (bookable ones first); a default range otherwise."""
    sessions = [s for s in ev.session_list if s.get("date") == day.isoformat() and s.get("start")]
    bookable = [s for s in sessions if s.get("booking_required")]
    out: list[str] = []
    for s in bookable or sessions:
        st = parse_time(s.get("start") or "")
        en = parse_time(s.get("end") or "")
        if not st:
            continue
        cur = datetime.combine(day, st)
        stop = datetime.combine(day, en) if en else cur + timedelta(hours=3)
        if stop <= cur:
            stop = cur + timedelta(minutes=30)
        while cur < stop:
            label = cur.strftime("%H:%M")
            if label not in out:
                out.append(label)
            cur += timedelta(minutes=30)
    if not out:
        out = [f"{h:02d}:{m:02d}" for h in range(10, 18) for m in (0, 30)]
    out.sort()
    return out[:limit]


def reminder_due(ev: Event, tz, today: date) -> datetime | None:  # noqa: ANN001
    """When the market-day reminder should fire (aware), or None if no reminder is needed."""
    if ev.user_decision in ("no", "filtered"):
        return None
    if ev.booked_start is not None:
        return ev.booked_start - REMINDER_BEFORE_SLOT
    if not ev.note_list:
        return None
    upcoming = [d for d in ev.date_list if d >= today]
    if not upcoming:
        return None
    return datetime.combine(upcoming[0], REMINDER_AT, tzinfo=tz)


class _Filter(filters.MessageFilter):
    def __init__(self, fn: Callable[[Message], bool], name: str):
        super().__init__(name=name)
        self._fn = fn

    def filter(self, message: Message) -> bool:
        try:
            return bool(self._fn(message))
        except Exception:  # noqa: BLE001
            log.exception("filter %s failed", self.name)
            return False


class BookingCommands:
    def __init__(self, app: Application, settings, db: Database, notifier=None,  # noqa: ANN001
                 now: Callable[[], datetime] | None = None):
        self.app = app
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self.tz = settings.tz
        self.chat_id = settings.allowed_chat_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.pending: dict[str, Any] | None = None

    # ------------------------------------------------------------------ setup
    def register(self) -> None:
        app = self.app
        only_me = filters.Chat(chat_id=self.chat_id)
        for name, fn in {"booked": self.cmd_booked, "unbook": self.cmd_unbook, "ticket": self.cmd_ticket,
                         "note": self.cmd_note, "notes": self.cmd_notes, "delnote": self.cmd_delnote}.items():
            app.add_handler(CommandHandler(name, fn, filters=only_me))
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^(bk|nt):"))
        # Must run before the core "I didn't get that" text handler, so it goes first in group 0;
        # its filter only matches while a question is open or for replies to an event card.
        text = MessageHandler(only_me & filters.TEXT & ~filters.COMMAND & _Filter(self.wants_text, "bk_text"),
                              self.on_text)
        app.add_handler(text, group=0)
        group = app.handlers[0]
        group.remove(text)
        group.insert(0, text)
        app.add_handler(MessageHandler(
            only_me & (filters.PHOTO | filters.Document.ALL) & _Filter(self.wants_file, "bk_file"), self.on_file))

        self.register_hooks()
        jq = app.job_queue
        if jq is not None:
            jq.run_daily(self._daily_job, time(0, 10, tzinfo=self.tz), name="note-reminders-daily")
            jq.run_once(self._startup_job, when=8, name="note-reminders-startup")

    def register_hooks(self) -> None:
        if self.card_buttons not in T.EXTRA_ANSWERED_BUTTONS:
            T.EXTRA_ANSWERED_BUTTONS.append(self.card_buttons)
        hooks.on("booking_confirmed", self.on_booking)
        hooks.on("ticket_added", self.on_ticket)
        hooks.on("note_added", self.on_note)

    async def _startup_job(self, _ctx=None) -> None:  # noqa: ANN001
        self.schedule_reminders(catch_up=True)
        try:  # add our commands to the menu set by app.py
            cmds = list(await self.app.bot.get_my_commands())
            have = {c.command for c in cmds}
            extra = [(c, d) for c, d in MY_COMMANDS if c not in have]
            if extra:
                await self.app.bot.set_my_commands([(c.command, c.description) for c in cmds] + extra)
        except TelegramError:
            log.warning("Couldn't extend the command menu", exc_info=True)

    async def _daily_job(self, _ctx=None) -> None:  # noqa: ANN001
        self.schedule_reminders()

    # ---------------------------------------------------------------- helpers
    def _load(self, event_id: int) -> tuple[Event | None, str]:
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            acc = s.get(Account, ev.account_id) if ev else None
            return ev, acc.handle if acc else "?"

    @staticmethod
    def _event_id(arg: str | None) -> int | None:
        arg = (arg or "").lstrip("#")
        return int(arg) if arg.isdigit() else None

    def _card_event(self, msg: Message | None) -> int | None:
        if msg is None or not getattr(msg, "message_id", None):
            return None
        with self.db.session() as s:
            return s.scalar(select(Event.id).where(Event.telegram_message_id == msg.message_id))

    def _pending(self, *kinds: str) -> dict[str, Any] | None:
        p = self.pending
        if not p or p["kind"] not in kinds:
            return None
        if self._now() > p["until"]:
            self.pending = None
            return None
        return p

    def _ask(self, kind: str, event_id: int, **extra: Any) -> None:
        self.pending = {"kind": kind, "event_id": event_id, "until": self._now() + PENDING_TTL[kind], **extra}

    def wants_text(self, msg: Message) -> bool:
        return bool(self._pending("note", "time") or self._card_event(msg.reply_to_message))

    def wants_file(self, msg: Message) -> bool:
        cap = msg.caption or ""
        return bool(TICKET_CAPTION_RE.match(cap) or NOTE_CAPTION_RE.match(cap) or self._pending("ticket", "note")
                    or self._card_event(msg.reply_to_message))

    def _fmt(self, dt: datetime) -> str:
        return T.fmt_dt(dt, self.tz)

    def _reply_to(self, message_id: int | None) -> dict[str, Any]:
        if not message_id:
            return {}
        return {"reply_parameters": ReplyParameters(message_id=message_id, allow_sending_without_reply=True)}

    async def refresh_card(self, event_id: int) -> None:
        """Redraw the event card in place (booked line, notes)."""
        ev, handle = self._load(event_id)
        if ev is None or not ev.telegram_message_id:
            return
        caption = T.event_caption(ev, handle, self.tz)
        kb = (T.decision_keyboard(ev, teaser=not ev.date_list) if ev.user_decision == "pending"
              else T.answered_keyboard(ev))
        bot = self.app.bot
        try:
            await bot.edit_message_caption(chat_id=self.chat_id, message_id=ev.telegram_message_id,
                                           caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest as exc:
            text = str(exc).lower()
            if "not modified" in text:
                return
            if "no caption" not in text:
                log.warning("card refresh failed for event %s: %s", event_id, exc)
                return
            try:
                await bot.edit_message_text(caption, chat_id=self.chat_id, message_id=ev.telegram_message_id,
                                            parse_mode=ParseMode.HTML, reply_markup=kb,
                                            disable_web_page_preview=True)
            except BadRequest as exc2:
                if "not modified" not in str(exc2).lower():
                    log.warning("card refresh failed for event %s: %s", event_id, exc2)
        except TelegramError as exc:
            log.warning("card refresh failed for event %s: %s", event_id, exc)

    # ---------------------------------------------------------------- buttons
    def card_buttons(self, ev: Event) -> list[InlineKeyboardButton]:
        try:
            row = []
            if ev.booking_method == "eventbrite" and ev.booked_start is None and ev.user_decision != "no":
                row.append(InlineKeyboardButton("🎟 I booked it", callback_data=f"bk:b:{ev.id}"))
            row.append(InlineKeyboardButton("📝 Add note", callback_data=f"nt:{ev.id}"))
            return row
        except Exception:  # noqa: BLE001 — a card must always render
            log.exception("booking buttons failed")
            return []

    def _days_keyboard(self, ev: Event, days: list[date], hhmm: str | None = None) -> InlineKeyboardMarkup:
        if hhmm:  # /booked without a date: pick the day, time already known
            btns = [InlineKeyboardButton(T.fmt_day(d), callback_data=f"bk:t:{ev.id}:{d:%Y%m%d}:{hhmm}")
                    for d in days]
        else:
            btns = [InlineKeyboardButton(T.fmt_day(d), callback_data=f"bk:d:{ev.id}:{d:%Y%m%d}") for d in days]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        rows.append([InlineKeyboardButton("✖ Cancel", callback_data="bk:x")])
        return InlineKeyboardMarkup(rows)

    def _times_keyboard(self, ev: Event, day: date) -> InlineKeyboardMarkup:
        times = slot_times(ev, day)
        btns = [InlineKeyboardButton(t, callback_data=f"bk:t:{ev.id}:{day:%Y%m%d}:{t.replace(':', '')}")
                for t in times]
        rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
        rows.append([InlineKeyboardButton("⌨️ Type it", callback_data=f"bk:k:{ev.id}:{day:%Y%m%d}"),
                     InlineKeyboardButton("✖ Cancel", callback_data="bk:x")])
        return InlineKeyboardMarkup(rows)

    def _booking_days(self, ev: Event) -> list[date]:
        today = self._now().astimezone(self.tz).date()
        days = ev.date_list
        upcoming = [d for d in days if d >= today]
        return upcoming or days

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        chat = update.effective_chat
        if not chat or chat.id != self.chat_id:
            await q.answer()
            return
        data = q.data or ""
        parts = data.split(":")
        try:
            if parts[0] == "nt" and len(parts) == 2 and parts[1].isdigit():
                await self._cb_note(q, int(parts[1]))
            elif data == "bk:x":
                self.pending = None
                await q.answer("Cancelled")
                await self._edit(q, "OK, nothing recorded.")
            elif data == "bk:skip":
                self.pending = None
                await q.answer()
                await self._edit(q, "OK — no ticket. You can send it later with the caption /ticket <id>.")
            elif parts[0] == "bk" and len(parts) == 3 and parts[1] in ("ft", "fn"):
                await self._cb_file(q, parts[1], int(parts[2]))
            elif parts[0] == "bk" and len(parts) >= 3 and parts[2].isdigit():
                await self._cb_booking(q, parts)
            else:
                await q.answer()
        except TelegramError:
            log.exception("booking callback %s failed", data)

    async def _edit(self, q, text: str, kb: InlineKeyboardMarkup | None = None) -> None:  # noqa: ANN001
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            await self.app.bot.send_message(self.chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)

    async def _cb_file(self, q, which: str, event_id: int) -> None:  # noqa: ANN001
        p = self._pending("file")
        if not p or p["event_id"] != event_id or not Path(p["path"]).is_file():
            await q.answer("That photo has expired — send it again.")
            return
        self.pending = None
        await q.answer()
        path = Path(p["path"])
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:
            pass
        if which == "ft":
            await self._save_ticket(q.message, event_id, path)
        else:
            await self._picture_note(q.message, event_id, path, "")

    async def _cb_note(self, q, event_id: int) -> None:  # noqa: ANN001
        ev, _ = self._load(event_id)
        if ev is None:
            await q.answer("That market is no longer tracked.")
            return
        self._ask("note", event_id)
        await q.answer()
        await self.app.bot.send_message(
            self.chat_id, f"📝 What should I remember for <b>{escape(ev.title)}</b>? Send it as your next message "
                          "(text or a photo).",
            parse_mode=ParseMode.HTML, **self._reply_to(ev.telegram_message_id))

    async def _cb_booking(self, q, parts: list[str]) -> None:  # noqa: ANN001
        action, event_id = parts[1], int(parts[2])
        ev, _ = self._load(event_id)
        if ev is None:
            await q.answer("That market is no longer tracked.")
            return
        day = None
        if len(parts) >= 4:
            try:
                day = datetime.strptime(parts[3], "%Y%m%d").date()
            except ValueError:
                await q.answer()
                return
        if action == "b":
            await q.answer()
            days = self._booking_days(ev)
            if not days:
                await self.app.bot.send_message(
                    self.chat_id, f"This market has no date yet — use <code>/booked {ev.id} HH:MM DD/MM</code>.",
                    parse_mode=ParseMode.HTML)
                return
            if len(days) == 1:
                text, kb = self._time_prompt(ev, days[0]), self._times_keyboard(ev, days[0])
            else:
                text, kb = f"🎟 Which day did you book for <b>{escape(ev.title)}</b>?", self._days_keyboard(ev, days)
            await self.app.bot.send_message(self.chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                            **self._reply_to(ev.telegram_message_id))
        elif action == "d" and day:
            await q.answer()
            await self._edit(q, self._time_prompt(ev, day), self._times_keyboard(ev, day))
        elif action == "k" and day:
            self._ask("time", event_id, day=day.isoformat(), message_id=getattr(q.message, "message_id", None))
            await q.answer()
            await self._edit(q, f"⌨️ Type the time of your slot on {T.fmt_day(day)} (e.g. <code>10:30</code>).")
        elif action == "t" and day and len(parts) == 5:
            t = parse_time(parts[4][:2] + ":" + parts[4][2:])
            if t is None:
                await q.answer()
                return
            await q.answer("Booking saved")
            text = await self._record(event_id, day, t)
            await self._edit(q, text, self._skip_kb())
        else:
            await q.answer()

    def _time_prompt(self, ev: Event, day: date) -> str:
        return f"🎟 What time is your slot for <b>{escape(ev.title)}</b> on {T.fmt_day(day)}?"

    @staticmethod
    def _skip_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="bk:skip")]])

    async def _record(self, event_id: int, day: date, t: time) -> str:
        start = datetime.combine(day, t, tzinfo=self.tz)
        ev = await bookings.record_booking(self.db, event_id, start, None, via="manual")
        self._ask("ticket", event_id)
        title = escape(ev.title) if ev else f"#{event_id}"
        return (f"🎟 Booked <b>{title}</b> — {self._fmt(start)}. I've updated your calendar.\n\n"
                "Send me the QR or ticket PDF (optional).")

    # --------------------------------------------------------------- commands
    async def cmd_booked(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        args = list(ctx.args or [])
        event_id = self._event_id(args[0] if args else None)
        t = parse_time(args[1]) if len(args) > 1 else None
        if event_id is None or t is None:
            await msg.reply_html("Use <code>/booked &lt;event id&gt; &lt;HH:MM&gt; [date]</code>, e.g. "
                                 "<code>/booked 12 10:30 13/11</code>. Event ids are in /events.")
            return
        ev, _ = self._load(event_id)
        if ev is None:
            await msg.reply_text(f"I don't know event #{event_id}. See /events.")
            return
        today = self._now().astimezone(self.tz).date()
        if len(args) > 2:
            day = parse_day(" ".join(args[2:]), ev.date_list, today)
            if day is None:
                await msg.reply_html("I couldn't read that date. Try <code>13/11</code> or <code>2026-11-13</code>.")
                return
        else:
            days = self._booking_days(ev)
            if not days:
                await msg.reply_html(f"#{event_id} has no date yet — add it: <code>/booked {event_id} "
                                     f"{t:%H:%M} DD/MM</code>.")
                return
            if len(days) > 1:
                await msg.reply_html(f"🎟 Which day is your {t:%H:%M} slot for <b>{escape(ev.title)}</b>?",
                                     reply_markup=self._days_keyboard(ev, days, f"{t:%H%M}"))
                return
            day = days[0]
        text = await self._record(event_id, day, t)
        await msg.reply_html(text, reply_markup=self._skip_kb())

    async def cmd_unbook(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        event_id = self._event_id((ctx.args or [None])[0])
        if event_id is None:
            await msg.reply_html("Use <code>/unbook &lt;event id&gt;</code>.")
            return
        ev, _ = self._load(event_id)
        if ev is None or ev.booked_start is None:
            await msg.reply_text(f"#{event_id} has no booking recorded." if ev else f"I don't know event #{event_id}.")
            return
        await bookings.clear_booking(self.db, event_id)
        await self._unpin(event_id)
        cal = self.app.bot_data.get("calendar") if hasattr(self.app, "bot_data") else None
        if cal is not None:
            cal._spawn(cal.unbooked(event_id))
        await self.refresh_card(event_id)
        self.schedule_reminders()
        await msg.reply_html(f"Booking cleared for <b>{escape(ev.title)}</b>"
                             + (" — the calendar shows all dates again." if cal is not None else "."))

    async def cmd_ticket(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        event_id = self._event_id((ctx.args or [None])[0])
        if event_id is None or self._load(event_id)[0] is None:
            await update.effective_message.reply_html(
                "Send the QR photo or ticket PDF with the caption <code>/ticket &lt;event id&gt;</code>.")
            return
        self._ask("ticket", event_id)
        await update.effective_message.reply_text(f"OK — send me the QR or ticket PDF for #{event_id}.")

    async def cmd_note(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        args = list(ctx.args or [])
        event_id = self._event_id(args[0] if args else None)
        if event_id is None or len(args) < 2:
            await msg.reply_html("Use <code>/note &lt;event id&gt; &lt;text&gt;</code>, or reply to an event card "
                                 "with the text.")
            return
        await self._add_note(msg, event_id, " ".join(args[1:]))

    async def _add_note(self, msg, event_id: int, text: str) -> None:  # noqa: ANN001
        ev = await bookings.add_note(self.db, event_id, text)
        if ev is None:
            await msg.reply_text(f"I don't know event #{event_id}." if text.strip() else "That note is empty.")
            return
        n = len(ev.note_list)
        await msg.reply_html(f"📝 Noted for <b>{escape(ev.title)}</b> ({n} note{'s' if n != 1 else ''}). "
                             "I'll remind you on the day.")

    async def cmd_notes(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        event_id = self._event_id((ctx.args or [None])[0])
        if event_id is None:
            await msg.reply_html("Use <code>/notes &lt;event id&gt;</code>.")
            return
        ev, _ = self._load(event_id)
        if ev is None:
            await msg.reply_text(f"I don't know event #{event_id}.")
            return
        notes = ev.note_list
        if not notes:
            await msg.reply_html(f"No notes for <b>{escape(ev.title)}</b> yet. Add one with "
                                 f"<code>/note {event_id} …</code>.")
            return
        lines = [f"📝 <b>{escape(ev.title)}</b>"]
        lines += [f"{i}. {escape(n.get('text', ''))}" + (" 📷" if n.get("image") else "")
                  for i, n in enumerate(notes, 1)]
        lines.append(f"\n<i>Delete one with /delnote {event_id} &lt;n&gt;</i>")
        await msg.reply_html("\n".join(lines))

    async def cmd_delnote(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        args = list(ctx.args or [])
        event_id = self._event_id(args[0] if args else None)
        if event_id is None or len(args) < 2 or not args[1].isdigit():
            await msg.reply_html("Use <code>/delnote &lt;event id&gt; &lt;n&gt;</code> (numbers from /notes).")
            return
        gone = await bookings.delete_note(self.db, event_id, int(args[1]))
        if gone is None:
            await msg.reply_text("No such note. See /notes " + str(event_id))
            return
        await msg.reply_html(f"Deleted: <i>{escape(gone)}</i>")

    # ------------------------------------------------------------ free input
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        text = (msg.text or "").strip()
        p = self._pending("note", "time")
        if p and p["kind"] == "note":
            self.pending = None
            await self._add_note(msg, p["event_id"], text)
            return
        if p and p["kind"] == "time":
            t = parse_time(text)
            if t is None:
                await msg.reply_html("Send the time like <code>10:30</code> (or tap Cancel).")
                return
            self.pending = None
            reply = await self._record(p["event_id"], date.fromisoformat(p["day"]), t)
            await msg.reply_html(reply, reply_markup=self._skip_kb())
            return
        event_id = self._card_event(msg.reply_to_message)
        if event_id is not None:
            await self._add_note(msg, event_id, text)

    async def on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A photo/PDF is a ticket (caption /ticket N, after /ticket, a PDF on a card) or a picture note
        (caption /note N …, a captioned photo on a card, after "📝 Add note"). An uncaptioned photo on a
        card could be either, so it asks."""
        msg = update.effective_message
        cap = (msg.caption or "").strip()
        tm, nm = TICKET_CAPTION_RE.match(cap), NOTE_CAPTION_RE.match(cap)
        p_ticket, p_note = self._pending("ticket"), self._pending("note")
        card = self._card_event(msg.reply_to_message)
        if tm:
            kind, event_id = "ticket", int(tm.group(1))
        elif nm:
            kind, event_id, cap = "note", int(nm.group(1)), nm.group(2).strip()
        elif p_ticket:
            kind, event_id = "ticket", p_ticket["event_id"]
        elif p_note:
            kind, event_id = "note", p_note["event_id"]
        elif card is not None and msg.document and not (msg.document.mime_type or "").startswith("image/"):
            kind, event_id = "ticket", card
        elif card is not None and cap and not cap.startswith("/"):
            kind, event_id = "note", card
        elif card is not None:
            kind, event_id = "ask", card
        else:
            return
        ev, _ = self._load(event_id)
        if ev is None:
            await msg.reply_text(f"I don't know event #{event_id}.")
            return
        got = await self._download(msg, event_id, kind)
        if got is None:
            return
        self.pending = None
        if kind == "ask":
            self._ask("file", event_id, path=str(got))
            await msg.reply_html(f"Is this the ticket for <b>{escape(ev.title)}</b>, or a photo to keep as a note?",
                                 reply_markup=InlineKeyboardMarkup([[
                                     InlineKeyboardButton("🎟 It's the ticket", callback_data=f"bk:ft:{event_id}"),
                                     InlineKeyboardButton("📝 Keep as a note", callback_data=f"bk:fn:{event_id}")]]))
            return
        if kind == "note":
            await self._picture_note(msg, event_id, got, cap)
            return
        await self._save_ticket(msg, event_id, got)

    async def _download(self, msg, event_id: int, kind: str) -> Path | None:  # noqa: ANN001
        if msg.photo:
            tg_file, ext = await msg.photo[-1].get_file(), ".jpg"
        elif msg.document:
            doc = msg.document
            ext = Path(doc.file_name or "").suffix.lower() or (
                ".pdf" if doc.mime_type == "application/pdf" else ".png" if (doc.mime_type or "").endswith("png")
                else ".jpg" if (doc.mime_type or "").startswith("image/") else ".bin")
            tg_file = await doc.get_file()
        else:
            return None
        if kind == "ticket":
            path = bookings.ticket_dir(self.settings.data_dir) / f"{event_id}{ext}"
        else:
            path = bookings.note_dir(self.settings.data_dir) / f"{event_id}_{msg.message_id}{ext}"
        await tg_file.download_to_drive(path)
        return path

    def _attach_word(self) -> str:
        cal = self.app.bot_data.get("calendar") if self.app else None
        return "attached to your calendar entry" if cal is not None and getattr(cal, "can_attach", False) \
            else "pinned in this chat"

    async def _save_ticket(self, msg, event_id: int, path: Path) -> None:  # noqa: ANN001
        ev, _ = self._load(event_id)
        target = bookings.ticket_dir(self.settings.data_dir) / f"{event_id}{path.suffix}"
        if path != target:
            path.replace(target)
        await bookings.attach_ticket(self.db, event_id, target)
        tail = "" if ev.booked_start else (f"\nIt isn't marked as booked yet — "
                                           f"<code>/booked {event_id} HH:MM</code> to add the slot.")
        await msg.reply_html(f"🎟 Ticket saved for <b>{escape(ev.title)}</b> and {self._attach_word()}.{tail}")

    async def _picture_note(self, msg, event_id: int, path: Path, text: str) -> None:  # noqa: ANN001
        ev = await bookings.add_note(self.db, event_id, text or "📷 photo", image=path)
        if ev is None:
            return
        where = "your calendar entry" if ev.user_decision == "yes" else "the calendar once you mark it ✅"
        await msg.reply_html(f"📝 Photo noted for <b>{escape(ev.title)}</b> — it goes on {where}.")

    # ------------------------------------------------------------------ hooks
    def _calendar_attaches(self) -> bool:
        """The calendar sync attaches the ticket (and pins it itself only if that upload fails)."""
        cal = self.app.bot_data.get("calendar") if self.app is not None else None
        return cal is not None and getattr(cal, "can_attach", False)

    async def on_booking(self, event_id: int, **_: Any) -> None:
        ev, _h = self._load(event_id)
        if ev is not None and ev.ticket_path and not self._calendar_attaches():
            await self.pin_ticket(event_id, ev.ticket_path)
        await self.refresh_card(event_id)
        self.schedule_reminders()

    async def on_ticket(self, event_id: int, path: str | None = None, **_: Any) -> None:
        if path and not self._calendar_attaches():
            await self.pin_ticket(event_id, path)
        await self.refresh_card(event_id)

    async def on_note(self, event_id: int, **_: Any) -> None:
        await self.refresh_card(event_id)
        self.schedule_reminders()

    # ------------------------------------------------------------ ticket pin
    def _pin_state(self, event_id: int) -> dict[str, Any]:
        try:
            return json.loads(self.db.get_setting(PIN_KEY.format(event_id), "{}") or "{}")
        except ValueError:
            return {}

    async def pin_ticket(self, event_id: int, path: str | Path) -> int | None:
        """Send the ticket as a reply to the event card and pin it (once per file)."""
        path = Path(path)
        if not path.is_file():
            log.warning("ticket file for event %s missing: %s", event_id, path)
            return None
        state = self._pin_state(event_id)
        if state.get("path") == str(path) and state.get("mtime") == path.stat().st_mtime:
            return state.get("msg")
        ev, _ = self._load(event_id)
        if ev is None:
            return None
        when = f" — {self._fmt(ev.booked_start)}" if ev.booked_start else ""
        caption = f"🎟 Ticket: {ev.title}{when}"[:1000]
        bot = self.app.bot
        try:
            with open(path, "rb") as f:
                if path.suffix.lower() in PHOTO_EXT:
                    sent = await bot.send_photo(self.chat_id, f, caption=caption, **self._reply_to(ev.telegram_message_id))
                else:
                    sent = await bot.send_document(self.chat_id, f, caption=caption, filename=path.name,
                                                   **self._reply_to(ev.telegram_message_id))
        except TelegramError:
            log.exception("Couldn't send ticket for event %s", event_id)
            return None
        if state.get("msg"):
            try:
                await bot.unpin_chat_message(self.chat_id, message_id=state["msg"])
            except TelegramError:
                pass
        try:
            await bot.pin_chat_message(self.chat_id, sent.message_id, disable_notification=True)
        except TelegramError:
            log.warning("Couldn't pin ticket for event %s", event_id, exc_info=True)
        self.db.set_setting(PIN_KEY.format(event_id), json.dumps(
            {"path": str(path), "mtime": path.stat().st_mtime, "msg": sent.message_id}))
        log.info("ticket pinned event=%s message=%s", event_id, sent.message_id)
        return sent.message_id

    async def _unpin(self, event_id: int) -> None:
        state = self._pin_state(event_id)
        if state.get("msg"):
            try:
                await self.app.bot.unpin_chat_message(self.chat_id, message_id=state["msg"])
            except TelegramError:
                pass
        self.db.set_setting(PIN_KEY.format(event_id), "{}")

    # -------------------------------------------------------------- reminders
    def _sent(self) -> list[str]:
        try:
            return json.loads(self.db.get_setting(SENT_KEY, "[]") or "[]")
        except ValueError:
            return []

    def _mark_sent(self, key: str) -> None:
        sent = self._sent()
        if key not in sent:
            sent.append(key)
        self.db.set_setting(SENT_KEY, json.dumps(sent[-300:]))

    def schedule_reminders(self, catch_up: bool = False) -> list[str]:
        """(Re)schedule one run_once job per event whose market-day reminder is due in the next 36 h.
        Returns the job names scheduled. Safe to call often: jobs are keyed by event and due time."""
        jq = getattr(self.app, "job_queue", None)
        if jq is None:
            return []
        now = self._now()
        today = now.astimezone(self.tz).date()
        sent = set(self._sent())
        with self.db.session() as s:
            evs = list(s.scalars(select(Event).where(Event.status != "past")))
        wanted: dict[str, tuple[datetime, int]] = {}
        for ev in evs:
            due = reminder_due(ev, self.tz, today)
            if due is None:
                continue
            key = f"{ev.id}:{due.astimezone(self.tz):%Y-%m-%d}"
            if key in sent:
                continue
            if due > now and due - now <= timedelta(hours=36):
                wanted[f"note-rem:{ev.id}:{due.astimezone(timezone.utc):%Y%m%dT%H%M}"] = (due, ev.id)
            elif catch_up and due <= now < due + timedelta(hours=3):
                wanted[f"note-rem:{ev.id}:{due.astimezone(timezone.utc):%Y%m%dT%H%M}"] = (now + timedelta(seconds=2), ev.id)
        existing = {j.name: j for j in jq.jobs() if (j.name or "").startswith("note-rem:")}
        for name, job in existing.items():
            if name not in wanted:
                job.schedule_removal()
        out = []
        for name, (when, ev_id) in wanted.items():
            if name not in existing:
                jq.run_once(self._reminder_job, when=when, name=name, data=ev_id)
            out.append(name)
        return out

    async def _reminder_job(self, ctx) -> None:  # noqa: ANN001
        await self.send_reminder(ctx.job.data)

    def reminder_text(self, ev: Event) -> tuple[str, InlineKeyboardMarkup | None]:
        lines = [f"⏰ <b>Today: {escape(ev.title)}</b>"]
        if ev.booked_start:
            lines.append(f"🎟 Your slot: {ev.booked_start.astimezone(self.tz):%H:%M}"
                         + (" (ticket pinned in this chat)" if ev.ticket_path else ""))
        where = ev.address or ev.area
        kb = None
        if where:
            url = maps_url(where)
            lines.append(f"📍 {escape(where)}")
            lines.append(f'🗺 <a href="{escape(url)}">Google Maps</a>')
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗺 Open in Maps", url=url)]])
        notes = T.notes_lines(ev)
        if notes:
            lines.append("")
            lines += notes
        return "\n".join(lines), kb

    async def send_reminder(self, event_id: int) -> bool:
        ev, _ = self._load(event_id)
        if ev is None:
            return False
        today = self._now().astimezone(self.tz).date()
        due = reminder_due(ev, self.tz, today)
        if due is None:
            return False
        key = f"{ev.id}:{due.astimezone(self.tz):%Y-%m-%d}"
        if key in self._sent():
            return False
        text, kb = self.reminder_text(ev)
        try:
            await self.app.bot.send_message(self.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                            disable_web_page_preview=True, **self._reply_to(ev.telegram_message_id))
        except TelegramError:
            log.exception("Couldn't send market-day reminder for event %s", event_id)
            return False
        self._mark_sent(key)
        log.info("market-day reminder sent event=%s", event_id)
        return True


def register_booking_cmds(app: Application, settings, db: Database, notifier=None) -> BookingCommands:  # noqa: ANN001
    cmds = BookingCommands(app, settings, db, notifier)
    cmds.register()
    app.bot_data["booking_cmds"] = cmds
    return cmds

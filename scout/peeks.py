"""Sneak peeks: photos of the pieces an account will sell (usually stories), sent as albums.

The pipeline stores a Peek for every image Gemini says shows pieces for sale (items_for_sale), linked
to the account's upcoming market. Every couple of minutes this module sends what's new, once the
account has gone quiet for a few minutes (a run of 30 stories becomes one album, not 30 messages):
up to 10 photos per album, numbered, each captioned with what Gemini saw, then one message of
⭐ buttons. Tapping ⭐ 3 adds a note with that photo to the market ("⭐ rattan armchair (photo 3)"),
which the card, the morning briefing and the calendar entry pick up; tapping again removes it.
Replying to a photo with text adds that text as a note with the photo. More than 10 waiting?
"👀 Show more" sends the next 10, so a big batch never floods the chat.

Markets you answered ❌ get no peeks. Photos that arrive before the account announces a date wait
until it does (event_created adopts them).
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.ext.filters import MessageFilter

from . import bookings, hooks
from .db import Account, Database, Event, Peek, utcnow

log = logging.getLogger(__name__)

ALBUM = 10  # Telegram's media-group limit
QUIET = timedelta(minutes=3)
UPLOAD_TIMEOUT = 60  # seconds; photo uploads from the Pi can be slow  # wait until the account stops posting before sending
ADOPT_DAYS = 21  # photos posted before the market was announced


class _ReplyToPeek(MessageFilter):
    def __init__(self, peeks: "Peeks"):
        super().__init__(name="reply_to_peek")
        self.peeks = peeks

    def filter(self, message: Message) -> bool:
        r = message.reply_to_message
        return bool(r and self.peeks.peek_for_message(r.message_id))


def _caption(n: int, items: list[str]) -> str:
    return f"{n} · {', '.join(items[:3])}" if items else str(n)


class Peeks:
    def __init__(self, app: Application, settings, db: Database, now=None):  # noqa: ANN001
        self.app = app
        self.settings = settings
        self.db = db
        self.chat_id = settings.allowed_chat_id
        self.now = now or utcnow

    # ============================================================ registration
    def register(self) -> None:
        hooks.on("event_created", self.on_event_created)
        hooks.on("decision_changed", self.on_decision_changed)
        only_me = filters.Chat(chat_id=self.chat_id)
        self.app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^pk:"))
        reply = MessageHandler(only_me & filters.TEXT & ~filters.COMMAND & _ReplyToPeek(self), self.on_reply)
        self.app.add_handler(reply, group=0)
        group = self.app.handlers[0]  # ahead of the core "I didn't get that" text handler
        group.remove(reply)
        group.insert(0, reply)
        if self.app.job_queue is not None:
            self.app.job_queue.run_repeating(self.tick, interval=90, first=60, name="peeks")

    # ================================================================ helpers
    def peek_for_message(self, message_id: int) -> Peek | None:
        with self.db.session() as s:
            return s.scalar(select(Peek).where(Peek.telegram_message_id == message_id))

    def _event(self, event_id: int) -> tuple[Event | None, str]:
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            acc = s.get(Account, ev.account_id) if ev else None
            return ev, acc.handle if acc else "?"

    def _paused(self) -> bool:
        return self.db.get_setting("paused") == "1"

    # ================================================================ sending
    async def tick(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> int:
        """Send new peeks of every market whose account has been quiet for QUIET. Returns photos sent."""
        if self._paused():
            return 0
        now = self.now()
        with self.db.session() as s:
            rows = list(s.scalars(select(Peek).where(Peek.state == "new", Peek.event_id.is_not(None))
                                  .order_by(Peek.created_at, Peek.id)))
        by_event: dict[int, list[Peek]] = {}
        for p in rows:
            by_event.setdefault(p.event_id, []).append(p)
        sent = 0
        for event_id, peeks in by_event.items():
            if now - max(p.created_at for p in peeks) < QUIET:
                continue  # still posting: wait for the rest of the run
            ev, _ = self._event(event_id)
            if ev is None or ev.user_decision in ("no", "filtered"):
                self._set_state([p.id for p in peeks], "skipped")
                continue
            first, rest = peeks[:ALBUM], peeks[ALBUM:]
            self._set_state([p.id for p in rest], "held")
            sent += await self.send_album(event_id, first)
        return sent

    def _set_state(self, ids: list[int], state: str) -> None:
        if not ids:
            return
        with self.db.session() as s:
            for p in s.scalars(select(Peek).where(Peek.id.in_(ids))):
                p.state = state

    def _held(self, event_id: int) -> list[Peek]:
        with self.db.session() as s:
            return list(s.scalars(select(Peek).where(Peek.event_id == event_id, Peek.state == "held")
                                  .order_by(Peek.created_at, Peek.id)))

    def _latest_keyboard(self, event_id: int) -> int | None:
        with self.db.session() as s:
            ids = [i for i in s.scalars(select(Peek.keyboard_message_id).where(Peek.event_id == event_id)) if i]
        return max(ids) if ids else None

    def keyboard(self, peeks: list[Peek], event_id: int, kb_id: int | None = None) -> InlineKeyboardMarkup:
        """⭐ buttons for one album; only the newest album carries "Show more"."""
        buttons = [InlineKeyboardButton(f"{'✅' if p.starred else '⭐'} {p.number}", callback_data=f"pk:s:{p.id}")
                   for p in sorted(peeks, key=lambda p: p.number or 0)]
        rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        latest = self._latest_keyboard(event_id)
        held = len(self._held(event_id)) if kb_id is None or latest is None or kb_id >= latest else 0
        if held:
            rows.append([InlineKeyboardButton(f"👀 Show {min(ALBUM, held)} more ({held} waiting)",
                                              callback_data=f"pk:m:{event_id}")])
        return InlineKeyboardMarkup(rows)

    async def send_album(self, event_id: int, peeks: list[Peek]) -> int:
        ev, handle = self._event(event_id)
        if ev is None:
            return 0
        usable = [p for p in peeks if Path(p.image_path).is_file() and Path(p.image_path).stat().st_size > 0]
        self._set_state([p.id for p in peeks if p not in usable], "sent")  # image pruned: nothing to show
        if not usable:
            return 0
        reply = ({"reply_parameters": ReplyParameters(message_id=ev.telegram_message_id,
                                                      allow_sending_without_reply=True)}
                 if ev.telegram_message_id else {})
        bot = self.app.bot
        try:
            if len(usable) == 1:
                p = usable[0]
                msgs = [await bot.send_photo(self.chat_id, Path(p.image_path).read_bytes(),
                                             caption=_caption(1, p.item_list), disable_notification=True,
                                             write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT, **reply)]
            else:
                media = [InputMediaPhoto(Path(p.image_path).read_bytes(), caption=_caption(i, p.item_list))
                         for i, p in enumerate(usable, 1)]
                msgs = list(await bot.send_media_group(self.chat_id, media, disable_notification=True,
                                                       write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT,
                                                       **reply))
        except BadRequest as exc:
            # A broken image would fail forever: send the album without it next time.
            log.warning("Sneak peek album for event %s refused (%s); dropping it", event_id, exc)
            self._set_state([p.id for p in usable], "sent")
            return 0
        except TimedOut:
            # Slow upload from the Pi: the photos stay queued and the next round (90 s) sends them.
            log.warning("Sneak peek upload for event %s timed out; retrying next round", event_id)
            return 0
        except TelegramError:
            log.exception("Couldn't send sneak peek album for event %s", event_id)
            return 0
        with self.db.session() as s:
            for i, (p, m) in enumerate(zip(usable, msgs), 1):
                row = s.get(Peek, p.id)
                row.number, row.telegram_message_id, row.state = i, m.message_id, "sent"
        fresh = self._album(msgs[0].message_id, [p.id for p in usable])
        n = len(usable)
        text = (f"👀 <b>Sneak peek</b> · {escape(ev.title)} (@{escape(handle)})\n"
                f"{n} photo{'s' * (n > 1)} of what's for sale. Tap ⭐ on what you like: it goes into the "
                "market's notes and your calendar. Or reply to a photo with a note.")
        try:
            kb_msg = await bot.send_message(self.chat_id, text, parse_mode=ParseMode.HTML,
                                            reply_markup=self.keyboard(fresh, event_id), disable_notification=True)
        except TelegramError:
            log.exception("Couldn't send sneak peek buttons for event %s", event_id)
            return n
        with self.db.session() as s:
            for p in s.scalars(select(Peek).where(Peek.id.in_([p.id for p in usable]))):
                p.keyboard_message_id = kb_msg.message_id
        log.info("sneak peek sent event=%s photos=%d held=%d", event_id, n, len(self._held(event_id)))
        return n

    def _album(self, _first_msg: int, ids: list[int]) -> list[Peek]:
        with self.db.session() as s:
            return list(s.scalars(select(Peek).where(Peek.id.in_(ids))))

    def _same_keyboard(self, peek: Peek) -> list[Peek]:
        with self.db.session() as s:
            return list(s.scalars(select(Peek).where(Peek.keyboard_message_id == peek.keyboard_message_id)))

    async def _redraw(self, peek: Peek) -> None:
        if not peek.keyboard_message_id or peek.event_id is None:
            return
        try:
            await self.app.bot.edit_message_reply_markup(self.chat_id, peek.keyboard_message_id,
                                                         reply_markup=self.keyboard(self._same_keyboard(peek),
                                                                                    peek.event_id,
                                                                                    peek.keyboard_message_id))
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.info("Couldn't redraw peek buttons: %s", exc)
        except TelegramError as exc:
            log.info("Couldn't redraw peek buttons: %s", exc)

    # ================================================================ starring
    def _copy_image(self, peek: Peek) -> Path | None:
        src = Path(peek.image_path)
        if not src.is_file():
            return None
        dst = bookings.note_dir(self.settings.data_dir) / f"peek_{peek.id}{src.suffix or '.jpg'}"
        shutil.copyfile(src, dst)
        return dst

    def _note_text(self, peek: Peek, text: str | None = None) -> str:
        if text:
            return text
        items = peek.item_list
        what = items[0] if items else "a piece from the sneak peek"
        return f"⭐ {what} (photo {peek.number})"

    async def star(self, peek_id: int, text: str | None = None) -> tuple[Event | None, bool]:
        """Toggle (text=None) or set with a note text (reply). Returns (event, starred now)."""
        with self.db.session() as s:
            peek = s.get(Peek, peek_id)
        if peek is None or peek.event_id is None:
            return None, False
        if peek.starred and text is None:
            await bookings.remove_notes(self.db, peek.event_id, peek=peek.id)
            starred = False
        else:
            if peek.starred:
                await bookings.remove_notes(self.db, peek.event_id, peek=peek.id)
            img = self._copy_image(peek)
            await bookings.add_note(self.db, peek.event_id, self._note_text(peek, text), image=img,
                                    peek=peek.id, photo=peek.url)
            starred = True
        with self.db.session() as s:
            row = s.get(Peek, peek_id)
            row.starred = starred
            peek = row
        await self._redraw(peek)
        ev, _ = self._event(peek.event_id)
        return ev, starred

    async def send_more(self, event_id: int) -> int:
        held = self._held(event_id)
        if not held:
            return 0
        return await self.send_album(event_id, held[:ALBUM])

    # ================================================================ telegram
    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        try:
            if not (update.effective_chat and update.effective_chat.id == self.chat_id):
                await q.answer()
                return
            _, action, raw = (q.data or "").split(":")
            if action == "s":
                ev, starred = await self.star(int(raw))
                if ev is None:
                    await q.answer("That market is no longer tracked.")
                elif not starred:
                    await q.answer("Removed from your notes")
                elif ev.user_decision == "yes":
                    await q.answer("⭐ Added to the notes and your calendar")
                else:
                    await q.answer("⭐ Added to the notes — it reaches the calendar when you mark the market ✅")
            elif action == "m":
                await q.answer("Sending more…")
                if await self.send_more(int(raw)):
                    # the old "Show more" button goes away: it now lives under the new album
                    rows = [r for r in (q.message.reply_markup.inline_keyboard if q.message.reply_markup else [])
                            if not any((b.callback_data or "").startswith("pk:m:") for b in r)]
                    try:
                        await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
                    except TelegramError:
                        pass
            else:
                await q.answer()
        except Exception:  # noqa: BLE001
            log.exception("Peek callback %r failed", getattr(q, "data", None))
            try:
                await q.answer("Something went wrong")
            except Exception:  # noqa: BLE001
                pass

    async def on_reply(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        peek = self.peek_for_message(msg.reply_to_message.message_id)
        if peek is None:
            return
        ev, _ = await self.star(peek.id, text=(msg.text or "").strip())
        if ev is not None:
            tail = "" if ev.user_decision == "yes" else " It reaches the calendar when you mark the market ✅."
            await msg.reply_html(f"📝 Noted for <b>{escape(ev.title)}</b>, with that photo.{tail}")

    # ================================================================== hooks
    async def on_event_created(self, event_id: int, **_: Any) -> None:
        """Photos posted before the market was announced join it now."""
        ev, _ = self._event(event_id)
        if ev is None or ev.user_decision in ("no", "filtered"):
            return
        since = self.now() - timedelta(days=ADOPT_DAYS)
        with self.db.session() as s:
            orphans = list(s.scalars(select(Peek).where(Peek.account_id == ev.account_id, Peek.event_id.is_(None),
                                                        Peek.created_at >= since)))
            for p in orphans:
                p.event_id = event_id
        if orphans:
            log.info("sneak peek: %d earlier photos joined event %s", len(orphans), event_id)

    async def on_decision_changed(self, event_id: int, old: str = "", new: str = "", **_: Any) -> None:
        if new == "no":
            with self.db.session() as s:
                for p in s.scalars(select(Peek).where(Peek.event_id == event_id, Peek.state.in_(("new", "held")))):
                    p.state = "skipped"


def setup_peeks(app: Application, settings, db: Database) -> Peeks:  # noqa: ANN001
    peeks = Peeks(app, settings, db)
    peeks.register()
    app.bot_data["peeks"] = peeks
    return peeks

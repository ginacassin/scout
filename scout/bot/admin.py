"""Duplicate-market tools for the chat: /merge, and keeping cards in step with merges and updates.

- "event_updated" for a pending (unanswered) event edits its card in place instead of sending a new one.
- "events_merged" turns the duplicate's card into a "merged" note (its buttons would act on a dead
  event) and refreshes the kept card if it is still pending.
- /merge <keep_id> <dup_id> asks for confirmation (mg: buttons) and folds one event into the other.
"""

from __future__ import annotations

import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from .. import hooks
from ..db import Account, Database, Event
from ..dedupe import merge_events
from . import templates as T

log = logging.getLogger(__name__)


class AdminTools:
    def __init__(self, db: Database, bot_getter, chat_id: int, tz):  # noqa: ANN001
        self.db = db
        self.bot = bot_getter  # callable -> telegram Bot (lazy: app.bot exists only after build)
        self.chat_id = chat_id
        self.tz = tz

    def register_hooks(self) -> None:
        hooks.on("event_updated", self.on_event_updated)
        hooks.on("events_merged", self.on_events_merged)

    # ------------------------------------------------------------ card edits
    def _load(self, event_id: int) -> tuple[Event | None, str]:
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            if ev is None:
                return None, "?"
            acc = s.get(Account, ev.account_id)
            return ev, acc.handle if acc else "?"

    async def _edit(self, message_id: int, text: str, kb: InlineKeyboardMarkup | None) -> bool:
        bot = self.bot()
        try:
            await bot.edit_message_caption(chat_id=self.chat_id, message_id=message_id, caption=text,
                                           parse_mode=ParseMode.HTML, reply_markup=kb)
            return True
        except BadRequest as exc:
            msg = str(exc).lower()
            if "not modified" in msg:
                return True
            if "no caption" not in msg:
                log.warning("card edit failed (message %s): %s", message_id, exc)
                return False
        except TelegramError as exc:
            log.warning("card edit failed (message %s): %s", message_id, exc)
            return False
        try:  # a text card (no photo)
            await bot.edit_message_text(text, chat_id=self.chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
                                        reply_markup=kb, disable_web_page_preview=True)
            return True
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            log.warning("card text edit failed (message %s): %s", message_id, exc)
        except TelegramError as exc:
            log.warning("card text edit failed (message %s): %s", message_id, exc)
        return False

    async def refresh_pending_card(self, event_id: int, header: str | None) -> bool:
        ev, handle = self._load(event_id)
        if ev is None or ev.user_decision != "pending" or not ev.telegram_message_id or ev.status == "past":
            return False
        caption = T.event_caption(ev, handle, self.tz, header=header)
        kb = T.decision_keyboard(ev, teaser=not ev.date_list)
        ok = await self._edit(ev.telegram_message_id, caption, kb)
        if ok:
            log.info("card refreshed in place event_id=%s", event_id)
        return ok

    async def on_event_updated(self, event_id: int, changes: list[str] | None = None, **_) -> None:  # noqa: ANN003
        header = "🔄 Updated: " + "; ".join(changes) if changes else "🔄 Updated"
        await self.refresh_pending_card(event_id, header)

    async def on_events_merged(self, event_id: int, merged_id: int | None = None, **_) -> None:  # noqa: ANN003
        if merged_id is not None:
            dup, handle = self._load(merged_id)
            keep, _h = self._load(event_id)
            if dup is not None and dup.telegram_message_id and dup.telegram_message_id != (keep and keep.telegram_message_id):
                title = keep.title if keep else f"#{event_id}"
                text = (f"🔁 <b>Same market as {escape(title)}</b> (#{event_id}) — merged, follow that card.\n\n"
                        f"<s>{escape(dup.title)}</s> <i>@{escape(handle)}</i>")
                await self._edit(dup.telegram_message_id, text, None)
        await self.refresh_pending_card(event_id, None)

    # ---------------------------------------------------------------- /merge
    async def cmd_merge(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        if len(args) != 2 or not all(a.lstrip("#").isdigit() for a in args):
            await update.effective_message.reply_text(
                "Usage: /merge <keep_id> <duplicate_id>\nThe ids are the #numbers in /events.")
            return
        keep_id, dup_id = (int(a.lstrip("#")) for a in args)
        problem, keep, dup, hk, hd = self._check_pair(keep_id, dup_id)
        if problem:
            await update.effective_message.reply_text(problem)
            return
        text = (
            "Merge these two markets?\n\n"
            f"Keep #{keep.id}:\n{T.short_line(keep, hk)}\n\n"
            f"Fold in #{dup.id}:\n{T.short_line(dup, hd)}\n\n"
            f"#{dup.id}'s answer, notes, booking and address details move to #{keep.id}; #{dup.id} disappears."
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Merge", callback_data=f"mg:ok:{keep.id}:{dup.id}"),
            InlineKeyboardButton("✖️ Cancel", callback_data="mg:x"),
        ]])
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                                  disable_web_page_preview=True)

    def _check_pair(self, keep_id: int, dup_id: int):  # noqa: ANN202
        if keep_id == dup_id:
            return "Those are the same market.", None, None, "", ""
        with self.db.session() as s:
            keep, dup = s.get(Event, keep_id), s.get(Event, dup_id)
            for ev, n in ((keep, keep_id), (dup, dup_id)):
                if ev is None:
                    return f"No market #{n}.", None, None, "", ""
                if ev.status == "past" and ev.user_decision == "filtered":
                    return f"#{n} was already merged or filtered out.", None, None, "", ""
            hk = (s.get(Account, keep.account_id) or Account(handle="?")).handle
            hd = (s.get(Account, dup.account_id) or Account(handle="?")).handle
            return None, keep, dup, hk, hd

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if update.effective_chat is None or update.effective_chat.id != self.chat_id:
            await q.answer()
            return
        data = q.data or ""
        if data == "mg:x":
            await q.answer("Cancelled")
            await q.edit_message_text("Merge cancelled.")
            return
        parts = data.split(":")
        if len(parts) != 4 or parts[1] != "ok" or not (parts[2].isdigit() and parts[3].isdigit()):
            await q.answer()
            return
        keep_id, dup_id = int(parts[2]), int(parts[3])
        problem, *_ = self._check_pair(keep_id, dup_id)
        if problem:
            await q.answer(problem[:190])
            await q.edit_message_text(problem)
            return
        with self.db.session() as s:
            keep = s.get(Event, keep_id)
            merge_events(s, keep, s.get(Event, dup_id))
            keep.log_change(f"merged #{dup_id} by hand")
            title = keep.title
        log.info("manual merge keep=%s dup=%s", keep_id, dup_id)
        await q.answer("Merged")
        await hooks.emit("events_merged", keep_id, merged_id=dup_id)
        await q.edit_message_text(f"✅ Merged #{dup_id} into #{keep_id} — {escape(title)}", parse_mode=ParseMode.HTML)


def register_admin(app: Application, settings, db: Database, notifier=None) -> AdminTools:  # noqa: ANN001
    tools = AdminTools(db, lambda: app.bot, settings.allowed_chat_id, settings.tz)
    tools.register_hooks()
    chat = filters.Chat(chat_id=settings.allowed_chat_id)
    app.add_handler(CommandHandler("merge", tools.cmd_merge, filters=chat))
    app.add_handler(CallbackQueryHandler(tools.on_callback, pattern=r"^mg:"))
    app.bot_data["admin_tools"] = tools
    return tools

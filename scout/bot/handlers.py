"""Telegram handlers, the notifier used by the pipeline, and scheduled jobs."""

from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import hooks
from ..db import Account, Database, Event
from ..media import prune_media
from . import templates as T

log = logging.getLogger(__name__)

HANDLE_RE = re.compile(r"(?:instagram\.com/)?@?([A-Za-z0-9._]{1,30})/?", re.I)
IG_URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:\?.*)?$", re.I)
RESERVED = {"p", "reel", "reels", "stories", "explore", "accounts", "tv"}


def parse_handle(text: str) -> str | None:
    text = text.strip()
    m = IG_URL_RE.match(text) or HANDLE_RE.fullmatch(text)
    if not m:
        return None
    handle = m.group(1).lower().rstrip(".")
    return None if handle in RESERVED else handle


class TelegramNotifier:
    """Implements pipeline.Notifier."""

    def __init__(self, app: Application, db: Database, chat_id: int, tz):  # noqa: ANN001
        self.app = app
        self.db = db
        self.chat_id = chat_id
        self.tz = tz

    @property
    def paused(self) -> bool:
        return self.db.get_setting("paused") == "1"

    def _handle(self, account_id: int) -> str:
        with self.db.session() as s:
            acc = s.get(Account, account_id)
            return acc.handle if acc else "?"

    async def send_event_card(self, ev: Event, header: str | None = None) -> int | None:
        handle = self._handle(ev.account_id)
        teaser = not ev.date_list
        caption = T.event_caption(ev, handle, self.tz, header=header)
        kb = T.decision_keyboard(ev, teaser=teaser) if ev.user_decision == "pending" else T.answered_keyboard(ev)
        bot = self.app.bot
        try:
            if ev.media_path and Path(ev.media_path).exists():
                with open(ev.media_path, "rb") as f:
                    msg = await bot.send_photo(self.chat_id, f, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                msg = await bot.send_message(self.chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=kb,
                                             disable_web_page_preview=True)
        except BadRequest:
            log.exception("Sending card for event %s failed; retrying without photo", ev.id)
            msg = await bot.send_message(self.chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        with self.db.session() as s:
            row = s.get(Event, ev.id)
            row.telegram_message_id = msg.message_id
        return msg.message_id

    async def new_event(self, event: Event, kind: str) -> None:
        if self.paused:
            return  # sent on /resume
        header = "👀 Teaser: something's coming" if not event.date_list else "🆕 New market"
        try:
            await self.send_event_card(event, header=header)
        except TelegramError:
            log.exception("Could not send event %s; it will be sent on /start or /resume", event.id)

    async def event_update(self, event: Event, changes: list[str]) -> None:
        if self.paused:
            return
        handle = self._handle(event.account_id)
        confirmed = event.user_decision == "maybe" and any(c.startswith(("date", "address")) for c in changes)
        if confirmed:
            await self.send_event_card(event, header="🔔 Confirmed: " + "; ".join(changes))
            with self.db.session() as s:
                s.get(Event, event.id).user_decision = "pending"  # ask again
            return
        text = f"📝 <b>Update</b> — {escape(event.title)} (@{escape(handle)})\n" + "\n".join(
            f"• {escape(c)}" for c in changes
        )
        kwargs = {}
        if event.telegram_message_id:
            kwargs["reply_to_message_id"] = event.telegram_message_id
        try:
            await self.app.bot.send_message(self.chat_id, text, parse_mode=ParseMode.HTML, **kwargs)
        except BadRequest:
            try:
                await self.app.bot.send_message(self.chat_id, text, parse_mode=ParseMode.HTML)
            except TelegramError:
                log.exception("Could not send update for event %s", event.id)
        except TelegramError:
            log.exception("Could not send update for event %s", event.id)

    async def warn(self, text: str) -> None:
        try:
            await self.app.bot.send_message(self.chat_id, escape(text), parse_mode=ParseMode.HTML)
        except TelegramError:
            log.exception("Could not send warning: %s", text)

    async def send_unsent(self, header: str) -> int:
        """Cards found while alerts were paused or before the chat was opened."""
        with self.db.session() as s:
            pending = list(s.scalars(select(Event).where(
                Event.user_decision == "pending", Event.telegram_message_id.is_(None), Event.status != "past")))
        for ev in sorted(pending, key=lambda e: e.created_at):
            await self.send_event_card(ev, header=header)
        return len(pending)


class BotHandlers:
    def __init__(self, scout, db: Database, notifier: TelegramNotifier, settings):  # noqa: ANN001
        self.scout = scout
        self.db = db
        self.notifier = notifier
        self.settings = settings

    # ------------------------------------------------------------- registration
    def register(self, app: Application) -> None:
        only_me = filters.Chat(chat_id=self.settings.allowed_chat_id)
        cmds = {
            "start": self.cmd_start, "help": self.cmd_help, "add": self.cmd_add, "remove": self.cmd_remove,
            "list": self.cmd_list, "upcoming": self.cmd_upcoming, "maybe": self.cmd_maybe, "check": self.cmd_check,
            "pause": self.cmd_pause, "resume": self.cmd_resume, "status": self.cmd_status,
        }
        for name, fn in cmds.items():
            app.add_handler(CommandHandler(name, fn, filters=only_me))
        # Core prefixes only; feature modules own their own (eb:, ab:, cal:) with their own handlers.
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^(d|c|r|rc|rx|f):|^x$|^rx$"))
        app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, self.on_text))

    def _allowed(self, update: Update) -> bool:
        return bool(update.effective_chat and update.effective_chat.id == self.settings.allowed_chat_id)

    # ----------------------------------------------------------------- commands
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_html(T.HELP)

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        n = len(self.db.active_accounts())
        await update.effective_message.reply_html(f"👋 Hi! I'm following {n} accounts for you.\n\n" + T.HELP)
        if not self.notifier.paused:
            await self.notifier.send_unsent("🆕 Found while you were away")

    async def cmd_add(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.effective_message.reply_html("Send <code>/add @handle</code> or paste the profile link.")
            return
        handle = parse_handle(ctx.args[0])
        if not handle:
            await update.effective_message.reply_text("That doesn't look like an Instagram account.")
            return
        await self._follow(update, ctx, handle)

    async def _follow(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, handle: str) -> None:
        msg = await update.effective_message.reply_html(f"Checking <b>@{escape(handle)}</b> on Instagram…")
        ctx.application.create_task(self._follow_check(msg, handle), update=update)

    async def _follow_check(self, msg, handle: str) -> None:  # noqa: ANN001
        try:
            snaps = await self.scout.source.profiles([handle])
        except Exception:  # noqa: BLE001
            log.exception("Validation of @%s failed", handle)
            await msg.edit_text(f"Couldn't reach Instagram to check @{handle}. Try again later.")
            return
        snap = snaps.get(handle)
        if not snap:
            await msg.edit_text(f"I couldn't find a public account called @{handle}.")
            return
        acc, created = self.db.add_account(handle)
        dated = [p for p in snap.posts if p.posted_at and not p.is_pinned]
        latest = max(dated, key=lambda p: p.posted_at) if dated else None
        last = f"\nLatest post: {latest.posted_at.astimezone(self.settings.tz):%-d %b}" if latest and latest.posted_at else ""
        name = f" ({escape(snap.full_name)})" if snap.full_name else ""
        verb = "Now following" if created else "Already following"
        await msg.edit_text(f"✅ {verb} <b>@{escape(handle)}</b>{name}.{last}\nI'll read its recent posts now.",
                            parse_mode=ParseMode.HTML)
        if created:
            await self.scout._process_profile(acc, snap)  # reuse the snapshot we already paid for

    async def cmd_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if ctx.args:
            handle = parse_handle(ctx.args[0])
            if handle and self.db.remove_account(handle):
                await update.effective_message.reply_html(f"Stopped following <b>@{escape(handle)}</b>.")
                return
        accounts = self.db.active_accounts()
        if not accounts:
            await update.effective_message.reply_text("I'm not following any accounts.")
            return
        rows = [[InlineKeyboardButton(f"@{a.handle}", callback_data=f"r:{a.handle}")] for a in accounts]
        await update.effective_message.reply_text("Which account should I stop following?",
                                                  reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        accounts = self.db.active_accounts()
        if not accounts:
            await update.effective_message.reply_text("No accounts yet. Add one with /add @handle.")
            return
        tz = self.settings.tz
        lines = [f"<b>Following {len(accounts)} accounts</b>"]
        for a in accounts:
            seen = f"checked {a.last_ok_at.astimezone(tz):%-d %b %H:%M}" if a.last_ok_at else "not checked yet"
            warn = " ⚠️" if a.consecutive_failures >= 3 else ""
            lines.append(f"• @{escape(a.handle)} — {seen}{warn}")
        await update.effective_message.reply_html("\n".join(lines))

    async def _list_events(self, update: Update, decision: str, title: str, empty: str) -> None:
        with self.db.session() as s:
            evs = list(s.scalars(select(Event).where(Event.user_decision == decision, Event.status != "past")))
            handles = {a.id: a.handle for a in s.scalars(select(Account))}
        if not evs:
            await update.effective_message.reply_text(empty)
            return
        evs.sort(key=lambda e: (e.date_list[0] if e.date_list else datetime.max.date()))
        # Group by weekend (ISO week) so clashes are visible.
        lines, week = [f"<b>{title}</b>"], None
        for e in evs:
            wk = e.date_list[0].isocalendar()[:2] if e.date_list else None
            if wk != week:
                week = wk
                lines.append("")
                lines.append(f"<i>Week of {e.date_list[0]:%-d %b}</i>" if e.date_list else "<i>No date yet</i>")
            lines.append(T.short_line(e, handles.get(e.account_id, "?")))
        await update.effective_message.reply_html("\n".join(lines), disable_web_page_preview=True)

    async def cmd_upcoming(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._list_events(update, "yes", "Your upcoming markets", "No markets marked ✅ yet.")

    async def cmd_maybe(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._list_events(update, "maybe", "Watching for you", "Nothing on 🤔 Maybe right now.")

    async def cmd_check(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = await update.effective_message.reply_text("Checking all accounts now… (takes about a minute)")
        ctx.application.create_task(self._check_now(msg), update=update)

    async def _check_now(self, msg) -> None:  # noqa: ANN001
        try:
            counts = await self.scout.poll_profiles()
        except Exception:  # noqa: BLE001
            log.exception("Manual check failed")
            await msg.edit_text("The check failed; I'll try again at the next scheduled run.")
            return
        new = sum(counts.values())
        await msg.edit_text(f"Done. {len(counts)} accounts read, {new} new item(s) processed.")

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.db.set_setting("paused", "1")
        await update.effective_message.reply_text("⏸ Alerts paused. I keep checking, and /resume sends what you missed.")

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.db.set_setting("paused", "0")
        await update.effective_message.reply_text("▶️ Alerts back on.")
        await self.notifier.send_unsent("🆕 While you were paused")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        tz = self.settings.tz
        accounts = self.db.active_accounts()
        last = max((a.last_ok_at for a in accounts if a.last_ok_at), default=None)
        failing = [a.handle for a in accounts if a.consecutive_failures >= 3]
        spend = await self.scout.source.spend_usd()
        lines = [
            "<b>Scout status</b>",
            f"Accounts: {len(accounts)}",
            f"Last successful check: {last.astimezone(tz):%a %-d %b %H:%M}" if last else "Last successful check: none yet",
            "Checks: posts and bio at " + ", ".join(f"{h:02d}:00" for h in self.settings.poll_hours) + "; stories at 21:00",
            "Alerts: paused" if self.notifier.paused else "Alerts: on",
        ]
        if spend:
            used, cap = spend
            lines.append(f"Apify this month: ${used:.2f}" + (f" of ${cap:.2f}" if cap else ""))
        if failing:
            lines.append("⚠️ Not readable: " + ", ".join("@" + h for h in failing))
        await update.effective_message.reply_html("\n".join(lines))

    # ----------------------------------------------------------------- free text
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.effective_message.text or "").strip()
        m = IG_URL_RE.match(text)
        if m and m.group(1).lower() not in RESERVED:
            handle = m.group(1).lower()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Yes, follow", callback_data=f"f:{handle}"),
                                        InlineKeyboardButton("No", callback_data="x")]])
            await update.effective_message.reply_html(f"Follow <b>@{escape(handle)}</b>?", reply_markup=kb)
            return
        await update.effective_message.reply_html("I didn't get that. " + T.HELP.split("\n\n", 1)[1])

    # ------------------------------------------------------------------ buttons
    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self._allowed(update):
            await q.answer()
            return
        data = q.data or ""
        if data == "x":
            await q.answer()
            await q.edit_message_text("OK, not following it.")
            return
        if data.startswith("f:"):
            await q.answer()
            handle = data[2:]
            await q.edit_message_text(f"Checking @{handle} on Instagram…")
            ctx.application.create_task(self._follow_check(q.message, handle), update=update)
            return
        if data.startswith("r:"):
            handle = data[2:]
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Yes, remove @{handle}", callback_data=f"rc:{handle}"),
                                        InlineKeyboardButton("Cancel", callback_data="rx")]])
            await q.answer()
            await q.edit_message_text(f"Stop following @{handle}? Events already found stay in /upcoming.", reply_markup=kb)
            return
        if data.startswith("rc:"):
            handle = data[3:]
            ok = self.db.remove_account(handle)
            await q.answer("Removed" if ok else "Already removed")
            await q.edit_message_text(f"Stopped following @{handle}." if ok else f"@{handle} wasn't on the list.")
            return
        if data == "rx":
            await q.answer()
            await q.edit_message_text("Nothing removed.")
            return
        if data.startswith("d:"):
            _, ev_id, decision = data.split(":")
            await self._decide(q, int(ev_id), decision)
            return
        if data.startswith("c:"):
            ev_id = int(data[2:])
            with self.db.session() as s:
                ev = s.get(Event, ev_id)
            await q.answer()
            if ev:
                await q.edit_message_reply_markup(T.decision_keyboard(ev, teaser=not ev.date_list))
            return
        await q.answer()

    async def _decide(self, q, ev_id: int, decision: str) -> None:  # noqa: ANN001
        with self.db.session() as s:
            ev = s.get(Event, ev_id)
            if not ev:
                await q.answer("That market is no longer tracked.")
                return
            old = ev.user_decision
            ev.user_decision = decision
            handle = s.get(Account, ev.account_id).handle
        await hooks.emit("decision_changed", ev_id, old=old, new=decision)
        toast = {"yes": "Marked as going", "maybe": "I'll tell you when it's confirmed", "no": "Skipped"}[decision]
        await q.answer(toast)
        caption = T.event_caption(ev, handle, self.settings.tz)
        try:
            if q.message and q.message.photo:
                await q.edit_message_caption(caption=caption, parse_mode=ParseMode.HTML, reply_markup=T.answered_keyboard(ev))
            else:
                await q.edit_message_text(caption, parse_mode=ParseMode.HTML, reply_markup=T.answered_keyboard(ev))
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise


# ---------------------------------------------------------------------- jobs
def schedule_jobs(app: Application, scout, settings, db: Database) -> None:  # noqa: ANN001
    jq = app.job_queue
    tz = settings.tz

    async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            counts = await scout.poll_profiles()
            log.info("Profile poll done: %s", counts)
            await _check_budget(scout, db, settings, app)
        except Exception:  # noqa: BLE001
            log.exception("Profile poll failed")

    async def stories_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n = await scout.poll_stories()
            log.info("Story poll done: %d new", n)
        except Exception:  # noqa: BLE001
            log.exception("Story poll failed")

    async def upkeep_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        n = scout.mark_past_events()
        removed = prune_media(settings.media_dir)
        log.info("Upkeep: %d events marked past, %d images pruned", n, removed)

    for h in settings.poll_hours:
        jq.run_daily(poll_job, time(hour=h, minute=2, tzinfo=tz), name=f"poll-{h:02d}")
    jq.run_daily(stories_job, time(hour=21, minute=7, tzinfo=tz), name="stories")
    jq.run_daily(upkeep_job, time(hour=4, minute=0, tzinfo=tz), name="upkeep")

    # First run after a fresh install, or if the last good poll is older than 3 h.
    beat = settings.heartbeat_file
    stale = not beat.exists() or (datetime.now().timestamp() - beat.stat().st_mtime) > 3 * 3600
    if stale:
        jq.run_once(poll_job, when=timedelta(seconds=30), name="startup-poll")


async def _check_budget(scout, db: Database, settings, app: Application) -> None:  # noqa: ANN001
    spend = await scout.source.spend_usd()
    if not spend:
        return
    used, cap = spend
    cap = cap or settings.apify_budget_usd
    month = datetime.now(settings.tz).strftime("%Y-%m")
    if cap and used >= 0.8 * cap and db.get_setting("budget_warned") != month:
        db.set_setting("budget_warned", month)
        await app.bot.send_message(
            settings.allowed_chat_id,
            f"⚠️ Apify has used ${used:.2f} of ${cap:.2f} this month. If it runs out, checks stop until the 1st.",
        )

"""Telegram handlers, the notifier used by the pipeline, and scheduled jobs."""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .. import hooks, obs
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
        obs.STATS.incr("alerts_sent")
        log.info("card sent %s", obs.kv(event_id=ev.id, message_id=msg.message_id, decision=ev.user_decision,
                                        header=header))
        return msg.message_id

    async def new_event(self, event: Event, kind: str) -> None:
        if self.paused:
            log.info("alert held %s", obs.kv(event_id=event.id, reason="paused"))
            return  # sent on /resume
        header = "👀 Teaser: something's coming" if not event.date_list else "🆕 New market"
        try:
            await self.send_event_card(event, header=header)
        except TelegramError:
            log.exception("Could not send event %s; it will be sent on /start or /resume", event.id)

    async def event_update(self, event: Event, changes: list[str]) -> None:
        if self.paused:
            log.info("update held %s", obs.kv(event_id=event.id, reason="paused"))
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
                return
        except TelegramError:
            log.exception("Could not send update for event %s", event.id)
            return
        obs.STATS.incr("alerts_sent")
        log.info("update sent %s", obs.kv(event_id=event.id, changes=len(changes)))

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


EVENTS_PAGE_SIZE = 8
DECISION_EMOJI = {"pending": "🆕", "yes": "✅", "maybe": "🤔", "no": "❌", "filtered": "🌍"}
EVENT_FILTERS: dict[str, str] = {  # code -> button label
    "up": "Upcoming", "new": "New", "yes": "✅", "maybe": "🤔", "no": "❌", "out": "Outside Madrid", "past": "Past",
}
EVENT_FILTER_TITLES = {
    "up": "Upcoming", "new": "New (not answered)", "yes": "✅ Going", "maybe": "🤔 Maybe", "no": "❌ Skipped",
    "out": "Outside Madrid", "past": "Past",
}
# BadRequest texts after which a card can't be edited in place, so a fresh one is sent.
UNEDITABLE = ("there is no caption in the message to edit", "there is no text in the message to edit",
              "message can't be edited", "message to edit not found")


def filter_events(evs: list[Event], code: str) -> list[Event]:
    """Apply an /events filter and sort (soonest first; past: most recent first)."""
    def live(e: Event) -> bool:
        return e.status != "past"

    preds = {
        "up": lambda e: live(e) and e.user_decision != "filtered",
        "new": lambda e: live(e) and e.user_decision == "pending",
        "yes": lambda e: live(e) and e.user_decision == "yes",
        "maybe": lambda e: live(e) and e.user_decision == "maybe",
        "no": lambda e: live(e) and e.user_decision == "no",
        "out": lambda e: live(e) and e.user_decision == "filtered",
        "past": lambda e: not live(e),
    }
    out = [e for e in evs if preds.get(code, preds["up"])(e)]
    out.sort(key=lambda e: (e.date_list[0] if e.date_list else date.max, e.id), reverse=code == "past")
    return out


def short_title(title: str, limit: int = 28) -> str:
    return title if len(title) <= limit else title[: limit - 1] + "…"


class BotHandlers:
    def __init__(self, scout, db: Database, notifier: TelegramNotifier, settings):  # noqa: ANN001
        self.scout = scout
        self.db = db
        self.notifier = notifier
        self.settings = settings

    # ------------------------------------------------------------- registration
    def register(self, app: Application) -> None:
        only_me = filters.Chat(chat_id=self.settings.allowed_chat_id)
        # Runs first for every update: trace id + one log line; handling then continues in group 0.
        app.add_handler(TypeHandler(Update, obs.make_update_logger(self.settings.allowed_chat_id)), group=-1)
        cmds = {
            "start": self.cmd_start, "help": self.cmd_help, "add": self.cmd_add, "remove": self.cmd_remove,
            "list": self.cmd_list, "upcoming": self.cmd_upcoming, "maybe": self.cmd_maybe, "check": self.cmd_check,
            "pause": self.cmd_pause, "resume": self.cmd_resume, "status": self.cmd_status,
            "events": self.cmd_events, "logs": self.cmd_logs,
        }
        for name, fn in cmds.items():
            app.add_handler(CommandHandler(name, fn, filters=only_me))
        # Core prefixes only; feature modules own their own (eb:, ab:, cal:) with their own handlers.
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^(d|c|r|rc|rx|f|ev|e):|^x$|^rx$"))
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
            async with obs.trace("poll_profiles", trigger="manual"):
                counts = await self.scout.poll_profiles()
        except Exception:  # noqa: BLE001 - logged with traceback by obs.trace
            log.warning("Manual check failed")
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
        st = obs.STATS
        lines += ["", f"Up for {st.uptime()} · Telegram: {self.settings.telegram_mode}"]
        poll = st.runs.get("poll_profiles")
        if poll:
            lines.append(f"Last poll: {poll.finished_at.astimezone(tz):%a %-d %b %H:%M}, "
                         f"{poll.duration_ms / 1000:.1f}s, {poll.outcome}")
        c = st.counters
        lines.append(f"Since start: {c['updates']} updates ({c['callbacks']} taps), {c['alerts_sent']} alerts, "
                     f"{c['gemini_calls']} Gemini calls ({c['gemini_tokens']:,} tokens), {c['apify_runs']} Apify runs, "
                     f"{c['errors']} errors")
        if st.last_error_at:
            lines.append(f"Last error: {st.last_error_at.astimezone(tz):%a %-d %b %H:%M} (see /logs)")
        await update.effective_message.reply_html("\n".join(lines))

    async def cmd_logs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = [a.lower() for a in (ctx.args or [])]
        level = logging.INFO if "all" in args else logging.WARNING
        n = next((int(a) for a in args if a.isdigit()), 20)
        n = max(1, min(n, 50))
        lines = obs.recent(n, level)
        if not lines:
            await update.effective_message.reply_text(
                "No warnings or errors since start." if level > logging.INFO else "No log records yet.")
            return
        text = "\n".join(lines)
        if len(text) > 4000:
            text = "…" + text[-3999:]
        await update.effective_message.reply_html(f"<pre>{escape(text)}</pre>")

    # ------------------------------------------------------------------ /events
    async def cmd_events(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text, kb = self.events_page("up", 0)
        await update.effective_message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)

    def events_page(self, code: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
        """Text and keyboard for one page of /events."""
        code = code if code in EVENT_FILTERS else "up"
        with self.db.session() as s:
            evs = filter_events(list(s.scalars(select(Event))), code)
            handles = {a.id: a.handle for a in s.scalars(select(Account))}
        pages = max(1, math.ceil(len(evs) / EVENTS_PAGE_SIZE))
        page = max(0, min(page, pages - 1))
        chunk = evs[page * EVENTS_PAGE_SIZE:(page + 1) * EVENTS_PAGE_SIZE]

        head = f"<b>Events · {EVENT_FILTER_TITLES[code]}</b> ({len(evs)})"
        if pages > 1:
            head += f" — page {page + 1}/{pages}"
        lines = [head, ""]
        if not chunk:
            lines.append("Nothing here.")
        for e in chunk:
            when = ", ".join(T.fmt_day(d) for d in e.date_list) if e.date_list else "date TBA"
            where = e.address or e.area or "place TBA"
            emoji = DECISION_EMOJI.get(e.user_decision, "•")
            lines.append(f"{emoji} <b>#{e.id}</b> {escape(e.title)} — {escape(when)} — {escape(where)} "
                         f"(@{escape(handles.get(e.account_id, '?'))})")

        labels = [(c, ("• " if c == code else "") + label) for c, label in EVENT_FILTERS.items()]
        rows = [[InlineKeyboardButton(lbl, callback_data=f"ev:{c}:0") for c, lbl in labels[:4]],
                [InlineKeyboardButton(lbl, callback_data=f"ev:{c}:0") for c, lbl in labels[4:]]]
        rows += [[InlineKeyboardButton(f"#{e.id} {short_title(e.title)}", callback_data=f"e:{e.id}")] for e in chunk]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"ev:{code}:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"ev:{code}:{page + 1}"))
        if nav:
            rows.append(nav)
        return "\n".join(lines), InlineKeyboardMarkup(rows)

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
        """Button taps. The query is answered exactly once (Telegram shows a spinner until then)."""
        q = update.callback_query
        answered = False

        async def answer(text: str | None = None) -> None:
            nonlocal answered
            if answered:
                return
            answered = True
            try:
                await q.answer(text)
            except TelegramError as exc:  # e.g. "query is too old" after a restart; the work still happens
                log.warning("callback answer failed %s: %s", obs.kv(data=q.data), exc)

        try:
            if not self._allowed(update):
                return
            await self._dispatch(q, ctx, update, q.data or "", answer)
        finally:
            await answer()

    async def _dispatch(self, q, ctx, update: Update, data: str, answer) -> None:  # noqa: ANN001
        if data == "x":
            await answer()
            await self._edit_text(q, "OK, not following it.")
        elif data.startswith("f:"):
            await answer()
            handle = data[2:]
            await self._edit_text(q, f"Checking @{handle} on Instagram…")
            ctx.application.create_task(self._follow_check(q.message, handle), update=update)
        elif data.startswith("r:"):
            await answer()
            handle = data[2:]
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Yes, remove @{handle}", callback_data=f"rc:{handle}"),
                                        InlineKeyboardButton("Cancel", callback_data="rx")]])
            await self._edit_text(q, f"Stop following @{handle}? Events already found stay in /upcoming.", kb)
        elif data.startswith("rc:"):
            handle = data[3:]
            ok = self.db.remove_account(handle)
            log.info("account removed %s", obs.kv(handle=handle, ok=ok))
            await answer("Removed" if ok else "Already removed")
            await self._edit_text(q, f"Stopped following @{handle}." if ok else f"@{handle} wasn't on the list.")
        elif data == "rx":
            await answer()
            await self._edit_text(q, "Nothing removed.")
        elif data.startswith("d:"):
            parts = data.split(":")
            if len(parts) != 3 or not parts[1].isdigit():
                log.warning("bad decision callback %s", obs.kv(data=data))
                return
            await self._decide(q, int(parts[1]), parts[2], answer)
        elif data.startswith("c:"):
            await answer()
            with self.db.session() as s:
                ev = s.get(Event, int(data[2:]))
            if ev:
                try:
                    await q.edit_message_reply_markup(T.decision_keyboard(ev, teaser=not ev.date_list))
                except BadRequest as exc:
                    if "not modified" in str(exc).lower():
                        return
                    log.warning("change-answer edit failed, sending fresh card %s: %s", obs.kv(event_id=ev.id), exc)
                    ev.user_decision = "pending"  # fresh card with decision buttons; DB keeps the old answer
                    await self.notifier.send_event_card(ev)
        elif data.startswith("ev:"):
            await answer()
            _, code, page = (data.split(":") + ["0"])[:3]
            text, kb = self.events_page(code, int(page) if page.isdigit() else 0)
            await self._edit_text(q, text, kb, html=True)
        elif data.startswith("e:"):
            with self.db.session() as s:
                ev = s.get(Event, int(data[2:])) if data[2:].isdigit() else None
            if not ev:
                await answer("That market is no longer tracked.")
                return
            await answer()
            await self.notifier.send_event_card(ev)

    async def _edit_text(self, q, text: str, kb: InlineKeyboardMarkup | None = None, html: bool = False) -> None:  # noqa: ANN001
        """Edit the tapped message; a stale or identical message is logged, not raised."""
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML if html else None,
                                      disable_web_page_preview=True)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("edit failed, sending new message %s: %s", obs.kv(data=q.data), exc)
            await q.get_bot().send_message(self.settings.allowed_chat_id, text, reply_markup=kb,
                                           parse_mode=ParseMode.HTML if html else None)

    async def _decide(self, q, ev_id: int, decision: str, answer=None) -> None:  # noqa: ANN001
        """Store the answer, toast it, run hooks, then turn the card into an answered card."""
        answer = answer or (lambda text=None: q.answer(text))
        if decision not in ("yes", "maybe", "no"):
            log.warning("unknown decision %s", obs.kv(event_id=ev_id, decision=decision))
            await answer()
            return
        with self.db.session() as s:
            ev = s.get(Event, ev_id)
            if not ev:
                log.warning("decision for unknown event %s", obs.kv(event_id=ev_id, decision=decision))
                await answer("That market is no longer tracked.")
                return
            old = ev.user_decision
            ev.user_decision = decision
            acc = s.get(Account, ev.account_id)
            handle = acc.handle if acc else "?"
        log.info("decision %s", obs.kv(event_id=ev_id, old=old, new=decision, teaser=not ev.date_list))
        toast = {"yes": "Marked as going", "maybe": "I'll tell you when it's confirmed", "no": "Skipped"}[decision]
        await answer(toast)
        await hooks.emit("decision_changed", ev_id, old=old, new=decision)
        await self._edit_card(q, ev, handle)

    async def _edit_card(self, q, ev: Event, handle: str) -> None:  # noqa: ANN001
        caption = T.event_caption(ev, handle, self.settings.tz)
        kb = T.answered_keyboard(ev)
        msg = q.message if isinstance(q.message, Message) else None
        try:
            if msg is None:
                raise BadRequest("Message to edit not found")
            if msg.photo:
                await q.edit_message_caption(caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await q.edit_message_text(caption, parse_mode=ParseMode.HTML, reply_markup=kb,
                                          disable_web_page_preview=True)
        except BadRequest as exc:
            text = str(exc).lower()
            if "not modified" in text:
                log.info("card unchanged %s", obs.kv(event_id=ev.id))
                return
            if not any(u in text for u in UNEDITABLE):
                log.warning("card edit failed %s: %s", obs.kv(event_id=ev.id), exc)
            log.info("sending fresh card %s", obs.kv(event_id=ev.id, reason=str(exc)))
            await self.notifier.send_event_card(ev)
        else:
            log.info("card edited %s", obs.kv(event_id=ev.id, decision=ev.user_decision))


# ---------------------------------------------------------------------- jobs
def schedule_jobs(app: Application, scout, settings, db: Database) -> None:  # noqa: ANN001
    jq = app.job_queue
    tz = settings.tz

    async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        up = app.bot_data.get("uptime")
        try:
            async with obs.trace("poll_profiles", job=ctx.job.name if ctx.job else None):
                counts = await scout.poll_profiles()
                log.info("Profile poll done: %s", counts)
                await _check_budget(scout, db, settings, app)
        except Exception as exc:  # noqa: BLE001 - logged by trace; reported after repeated failures
            if up:
                await up.poll_result(False, f"{type(exc).__name__}: {exc}")
            return
        if up:
            await up.poll_result(True)

    async def stories_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        async with obs.trace("poll_stories", swallow=True):
            n = await scout.poll_stories()
            log.info("Story poll done: %d new", n)

    async def upkeep_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        async with obs.trace("upkeep", swallow=True):
            merged = await scout.dedupe_events()
            n = scout.mark_past_events()
            log.info("Upkeep: %d duplicate events merged", merged)
            removed = prune_media(settings.media_dir)
            log.info("Upkeep: %d events marked past, %d images pruned", n, removed)
            from ..cleanup import run_cleanup; run_cleanup(db, settings)  # noqa: E702 — retention (RETENTION_DAYS)

    async def dedupe_once(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        n = await scout.dedupe_events()
        if n:
            log.info("Startup: merged %d duplicate events", n)

    jq.run_once(dedupe_once, when=timedelta(seconds=3), name="startup-dedupe")
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

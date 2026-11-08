"""Tell the user only when something is down.

A bot can't message you while it's down, so Scout reports outages when it comes back:
- every minute it touches DATA_DIR/alive; on start, a gap longer than DOWN_AFTER means the
  Pi, Docker or Scout was down, and you get one "was down from … to …" message.
  Normal deploys and restarts take under a minute and stay silent.
- scheduled Instagram checks that fail twice in a row send one warning, and one "back to
  normal" message when a check succeeds again.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

log = logging.getLogger(__name__)

DOWN_AFTER = timedelta(minutes=10)
POLL_FAILURES_BEFORE_WARNING = 2


def _fmt_span(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [f"{days} d"] * bool(days) + [f"{hours} h"] * bool(hours) + [f"{minutes} min"]
    return " ".join(parts)


class Uptime:
    def __init__(self, app: Application, settings, db):  # noqa: ANN001
        self.app = app
        self.settings = settings
        self.db = db
        self.alive = settings.data_dir / "alive"
        self.poll_failures = 0
        self.previous_alive: float | None = self.alive.stat().st_mtime if self.alive.exists() else None

    async def _send(self, text: str) -> None:
        try:
            await self.app.bot.send_message(self.settings.allowed_chat_id, text)
        except TelegramError:
            log.exception("Could not send uptime notice")

    async def report_downtime(self) -> None:
        """Called once after start. Silent for normal restarts."""
        if self.previous_alive is None:
            return
        gap = time.time() - self.previous_alive
        if gap < DOWN_AFTER.total_seconds():
            log.info("Restart after %.0f s: not reported", gap)
            return
        tz = self.settings.tz
        since = datetime.fromtimestamp(self.previous_alive, tz)
        now = datetime.now(tz)
        fmt = "%a %-d %b %H:%M" if since.date() != now.date() else "%H:%M"
        log.warning("Scout was down %s", _fmt_span(gap))
        await self._send(
            f"⚠️ Scout was down from {since.strftime(fmt)} to {now.strftime('%H:%M')} ({_fmt_span(gap)}).\n"
            "It's back now and catching up on missed checks."
        )

    async def beat(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        self.alive.parent.mkdir(parents=True, exist_ok=True)
        self.alive.touch()

    async def poll_result(self, ok: bool, error: str | None = None) -> None:
        if ok:
            if self.poll_failures >= POLL_FAILURES_BEFORE_WARNING:
                await self._send("✅ Instagram checks are working again.")
            self.poll_failures = 0
            return
        self.poll_failures += 1
        if self.poll_failures == POLL_FAILURES_BEFORE_WARNING:
            await self._send(
                f"⚠️ Instagram checks failed {self.poll_failures} times in a row"
                + (f": {error[:200]}" if error else ".")
                + "\nI'll keep trying and tell you when it's fixed. /logs has details."
            )


class WebhookWatch:
    """In webhook mode Telegram reports delivery errors (bad certificate, 502…) via getWebhookInfo.
    Warn once per outage, and once when deliveries recover."""

    def __init__(self, up: Uptime):
        self.up = up
        self.down_since: float | None = None

    async def check(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        try:
            info = await self.up.app.bot.get_webhook_info()
        except TelegramError:
            log.exception("getWebhookInfo failed")
            return
        err_ts = info.last_error_date.timestamp() if info.last_error_date else None
        failing = bool(err_ts and time.time() - err_ts < 15 * 60 and (info.pending_update_count or 0) > 0)
        if failing and self.down_since is None:
            self.down_since = err_ts
            log.warning("Telegram can't deliver to webhook: %s (pending %s)", info.last_error_message, info.pending_update_count)
            await self.up._send(
                f"⚠️ Telegram can't reach Scout at {info.url or 'the webhook'}: {info.last_error_message}.\n"
                "Buttons and commands won't work until it's fixed; alerts still arrive."
            )
        elif not failing and self.down_since is not None:
            self.down_since = None
            await self.up._send("✅ Telegram can reach Scout again; buttons and commands work.")


def setup_uptime(app: Application, settings, db) -> Uptime:  # noqa: ANN001
    up = Uptime(app, settings, db)
    app.bot_data["uptime"] = up
    app.job_queue.run_repeating(up.beat, interval=60, first=1, name="alive")

    async def after_start(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await up.report_downtime()

    app.job_queue.run_once(after_start, when=5, name="downtime-report")
    if settings.telegram_mode == "webhook":
        ww = WebhookWatch(up)
        app.job_queue.run_repeating(ww.check, interval=300, first=60, name="webhook-watch")
    return up

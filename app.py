"""Scout entry point: `python app.py` (same convention as the Cinematic Concierge bot).

Telegram updates arrive by webhook at WEBHOOK_URL (Dokploy → Traefik → port 8000).
"""

import hashlib
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from scout import obs
from scout.autobook import setup_autobook
from scout.bot.admin import register_admin
from scout.bot.handlers import BotHandlers, TelegramNotifier, schedule_jobs
from scout.config import Settings
from scout.db import Database
from scout.gcal import setup_calendar
from scout.uptime import setup_uptime
from scout.eventbrite import setup_booking_watcher
from scout.parser.gemini import GeminiParser
from scout.pipeline import Scout
from scout.sources.apify import ApifySource

log = logging.getLogger("scout")


def build_source(settings: Settings):
    if settings.ig_provider == "apify":
        return ApifySource(settings.apify_token)
    raise RuntimeError(f"Unknown IG_PROVIDER {settings.ig_provider!r}")


def main() -> None:
    obs.setup_logging(None)  # stdout only until DATA_DIR is known
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    obs.setup_logging(settings.data_dir)
    log.info("Scout starting %s", obs.kv(mode=settings.telegram_mode, data_dir=settings.data_dir,
                                         model=settings.gemini_model))
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "started").touch()

    db = Database(settings.db_path)
    db.seed_accounts(settings.seed_accounts)

    app = Application.builder().token(settings.bot_token).build()
    notifier = TelegramNotifier(app, db, settings.allowed_chat_id, settings.tz)
    scout = Scout(settings, db, build_source(settings), GeminiParser(settings.gemini_api_key, settings.gemini_model), notifier)
    BotHandlers(scout, db, notifier, settings).register(app)
    setup_booking_watcher(app, settings, db)  # M3: Eventbrite booking pings, /bookings
    schedule_jobs(app, scout, settings, db)
    setup_calendar(app, settings, db)  # M2: no-op unless GCAL_SA_JSON and GCAL_CALENDAR_ID are set
    app.add_error_handler(obs.ErrorReporter(settings.allowed_chat_id))
    setup_autobook(app, settings, db)
    setup_uptime(app, settings, db)  # tells you only when Scout was down or checks keep failing
    register_admin(app, settings, db, notifier)  # /merge, in-place card refresh on updates/merges

    async def on_start(application: Application) -> None:
        await application.bot.set_my_commands([
            ("upcoming", "Markets you said yes to"),
            ("bookings", "Eventbrite openings I'm watching"),
            ("maybe", "Markets I'm watching for you"),
            ("events", "All tracked markets, with filters"),
            ("add", "Follow an Instagram account"),
            ("remove", "Stop following an account"),
            ("list", "Accounts I follow"),
            ("check", "Check all accounts now"),
            ("status", "Last checks, counters and Apify credit"),
            ("logs", "Recent warnings and errors"),
            ("autobook", "Auto-book free Eventbrite tickets"),
            ("merge", "Merge two duplicate markets"),
            ("pause", "Mute alerts"),
            ("resume", "Unmute alerts"),
            ("help", "What Scout does"),
        ])
        # No "I'm up" message: you only hear about outages (scout/uptime.py).
        log.info("Scout up, following %d accounts", len(db.active_accounts()))

    app.post_init = on_start

    if settings.telegram_mode == "webhook":
        secret = hashlib.sha256(settings.bot_token.encode()).hexdigest()[:32]
        log.info("Starting webhook on :%d/%s", settings.port, settings.webhook_path)
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.port,
            url_path=settings.webhook_path,
            webhook_url=settings.webhook_url,
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        # Long polling: the bot fetches its own updates, so no public domain or certificate is needed.
        # run_polling deletes any webhook Telegram still has registered.
        log.info("Starting long polling")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()

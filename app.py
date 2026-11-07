"""Scout entry point: `python app.py` (same convention as the Cinematic Concierge bot).

Telegram updates arrive by webhook at WEBHOOK_URL (Dokploy → Traefik → port 8000).
"""

import hashlib
import logging

from telegram import Update
from telegram.ext import Application

from scout.bot.handlers import BotHandlers, TelegramNotifier, schedule_jobs
from scout.config import Settings
from scout.db import Database
from scout.parser.gemini import GeminiParser
from scout.pipeline import Scout
from scout.sources.apify import ApifySource

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("scout")


def build_source(settings: Settings):
    if settings.ig_provider == "apify":
        return ApifySource(settings.apify_token)
    raise RuntimeError(f"Unknown IG_PROVIDER {settings.ig_provider!r}")


def main() -> None:
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "started").touch()

    db = Database(settings.db_path)
    db.seed_accounts(settings.seed_accounts)

    app = Application.builder().token(settings.bot_token).build()
    notifier = TelegramNotifier(app, db, settings.allowed_chat_id, settings.tz)
    scout = Scout(settings, db, build_source(settings), GeminiParser(settings.gemini_api_key, settings.gemini_model), notifier)
    BotHandlers(scout, db, notifier, settings).register(app)
    schedule_jobs(app, scout, settings, db)

    async def on_start(application: Application) -> None:
        await application.bot.set_my_commands([
            ("upcoming", "Markets you said yes to"),
            ("maybe", "Markets I'm watching for you"),
            ("add", "Follow an Instagram account"),
            ("remove", "Stop following an account"),
            ("list", "Accounts I follow"),
            ("check", "Check all accounts now"),
            ("status", "Last checks and Apify credit"),
            ("pause", "Mute alerts"),
            ("resume", "Unmute alerts"),
            ("help", "What Scout does"),
        ])
        n = len(db.active_accounts())
        await application.bot.send_message(settings.allowed_chat_id, f"🟢 Scout is up — following {n} accounts.")

    app.post_init = on_start

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


if __name__ == "__main__":
    main()

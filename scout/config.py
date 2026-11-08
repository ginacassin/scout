"""Settings read from environment variables (set in Dokploy)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

DEFAULT_ACCOUNTS = [
    "mercadillosencasa",
    "arquitecturadelorden",
    "almamarket_home",
    "thecircularmarket.es",
    "homemarket_lmh",
    "my_market_mad",
]


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    webhook_url: str
    port: int
    allowed_chat_id: int
    gemini_api_key: str
    gemini_model: str
    ig_provider: str
    apify_token: str
    eventbrite_token: str
    gcal_sa_json: str
    gcal_calendar_id: str
    tz: ZoneInfo
    data_dir: Path
    poll_hours: list[int] = field(default_factory=lambda: [8, 11, 14, 17, 20, 23])
    apify_budget_usd: float = 5.0
    region_filter: str = "madrid"
    seed_accounts: list[str] = field(default_factory=lambda: list(DEFAULT_ACCOUNTS))
    telegram_mode: str = "webhook"  # webhook | polling

    @property
    def webhook_path(self) -> str:
        """Path part of WEBHOOK_URL without slashes, e.g. 'scoutbot'."""
        return urlparse(self.webhook_url).path.strip("/")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scout.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        missing = [k for k in ("BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_ID", "GEMINI_API_KEY", "APIFY_TOKEN") if not env.get(k)]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        hours = [int(h) for h in _list(env.get("POLL_HOURS"))] or [8, 11, 14, 17, 20, 23]
        return cls(
            bot_token=env["BOT_TOKEN"],
            webhook_url=env.get("WEBHOOK_URL", ""),
            port=int(env.get("PORT", "8000")),
            allowed_chat_id=int(env["TELEGRAM_ALLOWED_CHAT_ID"]),
            gemini_api_key=env["GEMINI_API_KEY"],
            gemini_model=env.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            ig_provider=env.get("IG_PROVIDER", "apify"),
            apify_token=env["APIFY_TOKEN"],
            eventbrite_token=env.get("EVENTBRITE_TOKEN", ""),
            gcal_sa_json=env.get("GCAL_SA_JSON", ""),
            gcal_calendar_id=env.get("GCAL_CALENDAR_ID", ""),
            tz=ZoneInfo(env.get("TZ", "Europe/Madrid")),
            data_dir=Path(env.get("DATA_DIR", "/data")),
            poll_hours=hours,
            apify_budget_usd=float(env.get("APIFY_MONTHLY_BUDGET_USD", "5")),
            region_filter=env.get("REGION_FILTER", "madrid"),
            seed_accounts=_list(env.get("SEED_ACCOUNTS")) or list(DEFAULT_ACCOUNTS),
            telegram_mode=env.get("TELEGRAM_MODE", "webhook"),
        )

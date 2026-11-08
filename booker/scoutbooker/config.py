"""Settings from environment variables (set in Dokploy)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    token: str = ""                     # BOOKER_TOKEN: shared secret with Scout (X-Booker-Token)
    bot_token: str = ""                 # BOT_TOKEN: same bot as Scout; we only call sendMessage/sendPhoto
    chat_id: int = 0                    # TELEGRAM_ALLOWED_CHAT_ID
    profile_dir: Path = Path("/profile")
    port: int = 8100
    quantity: int = 1                   # BOOKER_QUANTITY
    preopen_s: int = 90                 # open the event page this long before sales open
    poll_ms: int = 300                  # how often to look at the button
    reload_every_s: float = 2.5         # how often to reload the page while waiting
    window_s: int = 180                 # give up this long after the opening time
    display: str = ":99"
    vnc_public_url: str = "http://192.168.1.100:6080/vnc.html"
    vnc_password: str = ""              # optional; noVNC asks for it
    login_timeout_s: int = 900          # login mode closes itself after 15 minutes
    base_url: str = "https://www.eventbrite.es"
    tz: ZoneInfo = ZoneInfo("Europe/Madrid")

    @property
    def jobs_path(self) -> Path:
        return self.profile_dir / "jobs.json"

    @property
    def chromium_dir(self) -> Path:
        return self.profile_dir / "chromium"

    @property
    def shots_dir(self) -> Path:
        return self.profile_dir / "shots"

    @property
    def tickets_dir(self) -> Path:
        return self.profile_dir / "tickets"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        return cls(
            token=env.get("BOOKER_TOKEN", ""),
            bot_token=env.get("BOT_TOKEN", ""),
            chat_id=int(env.get("TELEGRAM_ALLOWED_CHAT_ID") or 0),
            profile_dir=Path(env.get("PROFILE_DIR", "/profile")),
            port=int(env.get("PORT", "8100")),
            quantity=max(1, min(10, int(env.get("BOOKER_QUANTITY", "1")))),
            preopen_s=int(env.get("BOOKER_PREOPEN_S", "90")),
            poll_ms=int(env.get("BOOKER_POLL_MS", "300")),
            reload_every_s=float(env.get("BOOKER_RELOAD_EVERY_S", "2.5")),
            window_s=int(env.get("BOOKER_WINDOW_S", "180")),
            display=env.get("DISPLAY", ":99"),
            vnc_public_url=env.get("VNC_PUBLIC_URL", "http://192.168.1.100:6080/vnc.html"),
            vnc_password=env.get("VNC_PASSWORD", ""),
            login_timeout_s=int(env.get("LOGIN_TIMEOUT_S", "900")),
            base_url=env.get("EVENTBRITE_BASE_URL", "https://www.eventbrite.es").rstrip("/"),
            tz=ZoneInfo(env.get("TZ", "Europe/Madrid")),
        )

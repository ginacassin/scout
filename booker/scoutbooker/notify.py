"""Telegram messages sent straight from the booker via the Bot API.

Only sendMessage and sendPhoto: never getUpdates, because Scout long-polls with the same token.
"""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path

import httpx

from . import eb_selectors as S
from .jobs import Job

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, bot_token: str, chat_id: int, tz, base_url: str = "https://www.eventbrite.es",  # noqa: ANN001
                 client: httpx.AsyncClient | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.tz = tz
        self.base_url = base_url
        self.client = client or httpx.AsyncClient(timeout=20)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    async def send(self, text: str, buttons: list[list[dict]] | None = None) -> None:
        if not self.enabled:
            log.warning("Telegram not configured; would send: %s", text)
            return
        data = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            data["reply_markup"] = {"inline_keyboard": buttons}
        try:
            r = await self.client.post(self._url("sendMessage"), json=data)
            if r.status_code != 200:
                log.error("sendMessage failed: %s %s", r.status_code, r.text[:300])
        except httpx.HTTPError:
            log.exception("sendMessage failed")

    async def photo(self, path: Path | None, caption: str) -> None:
        if not self.enabled or not path or not Path(path).exists():
            return
        try:
            with open(path, "rb") as f:
                r = await self.client.post(
                    self._url("sendPhoto"),
                    data={"chat_id": str(self.chat_id), "caption": caption[:1000], "parse_mode": "HTML"},
                    files={"photo": (Path(path).name, f, "image/png")},
                )
            if r.status_code != 200:
                log.error("sendPhoto failed: %s %s", r.status_code, r.text[:300])
        except httpx.HTTPError:
            log.exception("sendPhoto failed")

    def _when(self, job: Job) -> str:
        return job.opens.astimezone(self.tz).strftime("%a %-d %b %H:%M")

    def manual_buttons(self, job: Job) -> list[list[dict]]:
        row = []
        if job.event_url:
            row.append({"text": "🎟 Event page", "url": job.event_url})
        row.append({"text": "🛒 Checkout", "url": S.checkout_external_url(job.eventbrite_id, self.base_url)})
        return [row]

    async def armed(self, job: Job, logged_in: bool | None) -> None:
        text = (f"🤖 Auto-book armed for <b>{escape(job.title)}</b> — opens {self._when(job)}"
                f" ({job.quantity} ticket{'s' if job.quantity != 1 else ''}, free only).")
        if logged_in is False:
            text += "\n⚠️ The Eventbrite session has expired. Send /autobook login before then."
        await self.send(text)

    async def booked(self, job: Job, screenshot: Path | None) -> None:
        text = f"✅ Booked! <b>{escape(job.title)}</b>\nCheck your email for the tickets."
        if screenshot and Path(screenshot).exists():
            await self.photo(screenshot, text)
        else:
            await self.send(text)

    async def manual(self, job: Job, code: str, detail: str = "", maybe_done: bool = False) -> None:
        text = (f"⚠️ Couldn't auto-book <b>{escape(job.title)}</b> ({escape(S.reason_text(code))}) — "
                f"<b>book manually NOW</b>")
        if detail:
            text += f"\n<i>{escape(detail[:200])}</i>"
        if maybe_done:
            text += "\nIt got as far as submitting the order: check your email / My tickets before booking again."
        await self.send(text, self.manual_buttons(job))


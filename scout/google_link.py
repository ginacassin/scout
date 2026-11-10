"""Link your own Google Drive so tickets and note photos can be attached to calendar entries.

Google Calendar only attaches Google Drive files, and a service account has no Drive storage on a
personal @gmail.com account. So Scout uploads into *your* Drive instead, with the narrowest scope
there is (drive.file: Scout only sees the files it creates itself), into a folder called "Scout"
that is shared read-only with the calendar's service account.

One-time setup (README): an OAuth client of type "TVs and Limited Input devices" in the same Google
Cloud project → GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET in Dokploy → /google in Telegram,
which shows a code to enter at google.com/device. Publish the OAuth app ("In production") so the link
doesn't expire after 7 days; drive.file needs no Google verification.

The refresh token lives in the settings table on the /data volume. /google off forgets it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import time as _time
import uuid
from pathlib import Path
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from .db import Database

log = logging.getLogger(__name__)

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.file"
REFRESH_KEY = "google_user_refresh"
FOLDER_KEY = "google_user_folder"
SHARED_KEY = "google_user_folder_shared"
EXPIRED_KEY = "google_user_expired_warned"
FOLDER_NAME = "Scout"


class DriveError(Exception):
    def __init__(self, status: int | None, message: str, reason: str | None = None):
        super().__init__(f"{status}: {message}")
        self.status, self.reason = status, reason


class UserDrive:
    def __init__(self, settings, db: Database, client: httpx.AsyncClient | None = None,  # noqa: ANN001
                 sa_email: str | None = None):
        self.settings = settings
        self.db = db
        self.client = client or httpx.AsyncClient(timeout=30)
        self.client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        self.sa_email = sa_email
        self._access: tuple[str, float] | None = None
        self._lock = asyncio.Lock()
        self.on_expired = None  # async callable, set by the bot to warn once

    # ---------------------------------------------------------------- state
    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def linked(self) -> bool:
        return self.configured and bool(self.db.get_setting(REFRESH_KEY))

    def unlink(self) -> None:
        self.db.set_setting(REFRESH_KEY, "")
        self.db.set_setting(FOLDER_KEY, "")
        self.db.set_setting(SHARED_KEY, "")
        self._access = None

    # --------------------------------------------------------------- tokens
    async def token(self) -> str:
        async with self._lock:
            if self._access and self._access[1] > _time.time() + 60:
                return self._access[0]
            refresh = self.db.get_setting(REFRESH_KEY)
            if not refresh:
                raise DriveError(None, "Google Drive not linked", "not_linked")
            r = await self.client.post(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": refresh, "grant_type": "refresh_token"})
            data = r.json() if r.content else {}
            if r.status_code >= 400:
                err = data.get("error", "")
                if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
                    log.warning("Google Drive link no longer valid (%s); /google to link again", err)
                    self.db.set_setting(REFRESH_KEY, "")
                    if self.on_expired and self.db.get_setting(EXPIRED_KEY) != "1":
                        self.db.set_setting(EXPIRED_KEY, "1")
                        await self.on_expired()
                raise DriveError(r.status_code, data.get("error_description") or err or r.text[:200], err)
            self._access = (data["access_token"], _time.time() + int(data.get("expires_in", 3600)))
            return self._access[0]

    def invalidate(self) -> None:
        self._access = None

    async def start_device(self) -> dict[str, Any]:
        r = await self.client.post(DEVICE_URL, data={"client_id": self.client_id, "scope": SCOPE})
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise DriveError(r.status_code, data.get("error_description") or data.get("error") or r.text[:200],
                             data.get("error"))
        return data

    async def poll_device(self, device_code: str, interval: int, expires_in: int, sleep=asyncio.sleep) -> bool:  # noqa: ANN001
        """Wait for you to approve at google.com/device. True once linked."""
        deadline = _time.monotonic() + expires_in
        wait = max(5, interval)
        while _time.monotonic() < deadline:
            await sleep(wait)
            r = await self.client.post(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret, "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            data = r.json() if r.content else {}
            if r.status_code < 400 and data.get("refresh_token"):
                self.db.set_setting(REFRESH_KEY, data["refresh_token"])
                self.db.set_setting(EXPIRED_KEY, "0")
                self._access = (data["access_token"], _time.time() + int(data.get("expires_in", 3600)))
                return True
            err = data.get("error")
            if err == "slow_down":
                wait += 5
            elif err != "authorization_pending":
                log.warning("Google device sign-in ended: %s", err or r.status_code)
                return False
        return False

    # ---------------------------------------------------------------- Drive
    async def _call(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {await self.token()}", **kw.pop("headers", {})}
            r = await self.client.request(method, url, headers=headers, **kw)
            if r.status_code == 401 and attempt == 1:
                self.invalidate()
                continue
            if r.status_code >= 400:
                try:
                    err = r.json().get("error", {})
                    msg, reason = err.get("message", ""), next(
                        (e.get("reason") for e in err.get("errors") or [] if e.get("reason")), None)
                except (ValueError, AttributeError):
                    msg, reason = r.text[:200], None
                raise DriveError(r.status_code, msg, reason)
            return r.json() if r.content else {}
        raise AssertionError("unreachable")

    async def folder(self) -> str:
        fid = self.db.get_setting(FOLDER_KEY)
        if not fid:
            res = await self._call("POST", DRIVE_FILES, params={"fields": "id"},
                                   json={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"})
            fid = res["id"]
            self.db.set_setting(FOLDER_KEY, fid)
            self.db.set_setting(SHARED_KEY, "")
        if self.sa_email and self.db.get_setting(SHARED_KEY) != self.sa_email:
            try:  # the calendar's service account attaches the files, so it may read them
                await self._call("POST", f"{DRIVE_FILES}/{fid}/permissions",
                                 params={"sendNotificationEmail": "false"},
                                 json={"type": "user", "role": "reader", "emailAddress": self.sa_email})
                self.db.set_setting(SHARED_KEY, self.sa_email)
            except DriveError as exc:
                log.warning("Couldn't share the Scout Drive folder with the service account: %s", exc)
        return fid

    async def upload(self, path: Path | str, title: str) -> dict[str, Any]:
        path = Path(path)
        folder = await self.folder()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        boundary = f"scout-{uuid.uuid4().hex}"
        meta = json.dumps({"name": title, "parents": [folder], "mimeType": mime})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + path.read_bytes() + \
            f"\r\n--{boundary}--\r\n".encode()
        try:
            res = await self._call("POST", DRIVE_UPLOAD, content=body,
                                   params={"uploadType": "multipart", "fields": "id,name,mimeType,webViewLink"},
                                   headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        except DriveError as exc:
            if exc.status == 404:  # folder deleted by hand: make a new one and retry once
                self.db.set_setting(FOLDER_KEY, "")
                return await self.upload(path, title)
            raise
        fid = res["id"]
        return {"fileId": fid, "title": res.get("name") or title, "mimeType": res.get("mimeType") or mime,
                "fileUrl": res.get("webViewLink") or f"https://drive.google.com/open?id={fid}"}

    async def delete(self, file_id: str) -> None:
        try:
            await self._call("DELETE", f"{DRIVE_FILES}/{file_id}")
        except DriveError as exc:
            if exc.status not in (404, 410):
                log.info("Couldn't delete Drive file %s: %s", file_id, exc)


# ------------------------------------------------------------------- Telegram
class GoogleLinkCommands:
    def __init__(self, app: Application, settings, db: Database, drive: UserDrive):  # noqa: ANN001
        self.app = app
        self.settings = settings
        self.db = db
        self.drive = drive
        self._task: asyncio.Task | None = None

    def register(self) -> None:
        self.app.add_handler(CommandHandler("google", self.cmd_google,
                                            filters=filters.Chat(chat_id=self.settings.allowed_chat_id)))
        self.drive.on_expired = self._warn_expired

    async def _send(self, text: str, **kw: Any) -> None:
        try:
            await self.app.bot.send_message(self.settings.allowed_chat_id, text, parse_mode=ParseMode.HTML,
                                            disable_web_page_preview=True, **kw)
        except Exception:  # noqa: BLE001
            log.exception("Couldn't send Google link message")

    async def _warn_expired(self) -> None:
        await self._send("⚠️ Your Google Drive link has expired, so new tickets and note photos can't be attached "
                         "to the calendar (I'll pin tickets here meanwhile). Send /google to link it again.")

    async def cmd_google(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        arg = (ctx.args or [""])[0].lower()
        if arg in ("off", "unlink", "stop"):
            self.drive.unlink()
            await msg.reply_text("Google Drive unlinked. Files already in your Drive's Scout folder stay there.")
            return
        if not self.drive.configured:
            await msg.reply_html(
                "To attach tickets and note photos to calendar entries I need to put them in your Google Drive.\n"
                "One-time setup in Google Cloud (project of the calendar service account):\n"
                "1. <b>APIs &amp; Services → Library</b>: enable <i>Google Drive API</i>.\n"
                "2. <b>OAuth consent screen</b>: External, any name, your email; then <b>Publish app</b> "
                "(otherwise the link expires every 7 days).\n"
                "3. <b>Credentials → Create credentials → OAuth client ID</b>, type "
                "<i>TVs and Limited Input devices</i>.\n"
                "4. Put its ID and secret in Dokploy → scout-bot → Environment as "
                "<code>GOOGLE_OAUTH_CLIENT_ID</code> and <code>GOOGLE_OAUTH_CLIENT_SECRET</code>, redeploy.\n"
                "5. Send /google again.")
            return
        if self.drive.linked and arg != "relink":
            await msg.reply_html("✅ Google Drive is linked: tickets and note photos go into the <b>Scout</b> folder "
                                 "of your Drive and are attached to the calendar entries.\n"
                                 "<code>/google off</code> to unlink, <code>/google relink</code> to link again.")
            return
        try:
            dev = await self.drive.start_device()
        except Exception as exc:  # noqa: BLE001
            log.warning("Google device sign-in failed to start: %s", exc)
            await msg.reply_text(f"Google refused to start the sign-in ({exc}). Check the OAuth client is of type "
                                 "'TVs and Limited Input devices'.")
            return
        url = dev.get("verification_url") or dev.get("verification_uri") or "https://www.google.com/device"
        await msg.reply_html(
            f"1. Open <b>{url}</b>\n2. Enter the code <code>{dev['user_code']}</code>\n"
            "3. Choose your Google account and allow access to <i>files Scout creates</i>.\n"
            f"I'll wait {int(dev.get('expires_in', 1800)) // 60} minutes.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open google.com/device", url=url)]]))
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.get_running_loop().create_task(
            self._wait(dev["device_code"], int(dev.get("interval", 5)), int(dev.get("expires_in", 1800))))

    async def _wait(self, device_code: str, interval: int, expires_in: int) -> None:
        try:
            ok = await self.drive.poll_device(device_code, interval, expires_in)
        except Exception:  # noqa: BLE001
            log.exception("Google device sign-in failed")
            ok = False
        if not ok:
            await self._send("Google sign-in didn't complete. Send /google to try again.")
            return
        try:
            await self.drive.folder()
        except Exception as exc:  # noqa: BLE001
            log.warning("Scout folder creation failed: %s", exc)
        log.info("Google Drive linked")
        await self._send("✅ Google Drive linked. Tickets and note photos now go on your calendar entries "
                         "(folder <b>Scout</b> in your Drive). Adding the ones you already have…")
        cal = self.app.bot_data.get("calendar")
        if cal is not None:
            n = await cal.backfill_attachments()
            if n:
                await self._send(f"📎 Attached files to {n} calendar entr{'y' if n == 1 else 'ies'}.")


def setup_google_link(app: Application, settings, db: Database, sa_email: str | None = None) -> UserDrive:  # noqa: ANN001
    drive = UserDrive(settings, db, sa_email=sa_email)
    GoogleLinkCommands(app, settings, db, drive).register()
    app.bot_data["google_drive"] = drive
    return drive

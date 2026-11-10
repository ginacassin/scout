"""Your own Google Drive (/google): device sign-in, uploads, and files attached to calendar entries."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from scout import bookings
from scout.db import Database, Event
from scout.google_link import FOLDER_KEY, REFRESH_KEY, UserDrive
from tests.test_bookings_flow import DriveGoogle, eventbrite_event, png, slot, world  # noqa: F401
from tests.test_calendar import dates


class OAuth:
    """Google's OAuth + Drive endpoints for the user's account."""

    def __init__(self):
        self.pending = 2
        self.files = []
        self.perms = []
        self.deleted = []
        self.refresh_error = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        form = parse_qs(request.content.decode()) if path in ("/token", "/device/code") else {}
        if path == "/device/code":
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-EFGH", "interval": 5,
                                             "expires_in": 1800, "verification_url": "https://www.google.com/device"})
        if path == "/token":
            grant = form.get("grant_type", [""])[0]
            if grant.endswith("device_code"):
                if self.pending:
                    self.pending -= 1
                    return httpx.Response(428, json={"error": "authorization_pending"})
                return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
            if self.refresh_error:
                return httpx.Response(400, json={"error": self.refresh_error})
            return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})
        if path == "/drive/v3/files" and request.method == "POST":
            return httpx.Response(200, json={"id": "folder1"})
        if path.endswith("/permissions"):
            self.perms.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "p"})
        if path.startswith("/upload/drive/v3/files"):
            n = len(self.files) + 1
            self.files.append(request)
            return httpx.Response(200, json={"id": f"u{n}", "name": f"file{n}", "mimeType": "image/jpeg",
                                             "webViewLink": f"https://drive.google.com/file/d/u{n}/view"})
        if request.method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture
def drive(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    oauth = OAuth()
    db = Database(":memory:")
    d = UserDrive(SimpleNamespace(), db, httpx.AsyncClient(transport=httpx.MockTransport(oauth.handler)),
                  sa_email="sa@x")
    return d, oauth, db


async def no_sleep(_s):
    return None


async def test_device_sign_in_then_folder_shared_with_service_account(drive):
    d, oauth, db = drive
    assert d.configured and not d.linked
    dev = await d.start_device()
    assert dev["user_code"] == "ABCD-EFGH"
    assert await d.poll_device("dc", 5, 1800, sleep=no_sleep)
    assert d.linked and db.get_setting(REFRESH_KEY) == "rt"
    assert await d.folder() == "folder1"
    assert oauth.perms == [{"type": "user", "role": "reader", "emailAddress": "sa@x"}]
    await d.folder()
    assert len(oauth.perms) == 1  # shared once


async def test_expired_link_is_forgotten_and_warned_once(drive):
    d, oauth, db = drive
    db.set_setting(REFRESH_KEY, "rt")
    oauth.refresh_error = "invalid_grant"
    warned = AsyncMock()
    d.on_expired = warned
    with pytest.raises(Exception):
        await d.token()
    assert not d.linked and warned.await_count == 1


async def test_calendar_attaches_ticket_and_note_photos_to_your_drive(world, drive, tmp_path):  # noqa: F811
    w = world
    d, oauth, _ = drive
    d.db = w.db
    w.db.set_setting(REFRESH_KEY, "rt")
    w.db.set_setting(FOLDER_KEY, "folder1")
    w.sync.user_drive = d
    assert w.sync.can_attach
    ev_id = eventbrite_event(w.db, dates=dates(13))
    await bookings.record_booking(w.db, ev_id, slot(), via="auto", ticket_path=png(w.tmp))
    await w.sync.drain()
    e = w.google.by_key()["booked"]
    assert [a["fileId"] for a in e["attachments"]] == ["u1"]
    assert w.app.bot.named("pin_chat_message") == []  # attached instead of pinned
    assert w.google.uploads == []  # the service account uploaded nothing

    photo = tmp_path / "notes" / "lamp.jpg"
    photo.parent.mkdir(exist_ok=True)
    photo.write_bytes(b"jpg")
    await bookings.add_note(w.db, ev_id, "⭐ brass lamp (photo 2)", image=photo, peek=9)
    await w.sync.drain()
    e = w.google.by_key()["booked"]
    assert [a["fileId"] for a in e["attachments"]] == ["u1", "u2"]
    assert "brass lamp (photo 2) 📷 (photo attached)" in e["description"]

    await bookings.remove_notes(w.db, ev_id, peek=9)
    await w.sync.drain()
    e = w.google.by_key()["booked"]
    assert [a["fileId"] for a in e["attachments"]] == ["u1"]
    assert oauth.deleted == ["u2"] and not photo.exists()


async def test_photo_reply_to_card_routes_to_note_or_asks(world, tmp_path):  # noqa: F811
    w = world
    ev_id = eventbrite_event(w.db, dates=dates(13))

    async def download(path):
        path.write_bytes(b"jpeg")

    def photo_msg(caption, mid):
        msg = MagicMock()
        msg.caption = caption
        msg.message_id = mid
        msg.reply_to_message = SimpleNamespace(message_id=777)
        msg.photo = [SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(download_to_drive=download)))]
        msg.document = None
        msg.reply_html = AsyncMock()
        return msg

    msg = photo_msg("the green chair", 1)
    assert w.cmds.wants_file(msg)
    await w.cmds.on_file(SimpleNamespace(effective_message=msg), None)
    with w.db.session() as s:
        n = s.get(Event, ev_id).note_list[0]
    assert n["text"] == "the green chair" and n["image"].endswith(f"{ev_id}_1.jpg")

    msg = photo_msg(None, 2)  # no caption: ticket or note?
    await w.cmds.on_file(SimpleNamespace(effective_message=msg), None)
    assert "Is this the ticket" in msg.reply_html.await_args.args[0]
    assert w.cmds.pending["kind"] == "file"
    q = MagicMock()
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.message = MagicMock()
    q.message.reply_html = AsyncMock()
    await w.cmds._cb_file(q, "ft", ev_id)
    with w.db.session() as s:
        assert s.get(Event, ev_id).ticket_path.endswith(f"tickets/{ev_id}.jpg")

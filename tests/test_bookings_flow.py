"""Booking records: calendar swap to one booked entry, /unbook restore, ticket QR on Drive or pinned
in Telegram, /booked and the "🎟 I booked it" button flow, merged events."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote

import httpx
import pytest

from scout import bookings, hooks
from scout.bot import templates as T
from scout.bot.booking_cmds import BookingCommands, parse_day, slot_times
from scout.db import BookingWatch, Database, Event
from scout.gcal import CalendarSync, load_state
from tests.test_calendar import CAL, NOW, TZ, FakeGoogle, FakeTokens, add_event, dates, make_settings

FOLDER = "0AFolderInSharedDrive"


class DriveGoogle(FakeGoogle):
    """FakeGoogle plus the Drive multipart upload endpoint."""

    def __init__(self):
        super().__init__()
        self.uploads: list[httpx.Request] = []
        self.drive_error: dict | None = None
        self.params: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if path.startswith("/upload/drive/v3/files"):
            self.uploads.append(request)
            if self.drive_error:
                return httpx.Response(403, json={"error": self.drive_error})
            return httpx.Response(200, json={"id": "f1", "name": "Scout ticket #1.png", "mimeType": "image/png",
                                             "webViewLink": "https://drive.google.com/file/d/f1/view"})
        assert "drive" not in path and "permissions" not in path  # never shares anything
        if request.method in ("POST", "PATCH"):
            self.params.append(dict(request.url.params))
        return super().handler(request)


class FakeBot:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._ids = iter(range(1000, 2000))

    def __getattr__(self, name):
        async def method(*args, **kw):
            self.calls.append((name, args, kw))
            if name in ("send_message", "send_photo", "send_document"):
                return SimpleNamespace(message_id=next(self._ids))
            if name == "get_my_commands":
                return []
            return True
        return method

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeJob:
    def __init__(self, name, when, data):
        self.name, self.when, self.data, self.removed = name, when, data, False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.scheduled: list[FakeJob] = []

    def run_once(self, cb, when, name=None, data=None):
        self.scheduled.append(FakeJob(name, when, data))

    def jobs(self):
        return [j for j in self.scheduled if not j.removed]


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data: dict = {}
        self.job_queue = FakeJobQueue()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("GDRIVE_FOLDER_ID", raising=False)
    hooks.clear()
    T.EXTRA_ANSWERED_BUTTONS.clear()
    google = DriveGoogle()
    client = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    db = Database(":memory:")
    app = FakeApp()
    settings = make_settings(data_dir=tmp_path)
    sync = CalendarSync(settings, db, app, client=client, tokens=FakeTokens(), sa_email="sa@x", now=lambda: NOW)
    sync.register_hooks()
    app.bot_data["calendar"] = sync
    cmds = BookingCommands(app, settings, db, now=lambda: NOW)
    cmds.register_hooks()
    yield SimpleNamespace(sync=sync, db=db, google=google, app=app, cmds=cmds, tmp=tmp_path)
    hooks.clear()
    T.EXTRA_ANSWERED_BUTTONS.clear()


def slot(day=13, h=10, m=30):
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


def eventbrite_event(db, **kw):
    opens = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    return add_event(db, dates=kw.pop("dates", dates(13, 14, 15)), address="Calle Almagro 3, Madrid",
                     booking_method="eventbrite", booking_url="https://www.eventbrite.es/e/123",
                     booking_opens_at=opens, **kw)


def command(args):
    update = MagicMock()
    update.effective_chat = SimpleNamespace(id=42)
    update.effective_message.reply_html = AsyncMock()
    update.effective_message.reply_text = AsyncMock()
    return update, SimpleNamespace(args=args, application=MagicMock())


def callback(data, message_id=5000):
    q = MagicMock()
    q.data = data
    q.message = SimpleNamespace(message_id=message_id)
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = q
    update.effective_chat = SimpleNamespace(id=42)
    return update, q


def png(tmp, name="qr.png"):
    p = tmp / name
    p.write_bytes(b"\x89PNG fake qr")
    return p


# ------------------------------------------------------------------ calendar swap
async def test_record_booking_swaps_calendar_to_single_booked_event(world):
    w = world
    ev_id = eventbrite_event(w.db)
    assert await w.sync.sync(ev_id)
    assert set(w.google.by_key()) == {"2026-11-13..2026-11-15", "booking"}
    with w.db.session() as s:
        s.add(BookingWatch(event_id=ev_id, event_url="https://www.eventbrite.es/e/alma-123"))
        s.get(Event, ev_id).add_note("green lamp in the living room")

    await bookings.record_booking(w.db, ev_id, slot(), via="auto")
    await w.sync.drain()

    keys = w.google.by_key()
    assert set(keys) == {"booked"}
    e = keys["booked"]
    assert e["summary"] == "🎟 Alma Market — Chamberí — booked 10:30"
    assert e["start"] == {"dateTime": "2026-11-13T10:30:00", "timeZone": "Europe/Madrid"}
    assert e["end"] == {"dateTime": "2026-11-13T11:00:00", "timeZone": "Europe/Madrid"}
    assert e["location"] == "Calle Almagro 3, Madrid"
    for part in ("https://www.instagram.com/p/ABC/", "https://www.eventbrite.es/e/alma-123",
                 "eventbrite.es/mytickets", "• green lamp in the living room", "Booked automatically by Scout"):
        assert part in e["description"]
    with w.db.session() as s:
        raw = json.loads(s.get(Event, ev_id).gcal_event_id)
    assert raw == {"days": {}, "booking": None, "booked": e["id"]}
    texts = [c[2].get("text") or (c[1][1] if len(c[1]) > 1 else "") for c in w.app.bot.named("send_message")]
    assert any("only your booked slot is left — Fri 13 Nov 10:30" in t for t in texts)
    # the card was redrawn with the booked line
    edit = w.app.bot.named("edit_message_caption")[-1][2]
    assert "Booked Fri 13 Nov 10:30</b> (auto)" in edit["caption"]


async def test_unbook_restores_all_dates(world):
    w = world
    ev_id = eventbrite_event(w.db)
    assert await w.sync.sync(ev_id)
    await bookings.record_booking(w.db, ev_id, slot(), via="manual")
    await w.sync.drain()
    assert set(w.google.by_key()) == {"booked"}
    assert "Booked by you" in w.google.by_key()["booked"]["description"]

    update, ctx = command([str(ev_id)])
    await w.cmds.cmd_unbook(update, ctx)
    await w.sync.drain()
    assert set(w.google.by_key()) == {"2026-11-13..2026-11-15", "booking"}
    with w.db.session() as s:
        ev = s.get(Event, ev_id)
        assert ev.booked_start is None and ev.booked_via is None
        assert "booked" not in load_state(ev.gcal_event_id)
    assert "calendar shows all dates again" in update.effective_message.reply_html.await_args.args[0]


async def test_events_merged_removes_merged_entries(world):
    w = world
    keep = add_event(w.db, dates=dates(13), title="Alma")
    dup = add_event(w.db, dates=dates(13), title="Alma market")
    assert await w.sync.sync(keep) and await w.sync.sync(dup)
    assert len(w.google.events) == 2
    with w.db.session() as s:  # matching may already have deleted the row: we still find it in Google
        s.delete(s.get(Event, dup))
    await hooks.emit("events_merged", keep, merged_id=dup)
    await w.sync.drain()
    assert [e["extendedProperties"]["private"]["scoutId"] for e in w.google.events.values()] == [str(keep)]


# ------------------------------------------------------------------- ticket QR
async def test_ticket_uploaded_to_drive_and_attached(world, monkeypatch):
    w = world
    monkeypatch.setenv("GDRIVE_FOLDER_ID", FOLDER)
    ev_id = eventbrite_event(w.db, dates=dates(13))
    qr = png(w.tmp)
    await bookings.record_booking(w.db, ev_id, slot(), via="auto", ticket_path=qr)
    await w.sync.drain()

    up = w.google.uploads[0]
    assert up.url.params["uploadType"] == "multipart" and up.url.params["supportsAllDrives"] == "true"
    assert up.headers["content-type"].startswith("multipart/related; boundary=")
    meta = json.loads(up.content.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0])
    assert meta["parents"] == [FOLDER] and meta["mimeType"] == "image/png"
    assert b"fake qr" in up.content

    e = w.google.by_key()["booked"]
    assert e["attachments"] == [{"fileUrl": "https://drive.google.com/file/d/f1/view", "title": "Scout ticket #1.png",
                                 "mimeType": "image/png", "fileId": "f1"}]
    assert {"supportsAttachments": "true"} in w.google.params
    assert "Ticket QR: in your Scout chat (pinned)" in e["description"]
    # Telegram copy is always made too
    assert len(w.app.bot.named("pin_chat_message")) == 1


async def test_drive_quota_exceeded_falls_back_to_telegram(world, monkeypatch):
    w = world
    monkeypatch.setenv("GDRIVE_FOLDER_ID", "folder-in-my-drive")
    w.google.drive_error = {"code": 403, "message": "Service Accounts do not have storage quota.",
                            "errors": [{"reason": "storageQuotaExceeded"}]}
    ev_id = eventbrite_event(w.db, dates=dates(13))
    await bookings.record_booking(w.db, ev_id, slot(), via="auto", ticket_path=png(w.tmp))
    await w.sync.drain()

    e = w.google.by_key()["booked"]
    assert "attachments" not in e
    assert "Ticket QR: in your Scout chat (pinned)" in e["description"]
    infos = [c for c in w.app.bot.named("send_message") if "Shared Drive" in str(c)]
    assert len(infos) == 1
    assert w.db.get_setting("gdrive_quota_warned") == "1"
    # second ticket: no second warning
    await bookings.attach_ticket(w.db, ev_id, png(w.tmp, "qr2.png"))
    await w.sync.drain()
    assert len([c for c in w.app.bot.named("send_message") if "Shared Drive" in str(c)]) == 1


async def test_telegram_pin_fallback_without_drive(world):
    w = world
    ev_id = eventbrite_event(w.db, dates=dates(13))
    pdf = w.tmp / "ticket.pdf"
    pdf.write_bytes(b"%PDF fake")
    await bookings.record_booking(w.db, ev_id, slot(), via="auto", ticket_path=pdf)
    await w.sync.drain()
    assert w.google.uploads == []  # no folder configured

    doc = w.app.bot.named("send_document")
    assert len(doc) == 1
    assert doc[0][2]["reply_parameters"].message_id == 777  # reply to the event card
    assert doc[0][2]["caption"] == "🎟 Ticket: Alma Market — Chamberí — Fri 13 Nov 10:30"
    pin = w.app.bot.named("pin_chat_message")
    assert pin[0][1] == (42, 1000) and pin[0][2]["disable_notification"] is True
    # same file again (ticket_added for the same path) does not post a second copy
    await bookings.attach_ticket(w.db, ev_id, pdf)
    await w.sync.drain()
    assert len(w.app.bot.named("send_document")) == 1


# ---------------------------------------------------------------- /booked + buttons
async def test_booked_command_single_date_then_ticket_photo(world):
    w = world
    ev_id = eventbrite_event(w.db, dates=dates(13))
    update, ctx = command([str(ev_id), "10:30"])
    await w.cmds.cmd_booked(update, ctx)
    await w.sync.drain()
    with w.db.session() as s:
        ev = s.get(Event, ev_id)
        assert ev.booked_start == slot() and ev.booked_via == "manual"
    reply = update.effective_message.reply_html.await_args
    assert "Send me the QR or ticket PDF (optional)" in reply.args[0]
    assert w.cmds.pending["kind"] == "ticket"

    # next photo in the chat is the ticket
    async def download(path):
        path.write_bytes(b"jpeg")
    tg_file = SimpleNamespace(download_to_drive=download)
    msg = MagicMock()
    msg.caption = None
    msg.reply_to_message = None
    msg.photo = [SimpleNamespace(get_file=AsyncMock(return_value=tg_file))]
    msg.reply_html = AsyncMock()
    assert w.cmds.wants_file(msg)
    upd = MagicMock()
    upd.effective_message = msg
    await w.cmds.on_file(upd, None)
    await w.sync.drain()
    path = w.tmp / "tickets" / f"{ev_id}.jpg"
    assert path.read_bytes() == b"jpeg"
    with w.db.session() as s:
        assert s.get(Event, ev_id).ticket_path == str(path)
    assert w.app.bot.named("send_photo") and w.app.bot.named("pin_chat_message")
    assert w.cmds.pending is None


async def test_ticket_caption_anytime(world):
    w = world
    ev_id = eventbrite_event(w.db, dates=dates(13))
    msg = MagicMock()
    msg.caption = f"/ticket {ev_id}"
    msg.reply_to_message = None
    msg.photo = None
    msg.document = SimpleNamespace(file_name="Order 123.pdf", mime_type="application/pdf",
                                   get_file=AsyncMock(return_value=SimpleNamespace(
                                       download_to_drive=AsyncMock(side_effect=lambda p: p.write_bytes(b"%PDF")))))
    msg.reply_html = AsyncMock()
    assert w.cmds.wants_file(msg)
    upd = MagicMock()
    upd.effective_message = msg
    await w.cmds.on_file(upd, None)
    with w.db.session() as s:
        assert s.get(Event, ev_id).ticket_path.endswith(f"tickets/{ev_id}.pdf")
    assert "isn't marked as booked yet" in msg.reply_html.await_args.args[0]


async def test_booked_command_multi_day_asks_for_day(world):
    w = world
    ev_id = eventbrite_event(w.db)
    update, ctx = command([str(ev_id), "11:00"])
    await w.cmds.cmd_booked(update, ctx)
    kb = update.effective_message.reply_html.await_args.kwargs["reply_markup"]
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"bk:t:{ev_id}:20261114:1100" in data and "bk:x" in data
    upd, q = callback(f"bk:t:{ev_id}:20261114:1100")
    await w.cmds.on_callback(upd, None)
    with w.db.session() as s:
        assert s.get(Event, ev_id).booked_start == slot(14, 11, 0)


async def test_i_booked_it_button_walkthrough(world):
    w = world
    sessions = [{"date": "2026-11-14", "start": "10:00", "end": "12:00", "booking_required": True},
                {"date": "2026-11-14", "start": "16:00", "end": "20:00", "booking_required": False}]
    ev_id = eventbrite_event(w.db, dates=dates(13, 14), sessions=json.dumps(sessions))
    with w.db.session() as s:
        ev = s.get(Event, ev_id)
    kb = T.answered_keyboard(ev)
    data = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert f"bk:b:{ev_id}" in data and f"nt:{ev_id}" in data

    upd, q = callback(f"bk:b:{ev_id}")
    await w.cmds.on_callback(upd, None)
    sent = w.app.bot.named("send_message")[-1]
    days = [b.callback_data for row in sent[2]["reply_markup"].inline_keyboard for b in row]
    assert days[:2] == [f"bk:d:{ev_id}:20261113", f"bk:d:{ev_id}:20261114"]

    upd, q = callback(f"bk:d:{ev_id}:20261114")
    await w.cmds.on_callback(upd, None)
    times_kb = q.edit_message_text.await_args.kwargs["reply_markup"]
    times = [b.text for row in times_kb.inline_keyboard for b in row]
    assert times == ["10:00", "10:30", "11:00", "11:30", "⌨️ Type it", "✖ Cancel"]  # bookable session only

    upd, q = callback(f"bk:k:{ev_id}:20261114")
    await w.cmds.on_callback(upd, None)
    assert w.cmds.pending["kind"] == "time"
    msg = MagicMock()
    msg.text = "12:15"
    msg.reply_to_message = None
    msg.reply_html = AsyncMock()
    assert w.cmds.wants_text(msg)
    upd = MagicMock()
    upd.effective_message = msg
    await w.cmds.on_text(upd, None)
    with w.db.session() as s:
        ev = s.get(Event, ev_id)
    assert ev.booked_start == slot(14, 12, 15) and ev.booked_via == "manual"
    assert "🎟 <b>Booked Sat 14 Nov 12:15</b> (by you)" in T.event_caption(ev, "almamarket_home", TZ)
    # once booked the button goes away
    data = [b.callback_data for row in T.answered_keyboard(ev).inline_keyboard for b in row if b.callback_data]
    assert f"bk:b:{ev_id}" not in data


def test_parsers():
    days = [date(2026, 11, 13), date(2026, 11, 14)]
    today = date(2026, 11, 8)
    assert parse_day("14", days, today) == date(2026, 11, 14)
    assert parse_day("13/11", days, today) == date(2026, 11, 13)
    assert parse_day("sat", days, today) == date(2026, 11, 14)
    assert parse_day("2026-12-01", days, today) == date(2026, 12, 1)
    assert parse_day("5/1", days, today) == date(2027, 1, 5)
    assert parse_day("nope", days, today) is None
    ev = Event(title="x", sessions="[]", dates="[]")
    assert slot_times(ev, date(2026, 11, 13))[0] == "10:00" and len(slot_times(ev, date(2026, 11, 13))) == 16

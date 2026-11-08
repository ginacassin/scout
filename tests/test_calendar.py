"""Google Calendar sync against a fake Google (httpx.MockTransport)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from itertools import count
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
import pytest

from scout import hooks
from scout.config import Settings
from scout.db import Database, Event
from scout.gcal import CalendarSync, ServiceAccountTokens, load_state, setup_calendar

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
CAL = "scout123@group.calendar.google.com"


class FakeGoogle:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_status: int | None = None
        self.token_requests = 0
        self._ids = count(1)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if request.url.host == "oauth2.googleapis.com":
            self.token_requests += 1
            assert b"assertion=" in request.content
            return httpx.Response(200, json={"access_token": f"tok{self.token_requests}", "expires_in": 3600})
        self.calls.append((request.method, path))
        assert request.headers["authorization"].startswith("Bearer ")
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": {"message": "nope"}})
        base = f"/calendar/v3/calendars/{CAL}/events"
        assert path.startswith(base), path
        rest = path[len(base):].strip("/")
        if request.method == "GET":
            prop = request.url.params.get("privateExtendedProperty")
            items = list(self.events.values())
            if prop:
                k, v = prop.split("=")
                items = [e for e in items if e["extendedProperties"]["private"].get(k) == v]
            return httpx.Response(200, json={"items": items})
        if request.method == "POST":
            body = json.loads(request.content)
            gid = f"g{next(self._ids)}"
            body |= {"id": gid, "htmlLink": f"https://calendar.google.com/event?eid={gid}"}
            self.events[gid] = body
            return httpx.Response(200, json=body)
        if rest not in self.events:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        if request.method == "PATCH":
            self.events[rest] |= json.loads(request.content)
            return httpx.Response(200, json=self.events[rest])
        if request.method == "DELETE":
            del self.events[rest]
            return httpx.Response(204)
        raise AssertionError(request.method)

    def by_key(self) -> dict[str, dict]:
        return {e["extendedProperties"]["private"]["scoutKey"]: e for e in self.events.values()}


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data: dict = {}


class FakeTokens:
    def __init__(self):
        self.invalidated = 0

    async def token(self) -> str:
        return "fake"

    def invalidate(self) -> None:
        self.invalidated += 1


def make_settings(**kw) -> Settings:
    base = Settings(
        bot_token="x", webhook_url="", port=8000, allowed_chat_id=42, gemini_api_key="x", gemini_model="m",
        ig_provider="apify", apify_token="x", eventbrite_token="", gcal_sa_json="", gcal_calendar_id=CAL,
        tz=TZ, data_dir="/tmp",
    )
    return replace(base, **kw)


@pytest.fixture
def world():
    hooks.clear()
    google = FakeGoogle()
    client = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    db = Database(":memory:")
    app = FakeApp()
    sync = CalendarSync(make_settings(), db, app, client=client, tokens=FakeTokens(),
                        sa_email="scout@proj.iam.gserviceaccount.com", now=lambda: NOW)
    yield sync, db, google, app
    hooks.clear()


def add_event(db: Database, **kw) -> int:
    acc, _ = db.add_account("almamarket_home")
    with db.session() as s:
        ev = Event(account_id=acc.id, title=kw.pop("title", "Alma Market — Chamberí"),
                   source_url="https://www.instagram.com/p/ABC/", summary="Mid-century furniture.",
                   telegram_message_id=777, user_decision=kw.pop("decision", "yes"), **kw)
        s.add(ev)
        s.flush()
        return ev.id


def dates(*days: int) -> str:
    return json.dumps([date(2026, 11, d).isoformat() for d in days])


def stored(db: Database, ev_id: int) -> dict:
    with db.session() as s:
        return load_state(s.get(Event, ev_id).gcal_event_id)


async def test_all_day_multi_day_event(world):
    sync, db, google, app = world
    ev_id = add_event(db, dates=dates(13, 14, 15), address="Calle Almagro 3", area="Chamberí")
    assert await sync.sync(ev_id)
    assert len(google.events) == 1
    e = next(iter(google.events.values()))
    assert e["summary"] == "🪑 Alma Market — Chamberí"
    assert e["start"] == {"date": "2026-11-13"} and e["end"] == {"date": "2026-11-16"}  # end exclusive
    assert e["location"] == "Calle Almagro 3"
    assert "https://www.instagram.com/p/ABC/" in e["description"] and "Mid-century" in e["description"]
    assert e["extendedProperties"]["private"]["scoutId"] == str(ev_id)
    assert stored(db, ev_id) == {"days": {"2026-11-13..2026-11-15": e["id"]}, "booking": None}
    msg = app.bot.sent[-1]
    assert msg["text"] == "📅 Added to your Scout calendar"
    assert msg["reply_parameters"].message_id == 777
    assert msg["reply_markup"].inline_keyboard[0][0].url == e["htmlLink"]


async def test_timed_per_day_events(world):
    sync, db, google, _ = world
    sessions = [
        {"date": "2026-11-13", "start": "16:00", "end": "20:00", "booking_required": False},
        {"date": "2026-11-13", "start": "10:00", "end": "13:00", "booking_required": True},
        {"date": "2026-11-14", "start": "11:00", "end": "19:00", "booking_required": False},
    ]
    ev_id = add_event(db, dates=dates(13, 14), sessions=json.dumps(sessions), area="Chamberí")
    assert await sync.sync(ev_id)
    days = google.by_key()
    assert set(days) == {"2026-11-13", "2026-11-14"}
    fri = days["2026-11-13"]
    assert fri["start"] == {"dateTime": "2026-11-13T10:00:00", "timeZone": "Europe/Madrid"}
    assert fri["end"] == {"dateTime": "2026-11-13T20:00:00", "timeZone": "Europe/Madrid"}
    assert fri["location"] == "Chamberí"
    assert "Fri 13 Nov · 10:00–13:00 🎟, 16:00–20:00" in fri["description"]
    assert set(stored(db, ev_id)["days"]) == {"2026-11-13", "2026-11-14"}


async def test_booking_reminder_event(world):
    sync, db, google, _ = world
    opens = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)  # 10:00 Madrid
    ev_id = add_event(db, dates=dates(13), booking_method="eventbrite", booking_scope="Fri 13 10:00–13:00",
                      booking_url="https://www.eventbrite.es/e/123", booking_opens_at=opens)
    assert await sync.sync(ev_id)
    keys = google.by_key()
    book = keys["booking"]
    assert book["summary"] == "🎟 Book: Alma Market — Chamberí"
    assert book["start"]["dateTime"] == "2026-11-10T10:00:00"
    assert book["end"]["dateTime"] == "2026-11-10T10:15:00"
    assert book["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 5}]}
    assert ("Booking: Eventbrite — Fri 13 10:00–13:00; opens Tue 10 Nov 10:00; "
            "link https://www.eventbrite.es/e/123") in keys["2026-11-13"]["description"]
    assert stored(db, ev_id)["booking"] == book["id"]


async def test_idempotent_create_adopts_existing(world):
    sync, db, google, _ = world
    ev_id = add_event(db, dates=dates(13))
    assert await sync.sync(ev_id)
    with db.session() as s:  # simulate a crash after Google created it but before we saved
        s.get(Event, ev_id).gcal_event_id = None
    assert await sync.sync(ev_id)
    assert len(google.events) == 1
    assert [m for m, _ in google.calls].count("POST") == 1
    assert ("PATCH", f"/calendar/v3/calendars/{CAL}/events/g1") in google.calls
    assert stored(db, ev_id)["days"] == {"2026-11-13": "g1"}


async def test_patch_on_update_and_recreate_on_date_change(world):
    sync, db, google, app = world
    ev_id = add_event(db, dates=dates(13), area="Chamberí")
    await sync.on_decision(ev_id, old="pending", new="yes")
    await sync.drain()
    gid = stored(db, ev_id)["days"]["2026-11-13"]

    with db.session() as s:
        s.get(Event, ev_id).address = "Calle Almagro 3"
    await sync.on_update(ev_id, changes=["address: Calle Almagro 3"])
    await sync.drain()
    assert google.events[gid]["location"] == "Calle Almagro 3"
    assert [m for m, _ in google.calls].count("POST") == 1
    assert app.bot.sent[-1]["text"] == "📅 Calendar updated: address: Calle Almagro 3"

    with db.session() as s:
        s.get(Event, ev_id).dates = dates(20)
    await sync.on_update(ev_id, changes=["date: Fri 20 Nov"])
    await sync.drain()
    assert gid not in google.events
    assert set(google.by_key()) == {"2026-11-20"}
    assert set(stored(db, ev_id)["days"]) == {"2026-11-20"}


async def test_delete_on_decision_change(world):
    sync, db, google, _ = world
    opens = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    ev_id = add_event(db, dates=dates(13), booking_opens_at=opens, booking_method="eventbrite")
    assert await sync.sync(ev_id)
    assert len(google.events) == 2
    with db.session() as s:
        s.get(Event, ev_id).user_decision = "no"
    sync.register_hooks()
    await hooks.emit("decision_changed", ev_id, old="yes", new="no")
    await sync.drain()
    assert google.events == {}
    with db.session() as s:
        assert s.get(Event, ev_id).gcal_event_id is None


async def test_no_dates_waits_then_reconcile_creates(world):
    sync, db, google, app = world
    ev_id = add_event(db)  # teaser, tapped ✅
    assert await sync.sync(ev_id, reason="decision")
    assert google.calls == [] and app.bot.sent == []
    with db.session() as s:
        s.get(Event, ev_id).dates = dates(21)
    assert await sync.retry() == 1
    assert set(google.by_key()) == {"2026-11-21"}


async def test_past_event_left_alone(world):
    sync, db, google, _ = world
    ev_id = add_event(db, dates=dates(1), decision="no", gcal_event_id=json.dumps({"days": {"x": "g9"}}))
    assert await sync.sync(ev_id)
    assert google.calls == []


async def test_403_marks_pending_warns_once_then_retry_clears(world):
    sync, db, google, app = world
    google.fail_status = 403
    a = add_event(db, dates=dates(13), title="A")
    b = add_event(db, dates=dates(14), title="B")
    assert not await sync.sync(a)
    assert not await sync.sync(b)
    assert sync.pending() == [a, b]
    warnings = [m for m in app.bot.sent if m["text"].startswith("⚠️")]
    assert len(warnings) == 1
    assert "scout@proj.iam.gserviceaccount.com" in warnings[0]["text"]
    assert "Make changes to events" in warnings[0]["text"]
    assert (await sync.health())[0] is False

    google.fail_status = None  # user fixed the sharing
    assert await sync.retry() == 2
    assert sync.pending() == []
    assert len(google.events) == 2
    assert (await sync.health()) == (True, "Calendar OK")


async def test_network_error_never_raises(world):
    sync, db, _, _ = world

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    ev_id = add_event(db, dates=dates(13))
    assert not await sync.sync(ev_id)
    assert sync.pending() == [ev_id]


async def test_401_refreshes_token_once(world):
    sync, db, google, _ = world
    real = google.handler
    seen = []

    def flaky(request):
        if not seen:
            seen.append(1)
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return real(request)

    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    ev_id = add_event(db, dates=dates(13))
    assert await sync.sync(ev_id)
    assert sync.tokens.invalidated == 1


async def test_service_account_tokens_via_token_endpoint():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    info = {"type": "service_account", "client_email": "scout@proj.iam.gserviceaccount.com",
            "private_key": pem, "private_key_id": "k1", "token_uri": "https://oauth2.googleapis.com/token"}
    google = FakeGoogle()
    tokens = ServiceAccountTokens(info, httpx.AsyncClient(transport=httpx.MockTransport(google.handler)))
    assert await tokens.token() == "tok1"
    assert await tokens.token() == "tok1"  # cached
    tokens.invalidate()
    assert await tokens.token() == "tok2"
    assert tokens.email == "scout@proj.iam.gserviceaccount.com"


def test_setup_disabled_without_config():
    assert setup_calendar(FakeApp(), make_settings(gcal_calendar_id=""), Database(":memory:")) is None

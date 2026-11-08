"""Booker reports a booked job -> Scout downloads the ticket and records the slot exactly once."""

import json
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from scout import hooks
from scout.autobook import AutoBook, BookerClient
from scout.config import Settings
from scout.db import Account, BookingWatch, Database, Event, utcnow

PNG = b"\x89PNG\r\n\x1a\nfake-qr"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(bot_token="1:x", webhook_url="", port=8000, allowed_chat_id=42, gemini_api_key="g",
                    gemini_model="m", ig_provider="apify", apify_token="a", eventbrite_token="", gcal_sa_json="",
                    gcal_calendar_id="", tz=ZoneInfo("Europe/Madrid"), data_dir=tmp_path)


class FakeBooker:
    def __init__(self):
        self.jobs: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.ticket: bytes | None = PNG

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/jobs":
            return httpx.Response(200, json={"jobs": list(self.jobs.values())})
        if path.endswith("/ticket"):
            if self.ticket is None:
                return httpx.Response(404, json={"error": "no ticket"})
            return httpx.Response(200, content=self.ticket, headers={"content-type": "image/png"})
        if path == "/book":
            body = json.loads(request.content)
            self.jobs[body["event_id"]] = {**body, "state": "scheduled"}
            return httpx.Response(200, json={"job": self.jobs[body["event_id"]], "changed": True})
        return httpx.Response(404, json={})


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = FakeBooker()
    ab = AutoBook(make_settings(tmp_path), db, BookerClient("http://b", "", httpx.MockTransport(fake.handler)))
    confirmed: list[tuple[int, dict]] = []

    async def spy(event_id, **kw):  # noqa: ANN001, ANN003
        confirmed.append((event_id, kw))

    hooks.on("booking_confirmed", spy)
    yield db, fake, ab, confirmed, tmp_path
    hooks.clear()


def add_armed(db, opens_in=timedelta(minutes=-2)) -> int:  # noqa: ANN001
    with db.session() as s:
        acc = Account(handle="m")
        s.add(acc)
        s.flush()
        ev = Event(account_id=acc.id, title="Alma Market", user_decision="yes", booking_method="eventbrite")
        s.add(ev)
        s.flush()
        s.add(BookingWatch(event_id=ev.id, eventbrite_id="123456789", state="live", opens_at=utcnow() + opens_in,
                           autobook=True, autobook_state="running"))
        return ev.id


def booked_job(eid: int, **kw) -> dict:  # noqa: ANN003
    return {"event_id": eid, "state": "booked", "opens_at": utcnow().isoformat(),
            "slot_start": "2026-11-13T10:30:00+01:00", "slot_end": "2026-11-13T11:00:00+01:00",
            "ticket": f"{eid}.png", "constraints": {"date": "2026-11-13", "earliest": "10:00", "latest": "13:00"},
            **kw}


async def test_booked_job_records_once_with_ticket(env):
    db, fake, ab, confirmed, tmp = env
    eid = add_armed(db)
    fake.jobs[eid] = booked_job(eid)
    await ab.sync()
    await ab.sync()
    assert confirmed == [(eid, {"via": "auto"})]
    assert fake.calls.count(("GET", f"/jobs/{eid}/ticket")) == 1
    with db.session() as s:
        ev = s.get(Event, eid)
        assert ev.booked_start.isoformat() == "2026-11-13T09:30:00+00:00"
        assert ev.booked_end.isoformat() == "2026-11-13T10:00:00+00:00"
        assert ev.booked_via == "auto"
        assert ev.ticket_path == str(tmp / "tickets" / f"{eid}.png")
        assert s.query(BookingWatch).filter_by(event_id=eid).one().autobook_state == "booked"
    assert (tmp / "tickets" / f"{eid}.png").read_bytes() == PNG


async def test_ticket_arriving_later_is_attached_not_rerecorded(env):
    db, fake, ab, confirmed, tmp = env
    added = []

    async def on_ticket(event_id, **kw):  # noqa: ANN001, ANN003
        added.append(kw["path"])

    hooks.on("ticket_added", on_ticket)
    eid = add_armed(db)
    fake.jobs[eid] = booked_job(eid, ticket="")
    await ab.sync()
    assert len(confirmed) == 1 and not any(p.endswith("/ticket") for _, p in fake.calls)
    fake.jobs[eid]["ticket"] = f"{eid}.png"
    await ab.sync()
    assert len(confirmed) == 1 and added == [str(tmp / "tickets" / f"{eid}.png")]


async def test_untimed_booking_falls_back_to_session_window(env):
    db, fake, ab, confirmed, _ = env
    eid = add_armed(db)
    fake.jobs[eid] = booked_job(eid, slot_start="", slot_end="")
    fake.ticket = b"%PDF-1.4 ticket"
    await ab.sync()
    with db.session() as s:
        ev = s.get(Event, eid)
        assert ev.booked_start.isoformat() == "2026-11-13T09:00:00+00:00"
        assert ev.ticket_path.endswith(f"{eid}.pdf")


async def test_sync_is_fast_near_opening(env):
    db, fake, ab, _, _ = env
    add_armed(db, opens_in=timedelta(hours=5))
    await ab.tick()
    n = len(fake.calls)
    ab._last_sync = time.monotonic() - 61
    await ab.tick()
    assert len(fake.calls) == n  # nothing opens soon: 10-minute cadence
    with db.session() as s:
        s.query(BookingWatch).one().opens_at = utcnow() + timedelta(minutes=20)
    await ab.tick()
    assert len(fake.calls) > n  # opens within 30 min: every minute

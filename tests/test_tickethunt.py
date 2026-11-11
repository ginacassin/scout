"""Ticket hunt: per-slot availability parsing and the /hunt watch (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scout import hooks
from scout.bot import templates as T
from scout.bookings import record_booking
from scout.config import Settings
from scout.db import BookingWatch, Database
from scout.eventbrite.client import ld_events
from scout.eventbrite.slots import SlotInfo, slots_from_api, slots_from_ld
from scout.tickethunt import TicketHunt
from tests.test_eventbrite import TZ, Clock, FakeApp, add_event, mad


# ------------------------------------------------------------------ parsing
def test_api_ticket_class_per_slot():
    ev = {"start": {"utc": "2026-11-14T09:00:00Z"}, "end": {"utc": "2026-11-14T13:00:00Z"}, "is_free": True}
    classes = [{"name": "Sábado 14 · 10:00–10:30", "on_sale_status": "SOLD_OUT", "free": True},
               {"name": "Sábado 14 · 10:30–11:00", "on_sale_status": "AVAILABLE", "free": True},
               {"name": "11:00 - 11:30", "on_sale_status": "SOLD_OUT", "free": True}]
    slots = slots_from_api(ev, classes)
    assert [s.start.astimezone(TZ).strftime("%d %H:%M") for s in slots] == ["14 10:00", "14 10:30", "14 11:00"]
    assert [s.available for s in slots] == [False, True, False]
    assert all(s.exact for s in slots)


def test_api_series_children():
    children = [
        {"name": {"text": "Alma"}, "start": {"utc": "2026-11-14T09:00:00Z"}, "end": {"utc": "2026-11-14T09:30:00Z"},
         "is_free": True, "ticket_availability": {"has_available_tickets": False, "is_sold_out": True}},
        {"name": {"text": "Alma"}, "start": {"utc": "2026-11-14T09:30:00Z"}, "end": {"utc": "2026-11-14T10:00:00Z"},
         "is_free": True, "ticket_availability": {"has_available_tickets": True, "is_sold_out": False}},
    ]
    slots = slots_from_api({}, None, children)
    assert [s.available for s in slots] == [False, True]


def test_api_single_ticket_is_event_level():
    ev = {"start": {"utc": "2026-11-14T09:00:00Z"}, "is_free": True,
          "ticket_availability": {"has_available_tickets": True}}
    (s,) = slots_from_api(ev, [{"name": "Entrada general", "on_sale_status": "AVAILABLE", "free": True}])
    assert s.available and not s.exact


def _ld(*events):
    return "".join(f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in events)


def test_ld_series_keeps_children_not_parent():
    def ev(start, end, avail):
        return {"@type": "Event", "name": "Alma", "startDate": start, "endDate": end,
                "offers": [{"@type": "Offer", "price": "0", "availability": f"https://schema.org/{avail}"}]}
    html = _ld(ev("2026-11-14T10:00:00+01:00", "2026-11-15T14:00:00+01:00", "InStock"),
               ev("2026-11-14T10:00:00+01:00", "2026-11-14T10:30:00+01:00", "SoldOut"),
               ev("2026-11-14T10:30:00+01:00", "2026-11-14T11:00:00+01:00", "InStock"))
    slots = slots_from_ld(ld_events(html))
    assert len(slots) == 2 and [s.available for s in slots] == [False, True] and slots[0].free is True


def test_ld_offers_named_by_time():
    html = _ld({"@type": "Event", "name": "Alma", "startDate": "2026-11-14T10:00:00+01:00",
                "endDate": "2026-11-14T14:00:00+01:00",
                "offers": [{"@type": "Offer", "name": "10:00 - 10:30", "price": "0",
                            "availability": "https://schema.org/SoldOut"},
                           {"@type": "Offer", "name": "12:00 - 12:30", "price": "0",
                            "availability": "https://schema.org/InStock"}]})
    slots = slots_from_ld(ld_events(html))
    assert [(s.start.astimezone(TZ).hour, s.available) for s in slots] == [(10, False), (12, True)]


# -------------------------------------------------------------------- hunt
def slot(day, h, m, available, exact=True):
    start = mad(11, day, h, m)
    return SlotInfo(start, None, f"{h}:{m:02d}", available, True, exact=exact)


class FakeSlots:
    token = "tok"

    def __init__(self):
        self.slots: list[SlotInfo] | None = []
        self.calls = 0

    async def slot_states(self, eid, url=None):
        self.calls += 1
        return self.slots


@pytest.fixture
def henv(tmp_path):
    hooks.clear()
    before = list(T.EXTRA_ANSWERED_BUTTONS)
    settings = Settings(
        bot_token="x", webhook_url="", port=8000, allowed_chat_id=1, gemini_api_key="x", gemini_model="m",
        ig_provider="apify", apify_token="x", eventbrite_token="tok", gcal_sa_json="", gcal_calendar_id="",
        tz=TZ, data_dir=tmp_path,
    )
    db = Database(":memory:")
    app = FakeApp()
    clock = Clock(mad(11, 10, 21))  # Tue 10 Nov 21:00
    client = FakeSlots()
    hunt = TicketHunt(app, settings, db, client=client, now=clock)
    hunt.register()
    eid = add_event(db, dates=("2026-11-14", "2026-11-15"))
    with db.session() as s:
        s.add(BookingWatch(event_id=eid, eventbrite_id="2222", state="sold_out"))
    yield hunt, db, app, clock, client, eid
    hooks.clear()
    T.EXTRA_ANSWERED_BUTTONS[:] = before


def alerts(app):
    return [m for m in app.bot.sent if m["text"].startswith("🎟")]


async def test_not_booked_alerts_once_when_any_slot_frees(henv):
    hunt, db, app, clock, client, eid = henv
    client.slots = [slot(14, 10, 0, False), slot(14, 10, 30, False)]
    text = await hunt.start(eid)
    assert "any slot" in text and "all 2 slots taken" in text and not alerts(app)
    client.slots = [slot(14, 10, 0, False), slot(14, 10, 30, True)]
    await hunt.check(eid)
    await hunt.check(eid)  # still free: no repeat
    assert len(alerts(app)) == 1 and "10:30" in alerts(app)[0]["text"]
    client.slots = [slot(14, 10, 0, False), slot(14, 10, 30, False)]
    await hunt.check(eid)
    client.slots = [slot(14, 10, 0, False), slot(14, 10, 30, True)]
    await hunt.check(eid)  # gone and back: alert again
    assert len(alerts(app)) == 2


async def test_booked_only_earlier_slots_count(henv):
    hunt, db, app, clock, client, eid = henv
    await record_booking(db, eid, mad(11, 14, 12, 0))
    client.slots = [slot(14, 10, 0, False), slot(14, 12, 0, True), slot(14, 12, 30, True)]
    text = await hunt.start(eid)
    assert "earlier than your 12:00" in text and not alerts(app)
    client.slots[0] = slot(14, 10, 0, True)
    await hunt.check(eid)
    (a,) = alerts(app)
    assert "Earlier than your 12:00" in a["text"] and "10:00" in a["text"] and "12:30" not in a["text"]
    assert "Cancel your old ticket" in a["text"]


async def test_before_cutoff_and_vague_event_level(henv):
    hunt, db, app, clock, client, eid = henv
    client.slots = [slot(14, 11, 30, True)]
    await hunt.start(eid, before="11:00")
    assert not alerts(app)
    client.slots = [SlotInfo(mad(11, 14, 10), None, "General", True, True, exact=False)]
    await hunt.check(eid)
    assert "doesn't say which times" not in alerts(app)[0]["text"]  # not booked: any ticket is good news


async def test_stops_at_9_on_first_day(henv):
    hunt, db, app, clock, client, eid = henv
    client.slots = []
    await hunt.start(eid)
    clock.t = mad(11, 14, 8, 59)
    await hunt.tick()
    with db.session() as s:
        assert s.query(BookingWatch).one().hunt_on
    clock.t = mad(11, 14, 9, 0)
    await hunt.tick()
    with db.session() as s:
        assert not s.query(BookingWatch).one().hunt_on
    assert any("Stopped watching tickets" in m["text"] for m in app.bot.sent)
    assert "Too late" in await hunt.start(eid)


async def test_quiet_hours_and_interval(henv):
    hunt, db, app, clock, client, eid = henv
    client.slots = [slot(14, 10, 0, False)]
    await hunt.start(eid)
    calls = client.calls
    await hunt.tick()  # just checked: skipped
    assert client.calls == calls
    clock.t = mad(11, 11, 2, 0)  # 02:00, long after the interval
    client.slots = [slot(14, 10, 0, True)]
    await hunt.tick()
    assert alerts(app)[-1]["disable_notification"] is True


async def test_card_button_and_no_decision_stops(henv):
    hunt, db, app, clock, client, eid = henv
    from scout.db import Event
    with db.session() as s:
        ev = s.get(Event, eid)
    assert hunt.card_buttons(ev)[0].callback_data == f"th:on:{eid}"
    await hunt.start(eid)
    assert hunt.card_buttons(ev)[0].callback_data == f"th:off:{eid}"
    await hooks.emit("decision_changed", eid, old="yes", new="no")
    with db.session() as s:
        assert not s.query(BookingWatch).one().hunt_on


async def test_no_eventbrite_link(henv):
    hunt, db, app, clock, client, eid = henv
    with db.session() as s:
        s.query(BookingWatch).one().eventbrite_id = None
    assert "nothing to watch" in await hunt.start(eid)


async def test_checks_every_15_min_by_day_and_45_at_night(henv):
    hunt, db, app, clock, client, eid = henv
    client.slots = [slot(14, 10, 0, False)]
    clock.t = mad(11, 11, 12, 0)
    text = await hunt.start(eid)
    assert "every 15 min (45 min at night, 00:00–05:30)" in text
    n = client.calls
    clock.t = mad(11, 11, 12, 14)
    await hunt.tick()
    assert client.calls == n  # 14 min: too soon
    clock.t = mad(11, 11, 12, 15)
    await hunt.tick()
    assert client.calls == n + 1
    clock.t = mad(11, 11, 23, 45)  # still daytime cadence
    await hunt.tick()
    n = client.calls
    clock.t = mad(11, 12, 0, 15)  # 30 min later, now night: wait
    await hunt.tick()
    assert client.calls == n
    clock.t = mad(11, 12, 0, 30)  # 45 min after the last check
    await hunt.tick()
    assert client.calls == n + 1
    clock.t = mad(11, 12, 5, 30)  # night over: 15 min again
    await hunt.tick()
    n = client.calls
    clock.t = mad(11, 12, 5, 45)
    await hunt.tick()
    assert client.calls == n + 1

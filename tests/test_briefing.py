from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from scout.briefing import Briefing, maps_route_url, stop_time
from scout.db import Database, Event

TZ = ZoneInfo("Europe/Madrid")
FRI = date(2026, 11, 13)


def setup(tmp_path):
    db = Database(":memory:")
    a, _ = db.add_account("thecircularmarket.es")
    b, _ = db.add_account("almamarket_home")
    with db.session() as s:
        clara = Event(account_id=a.id, title="Mercadillo Clara del Rey", address="C/ Clara del Rey 14, 28002 Madrid",
                      summary="Dishes, furniture and lamps.", user_decision="yes",
                      source_url="https://www.instagram.com/p/DfZkLmIDRJk/",
                      booked_start=datetime(2026, 11, 13, 10, 30, tzinfo=TZ), booked_end=datetime(2026, 11, 13, 11, 0, tzinfo=TZ),
                      booked_via="auto", ticket_path="/data/tickets/1.png")
        clara.date_list = [FRI, date(2026, 11, 14)]
        alma = Event(account_id=b.id, title="Alma Market — flat in Chamberí", area="Chamberí (Almagro)", user_decision="yes",
                     summary="Tableware, books, clothes.")
        alma.date_list = [FRI, date(2026, 11, 14)]
        alma.session_list = [{"date": "2026-11-13", "start": "11:30", "end": "14:30", "booking_required": True},
                             {"date": "2026-11-13", "start": "16:00", "end": "20:00", "booking_required": False}]
        alma.add_note("Green lamp in the living room")
        no = Event(account_id=b.id, title="Skipped market", user_decision="no")
        no.date_list = [FRI]
        s.add_all([clara, alma, no])
    app = MagicMock()
    app.bot_data = {}
    app.bot.send_message = AsyncMock()
    settings = SimpleNamespace(tz=TZ, allowed_chat_id=1)
    return Briefing(app, settings, db), app, db


@pytest.mark.asyncio
async def test_briefing_lists_markets_in_time_order_with_details(tmp_path):
    b, app, db = setup(tmp_path)
    text, kb = await b.text(FRI)
    assert "2 markets" in text
    assert text.index("10:30 · Mercadillo Clara del Rey") < text.index("16:00 · Alma Market")  # free session after booked slot
    assert 'href="https://www.instagram.com/thecircularmarket.es/"' in text
    assert "Your slot 10:30–11:00 · ticket pinned" in text
    assert "Green lamp" in text and "Dishes, furniture" in text
    assert "Skipped market" not in text
    assert kb.inline_keyboard[0][0].url.startswith("https://www.google.com/maps/dir/?api=1")


@pytest.mark.asyncio
async def test_no_markets_no_message_and_sent_once(tmp_path):
    b, app, db = setup(tmp_path)
    assert await b.send(date(2026, 11, 20)) is False
    app.bot.send_message.assert_not_called()
    assert await b.send(FRI) is True
    assert await b.send(FRI) is False
    assert app.bot.send_message.call_count == 1


@pytest.mark.asyncio
async def test_drive_times_between_stops_when_planner_available(tmp_path):
    b, app, db = setup(tmp_path)
    planner = MagicMock()
    planner._point = AsyncMock(side_effect=lambda ev: (40.44, -3.68))
    planner.geo.leg = AsyncMock(return_value=SimpleNamespace(minutes=12, km=3.4, guessed=False))
    app.bot_data["planner"] = planner
    text, _ = await b.text(FRI)
    assert "about 12′ drive, 3.4 km" in text


def test_stop_time_and_route_url():
    ev = Event(title="x")
    ev.session_list = [{"date": "2026-11-13", "start": "10:00", "end": "13:00", "booking_required": True}]
    assert stop_time(ev, FRI, TZ).strftime("%H:%M") == "10:00"
    url = maps_route_url(["C/ Clara del Rey 14", "Chamberí"])
    assert "waypoints=C%2F%20Clara%20del%20Rey%2014" in url and "destination=Chamber%C3%AD" in url


@pytest.mark.asyncio
async def test_login_check_warns_once_until_fixed(tmp_path):
    b, app, db = setup(tmp_path)
    ab = SimpleNamespace(enabled=True, client=SimpleNamespace(check_login=AsyncMock(return_value=(200, {"logged_in": False}))))
    app.bot_data["autobook"] = ab
    await b.login_check()
    await b.login_check()
    assert app.bot.send_message.call_count == 1
    ab.client.check_login = AsyncMock(return_value=(200, {"logged_in": True}))
    await b.login_check()
    ab.client.check_login = AsyncMock(return_value=(200, {"logged_in": False}))
    await b.login_check()
    assert app.bot.send_message.call_count == 2

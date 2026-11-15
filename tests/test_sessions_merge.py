"""Opening hours: a later vague mention never wipes hours out; repair-sessions rebuilds them."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from scout.db import Account, Database, Event, SourceItem
from scout.pipeline import merge_sessions
from scout.repair import repair_sessions

SAT, SUN = "2026-11-21", "2026-11-22"


def sess(day, start=None, end=None):
    return {"date": day, "start": start, "end": end, "booking_required": False}


def test_vague_later_mention_keeps_hours():
    old = [sess(SAT, "09:00", "15:00"), sess(SUN, "09:00", "15:00")]
    assert merge_sessions(old, [sess(SAT), sess(SUN)]) == old


def test_newer_hours_win_and_days_are_kept():
    old = [sess(SAT, "10:00", "14:00"), sess(SUN, "10:00", "14:00")]
    merged = merge_sessions(old, [sess(SAT, "10:30", "19:00")])
    assert merged == [sess(SAT, "10:30", "19:00"), sess(SUN, "10:00", "14:00")]


def test_first_hours_fill_in():
    assert merge_sessions([sess(SAT)], [sess(SAT, "11:00", "20:00")]) == [sess(SAT, "11:00", "20:00")]


def parsed(sessions, address=None, url=None):
    ev = {"title": "Vintage Market", "dates": [SAT], "sessions": sessions, "address": address,
          "region": "madrid", "booking_method": "eventbrite", "booking_url": url,
          "booking_status": "unknown", "link_in_bio": False, "summary_en": "x", "confidence": 0.9}
    return json.dumps({"kind": "new_event", "events": [ev]})


async def test_repair_rebuilds_hours_wiped_by_a_story():
    db = Database(":memory:")
    with db.session() as s:
        acc = Account(handle="market")
        s.add(acc)
        s.flush()
        ev = Event(account_id=acc.id, title="Vintage Market", dates="[]", status="announced", user_decision="pending")
        ev.date_list = [date(2026, 11, 21)]
        ev.session_list = [sess(SAT)]  # what the old merge left
        s.add(ev)
        s.flush()
        s.add(SourceItem(account_id=acc.id, ig_id="post:1", kind="post", caption="",
                         parsed_json=parsed([sess(SAT, "09:00", "15:00")], "C/ Gacela 1, Batres",
                                            "https://www.eventbrite.es/e/1"), event_ids=json.dumps([ev.id])))
        s.add(SourceItem(account_id=acc.id, ig_id="story:2", kind="story", caption="",
                         parsed_json=parsed([sess(SAT)]), event_ids=json.dumps([ev.id])))
        eid = ev.id
    out = await repair_sessions(SimpleNamespace(bot_data={}), db, [])
    assert out.startswith("1 of 1 markets repaired")
    with db.session() as s:
        ev = s.get(Event, eid)
        assert ev.session_list == [sess(SAT, "09:00", "15:00")]
        assert ev.booking_url == "https://www.eventbrite.es/e/1"
        assert "Gacela" in ev.address
    assert (await repair_sessions(SimpleNamespace(bot_data={}), db, [])).startswith("0 of 1")

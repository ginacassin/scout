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


def roundup(*markets):
    evs = []
    for title, address, start in markets:
        evs.append({"title": title, "dates": [SAT], "sessions": [sess(SAT, start, "14:00")], "address": address,
                    "region": "madrid", "booking_method": "free_entry", "booking_status": "unknown",
                    "link_in_bio": False, "summary_en": "x", "confidence": 0.9})
    return json.dumps({"kind": "new_event", "events": evs})


async def test_roundup_gives_each_market_only_its_own_details(tmp_path):
    db = Database(":memory:")
    with db.session() as s:
        acc = Account(handle="mercadillosmad")
        s.add(acc)
        s.flush()
        ids = []
        for title in ("Mercadillo en casa Arturo Soria", "El Déballage de La Moraleja"):
            ev = Event(account_id=acc.id, title=title, dates="[]", status="announced", user_decision="pending")
            ev.date_list = [date(2026, 11, 21)]
            ev.session_list = [sess(SAT)]
            s.add(ev)
            s.flush()
            ids.append(ev.id)
        s.add(SourceItem(account_id=acc.id, ig_id="post:r", kind="post", caption="",
                         parsed_json=roundup(("Mercadillo en casa Arturo Soria", "Caribe 5 y 7", "10:00"),
                                             ("El Déballage de La Moraleja", "C.C. El Bulevar", "11:00")),
                         event_ids=json.dumps(sorted(ids))))
    await repair_sessions(SimpleNamespace(bot_data={}), db, [])
    with db.session() as s:
        a, b = (s.get(Event, i) for i in ids)
        assert "Caribe" in a.address and a.session_list[0]["start"] == "10:00"
        assert "Bulevar" in b.address and b.session_list[0]["start"] == "11:00"


async def test_repair_from_an_older_copy(tmp_path):
    import sqlite3
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("create table events (id integer, sessions text, address text, booking_url text)")
    con.execute("insert into events values (1, ?, 'Serrano 1', null)", (json.dumps([sess(SAT, "09:00", "13:00")]),))
    con.commit()
    con.close()
    db = Database(":memory:")
    with db.session() as s:
        acc = Account(handle="m")
        s.add(acc)
        s.flush()
        ev = Event(id=1, account_id=acc.id, title="Market", dates="[]", status="announced", address="Wrong 5")
        ev.date_list = [date(2026, 11, 21)]
        ev.session_list = [sess(SAT, "18:00", "20:00")]
        s.add(ev)
        s.add(SourceItem(account_id=acc.id, ig_id="story:x", kind="story", caption="",
                         parsed_json=parsed([sess(SAT)]), event_ids="[1]"))
    dry = await repair_sessions(SimpleNamespace(bot_data={}), db, [f"--from={old}", "--dry-run"])
    assert dry.startswith("1 of 1 markets would change") and "Serrano 1" in dry
    with db.session() as s:
        assert s.get(Event, 1).address == "Wrong 5"
    out = await repair_sessions(SimpleNamespace(bot_data={}), db, [f"--from={old}"])
    assert out.startswith("1 of 1")
    with db.session() as s:
        ev = s.get(Event, 1)
        assert ev.address == "Serrano 1" and ev.session_list == [sess(SAT, "09:00", "13:00")]

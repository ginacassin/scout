"""Slot planner: forbidden windows from travel + visit time, session limits, re-plan, /plan (no network)."""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from scout import bookings, hooks
from scout.autobook import AutoBook, BookerClient
from scout.config import Settings
from scout.db import Account, BookingWatch, Database, Event, utcnow
from scout.geo import Leg
from scout.planner import Planner, forbidden_window, merge, parse_day, plan_day, slot_allowed

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "booker"))
from scoutbooker import slots as booker_slots  # noqa: E402  (the booker applies the same rule)

TZ = ZoneInfo("Europe/Madrid")
DAY = date(2026, 11, 13)
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)
CHAT = 42


def at(hm: str, day: date = DAY) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def settings(tmp_path: Path) -> Settings:
    return Settings(bot_token="1:x", webhook_url="", port=8000, allowed_chat_id=CHAT, gemini_api_key="g",
                    gemini_model="m", ig_provider="apify", apify_token="a", eventbrite_token="", gcal_sa_json="",
                    gcal_calendar_id="", tz=TZ, data_dir=tmp_path)


class FakeGeo:
    """Venues are their address strings; drive minutes from a symmetric table."""

    def __init__(self, minutes: dict[frozenset, int], default: int = 30):
        self.minutes = minutes
        self.default = default

    async def locate(self, address, area):  # noqa: ANN001
        return address or area

    async def leg(self, a, b):  # noqa: ANN001
        if a is None or b is None:
            return Leg(self.default, None, "unknown")
        if a == b:
            return Leg(5, 0.0, "same")
        return Leg(self.minutes.get(frozenset((a, b)), self.default), 8.4, "osrm")


GEO = FakeGeo({frozenset(("Alma", "Clara")): 25, frozenset(("Alma", "Cobo")): 40,
               frozenset(("Clara", "Cobo")): 35, frozenset(("Alma", "MyMarket")): 15,
               frozenset(("Clara", "MyMarket")): 20, frozenset(("Cobo", "MyMarket")): 45})


def add_event(db, title, address, sessions=None, booked=None, armed=False, opens_in=timedelta(days=2)) -> int:  # noqa: ANN001
    with db.session() as s:
        acc = s.query(Account).first() or Account(handle="m")
        s.add(acc)
        s.flush()
        ev = Event(account_id=acc.id, title=title, address=address, user_decision="yes",
                   booking_method="eventbrite")
        ev.date_list = [DAY]
        ev.session_list = sessions if sessions is not None else [
            {"date": DAY.isoformat(), "start": "10:00", "end": "14:00", "booking_required": True}]
        if booked:
            ev.booked_start, ev.booked_end = at(booked[0]), at(booked[1])
        s.add(ev)
        s.flush()
        if armed:
            s.add(BookingWatch(event_id=ev.id, eventbrite_id=str(100000000 + ev.id), state="waiting",
                               event_url=f"https://www.eventbrite.es/e/x-{100000000 + ev.id}",
                               opens_at=utcnow() + opens_in, autobook=True, autobook_state="scheduled"))
        return ev.id


@pytest.fixture
def db(tmp_path):
    yield Database(tmp_path / "p.db")
    hooks.clear()


def planner(db, tmp_path, visit=30):  # noqa: ANN001
    return Planner(settings(tmp_path), db, geo=GEO, visit_minutes=visit, now=lambda: NOW)


# ------------------------------------------------------------------- pure rule
def test_window_and_rule_with_30_min_visit():
    v = timedelta(minutes=30)
    w = forbidden_window(at("11:00"), at("11:30"), "Clara", Leg(25, 8.0, "osrm"), Leg(25, 8.0, "osrm"), v)
    assert (w.lo, w.hi) == (at("10:05"), at("11:55"))
    win = [(w.lo, w.hi)]
    assert slot_allowed(at("10:00"), at("10:30"), win, v)       # leave 10:30, +25 -> 10:55 ≤ 11:00
    assert slot_allowed(at("10:05"), at("10:35"), win, v)       # exactly on time
    assert not slot_allowed(at("10:30"), at("11:00"), win, v)
    assert not slot_allowed(at("11:30"), at("12:00"), win, v)   # can't get here from Clara by 11:30
    assert slot_allowed(at("11:55"), at("12:25"), win, v)
    assert not slot_allowed(at("10:00"), at("11:00"), win, v)   # 1-h slot: you'd leave at 11:00


def test_long_booked_slot_extends_the_stay():
    w = forbidden_window(at("11:00"), at("12:00"), "Clara", Leg(25, None, "osrm"), Leg(20, None, "osrm"),
                         timedelta(minutes=30))
    assert (w.lo, w.hi, w.stay) == (at("10:10"), at("12:25"), 60)


def test_merge_windows():
    assert merge([(at("12:00"), at("13:00")), (at("10:00"), at("11:00")), (at("10:30"), at("12:00"))]) == [
        (at("10:00"), at("13:00"))]


def test_parse_day():
    today = date(2026, 11, 8)  # Sunday
    assert parse_day("2026-11-13", today) == DAY
    assert parse_day("13/11", today) == DAY
    assert parse_day("13", today) == DAY
    assert parse_day("viernes", today) == DAY
    assert parse_day("fri", today) == DAY
    assert parse_day("tomorrow", today) == date(2026, 11, 9)
    assert parse_day("5", today) == date(2026, 12, 5)
    assert parse_day("blah", today) is None


# ------------------------------------------------------------- constraints
async def test_constraints_with_travel_and_sessions(db, tmp_path):
    add_event(db, "Circular Market Clara del Rey", "Clara", booked=("11:00", "11:30"))
    alma = add_event(db, "Alma Market Chamberí", "Alma",
                     sessions=[{"date": "2026-11-13", "start": "10:00", "end": "13:00", "booking_required": True},
                               {"date": "2026-11-13", "start": "17:00", "end": "20:00", "booking_required": False}])
    plan = await planner(db, tmp_path).constraints_for(alma)
    assert plan.constraints == {"date": "2026-11-13", "earliest": "10:00", "latest": "13:00",
                                "avoid": [["10:05", "11:55"]], "prefer": "earliest", "visit_minutes": 30,
                                "tz": "Europe/Madrid"}
    text = plan.text()
    assert "Opening hours that need a booking: 10:00–13:00" in text
    assert "Avoiding 10:05–11:55: you're at Circular Market Clara del Rey 11:00 + 30′ + 25′ drive" in text


async def test_multiple_bookings_and_visit_env(db, tmp_path, monkeypatch):
    add_event(db, "Clara", "Clara", booked=("10:00", "10:30"))
    add_event(db, "My Market", "MyMarket", booked=("12:00", "12:15"))
    cobo = add_event(db, "Home Market Cobo Calleja", "Cobo")
    monkeypatch.setenv("VISIT_MINUTES", "45")
    p = Planner(settings(tmp_path), db, geo=GEO, now=lambda: NOW)
    c = (await p.constraints_for(cobo)).constraints
    # Clara: 10:00-45-35 = 08:40 .. 10:45+35 = 11:20 ; My Market: 12:00-45-45 = 10:30 .. 12:45+45 = 13:30
    assert c["avoid"] == [["08:40", "13:30"]] and c["visit_minutes"] == 45
    assert c["earliest"] == "10:00" and c["latest"] == "14:00"
    # the booker agrees: only slots from 13:30 fit
    slots = [booker_slots.parse_slot(f"Viernes 13 · {h}", DAY, TZ) for h in
             ("10:00–10:30", "13:00–13:30", "13:30–14:00")]
    chosen, rejected = booker_slots.choose(slots, c, TZ)
    assert chosen.start == at("13:30") and len(rejected) == 2


async def test_no_bookings_no_sessions_and_other_days(db, tmp_path):
    other_day = add_event(db, "Saturday thing", "Clara")
    with db.session() as s:
        ev = s.get(Event, other_day)
        ev.booked_start, ev.booked_end = at("11:00", date(2026, 11, 14)), at("11:30", date(2026, 11, 14))
    alma = add_event(db, "Alma", "Alma", sessions=[])
    plan = await planner(db, tmp_path).constraints_for(alma)
    assert plan.constraints["avoid"] == [] and plan.constraints["earliest"] is None
    assert "No other bookings" in plan.text()


def test_plan_day_prefers_booking_sessions():
    ev = Event(title="x")
    ev.date_list = [date(2026, 11, 12), DAY]
    ev.session_list = [{"date": "2026-11-12", "start": "10:00", "booking_required": False},
                       {"date": "2026-11-13", "start": "10:00", "booking_required": True}]
    assert plan_day(ev, date(2026, 11, 8)) == DAY
    assert plan_day(ev, date(2026, 11, 14)) is None


def test_planner_matches_booker_rule_exhaustively():
    """Every 5-minute slot start, 15/30/60-min slots: planner rule == booker check."""
    v = timedelta(minutes=30)
    w = forbidden_window(at("11:00"), at("11:30"), "Clara", Leg(25, None, "osrm"), Leg(20, None, "osrm"), v)
    cons = {"date": DAY.isoformat(), "avoid": [[w.lo.strftime("%H:%M"), w.hi.strftime("%H:%M")]],
            "visit_minutes": 30}
    for minutes in range(9 * 60, 14 * 60, 5):
        s = at("00:00") + timedelta(minutes=minutes)
        for length in (15, 30, 60):
            e = s + timedelta(minutes=length)
            slot = booker_slots.Slot("x", s, e)
            assert slot_allowed(s, e, [(w.lo, w.hi)], v) == booker_slots.check(slot, cons, TZ)[0], (s, length)


# -------------------------------------------------------------------- re-plan
class FakeBooker:
    def __init__(self):
        self.posts: list[dict] = []
        self.jobs: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/book":
            body = json.loads(request.content)
            self.posts.append(body)
            self.jobs[body["event_id"]] = {**body, "state": "scheduled"}
            return httpx.Response(200, json={"job": self.jobs[body["event_id"]], "changed": True})
        if request.url.path == "/jobs":
            return httpx.Response(200, json={"jobs": list(self.jobs.values())})
        return httpx.Response(404, json={})


async def test_replan_pushes_constraints_to_other_armed_jobs(db, tmp_path):
    fake = FakeBooker()
    ab = AutoBook(settings(tmp_path), db, BookerClient("http://b", "", httpx.MockTransport(fake.handler)))
    await ab.check_health()
    p = Planner(settings(tmp_path), db, geo=GEO, visit_minutes=30, autobook=ab, now=lambda: NOW)
    ab.planner = p
    hooks.on("booking_confirmed", p.on_booking_confirmed)
    clara = add_event(db, "Clara del Rey", "Clara", armed=True)
    alma = add_event(db, "Alma", "Alma", armed=True)
    cobo = add_event(db, "Cobo Calleja", "Cobo", armed=True, opens_in=timedelta(days=1))
    await ab.arm(alma)
    assert fake.posts[-1]["constraints"]["avoid"] == []

    fake.posts.clear()
    await bookings.record_booking(db, clara, at("11:00"), at("11:30"), via="auto")
    by_id = {b["event_id"]: b for b in fake.posts}
    assert set(by_id) == {alma, cobo}  # not the one that was just booked
    assert by_id[alma]["constraints"]["avoid"] == [["10:05", "11:55"]]
    assert by_id[cobo]["constraints"]["avoid"] == [["09:55", "12:05"]]

    # sync: the booker already has the fresh constraints (no re-post); the booked one is disarmed
    fake.posts.clear()
    await ab.sync()
    assert fake.posts == []
    assert ("DELETE", f"/book/{clara}") in fake.calls
    with db.session() as s:
        assert s.query(BookingWatch).filter_by(event_id=clara).one().autobook is False


# ----------------------------------------------------------------------- /plan
class Msg:
    def __init__(self):
        self.html: list[str] = []
        self.text: list[str] = []

    async def reply_html(self, text, **kw):  # noqa: ANN001, ANN003
        self.html.append(text)

    async def reply_text(self, text, **kw):  # noqa: ANN001, ANN003
        self.text.append(text)


class Upd:
    def __init__(self):
        self.effective_message = Msg()


class Ctx:
    def __init__(self, args):  # noqa: ANN001
        self.args = args


async def test_plan_command_shows_route_drives_and_conflicts(db, tmp_path):
    add_event(db, "Alma Market Chamberí", "Alma", booked=("10:00", "10:30"))
    add_event(db, "Circular Market Clara del Rey", "Clara", booked=("11:00", "11:30"))
    add_event(db, "Home Market Cobo Calleja", "Cobo", booked=("12:00", "12:30"))
    add_event(db, "My Market", "MyMarket", armed=True)
    p = planner(db, tmp_path)
    upd = Upd()
    await p.cmd_plan(upd, Ctx(["13/11"]))
    out = upd.effective_message.html[0]
    assert "Fri 13 Nov" in out
    assert out.index("Alma Market") < out.index("Clara del Rey") < out.index("Cobo Calleja")
    assert "🚗 25′ drive, 8.4 km · leave 10:30 · 5′ to spare" in out
    assert "⚠️ <b>conflict</b>: you'd arrive 12:05, 5′ late" in out
    assert "Armed, not booked yet" in out and "My Market" in out and "avoiding" in out

    upd = Upd()
    await p.cmd_plan(upd, Ctx([]))  # defaults to the next day with bookings
    assert "Fri 13 Nov" in upd.effective_message.html[0]
    upd = Upd()
    await p.cmd_plan(upd, Ctx(["nonsense"]))
    assert "Usage" in upd.effective_message.text[0]

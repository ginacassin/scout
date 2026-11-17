"""/route: order a day's markets by car around booked slots, opening hours and lunch."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from scout.db import Account, Database, Event
from scout.geo import Leg
from scout.route import LUNCH, RoutePlanner, Stop, hm, plan_options, solve

TZ = ZoneInfo("Europe/Madrid")


def H(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def matrix_leg(m: dict[tuple[int, int], int]):
    def leg(a, b):
        if a is None or b is None or a == b:
            return Leg(0, 0.0, "same")
        return Leg(m.get((a, b), m.get((b, a), 30)), 1.0, "osrm")
    return leg


def market(i, title, opens="10:00", closes="14:00", stay=30):
    return Stop(f"m{i}", title, "market", i, [(H(opens), H(closes))], stay)


def fixed(i, title, at, stay=30):
    return Stop(f"f{i}", title, "fixed", i, stay=stay, start=H(at), leave=H(at) + stay)


def order(route):
    return [v.stop.title for v in route.visits]


def test_nearest_first_with_least_driving():
    # 0 and 1 are close together, 2 is far from both
    leg = matrix_leg({(0, 1): 5, (0, 2): 40, (1, 2): 38})
    r = solve([market(2, "Far"), market(0, "A"), market(1, "B")], leg)
    assert r.drive == 5 + 38 and order(r)[-1] == "Far" or order(r)[0] == "Far"
    assert r.drive == 43


def test_gap_between_two_bookings_gets_a_nearby_market():
    leg = matrix_leg({(0, 1): 15, (0, 2): 10, (1, 2): 10})
    stops = [fixed(0, "Booked 10", "10:00"), fixed(1, "Booked 13", "13:00"),
             market(2, "Nearby", "10:00", "19:00")]
    r = solve(stops, leg)
    assert order(r) == ["Booked 10", "Nearby", "Booked 13"]
    nearby = r.visits[1]
    assert nearby.begin == H("10:40") and nearby.leave == H("11:10")


def test_market_after_closing_is_impossible_and_waits_for_opening():
    leg = matrix_leg({(0, 1): 10})
    r = solve([market(0, "Morning", "10:00", "11:00"), market(1, "Afternoon", "17:00", "20:00")], leg)
    assert order(r) == ["Morning", "Afternoon"]
    assert r.visits[1].begin == H("17:00")
    assert solve([fixed(0, "Booked", "10:20"), market(1, "Tight", "10:00", "10:45")], leg) is None


def test_lunch_hour_around_two_wherever_you_are():
    leg = matrix_leg({(0, 1): 10})
    r, opts, problem = plan_options([], [market(0, "A", "12:00", "14:00"), market(1, "B", "16:00", "19:00")], leg)
    titles = order(r)
    assert titles == ["A", "Lunch", "B"]
    lunch = r.visits[1]
    assert H("13:30") <= lunch.begin <= H("14:30") and lunch.leave - lunch.begin == 60


def test_lunch_dropped_from_the_end_when_the_day_is_over():
    leg = matrix_leg({(0, 1): 10})
    r, _, _ = plan_options([], [market(0, "A", "10:00", "11:00"), market(1, "B", "10:00", "12:00")], leg)
    assert "Lunch" not in order(r) and not r.lunch


def test_preference_goes_first_even_if_it_costs_driving():
    leg = matrix_leg({(0, 1): 5, (0, 2): 40, (1, 2): 38})
    stops = [market(0, "A", "10:00", "19:00"), market(1, "B", "10:00", "19:00"), market(2, "Far", "10:00", "19:00")]
    r = solve(stops, leg, prefs=["m2"])
    assert order(r)[0] == "Far"


def test_too_much_for_one_day_offers_options():
    leg = matrix_leg({(0, 1): 20, (0, 2): 20, (1, 2): 20, (0, 3): 20, (1, 3): 20, (2, 3): 20})
    fixed_stops = [fixed(0, "Booked", "12:00", stay=60)]
    markets = [market(1, "M1", "11:00", "12:30"), market(2, "M2", "13:00", "14:15"), market(3, "M3", "11:00", "11:40")]
    best, options, problem = plan_options(fixed_stops, markets, leg, start=H("11:00"))
    assert best is None and options and problem is None
    assert len(options) <= 3
    for o in options:
        assert "Booked" in order(o)
        assert o.label
    assert any(o.dropped for o in options)


def test_clashing_bookings_are_reported():
    leg = matrix_leg({(0, 1): 45})
    _, options, problem = plan_options([fixed(0, "A", "10:00"), fixed(1, "B", "10:45")], [], leg)
    assert options == [] and "clash" in problem


def test_home_start_and_return():
    leg = matrix_leg({(0, 1): 10, (0, 2): 20, (1, 2): 25})
    r = solve([market(0, "A", "10:00", "14:00"), market(1, "B", "10:00", "14:00")], leg, home=2)
    assert r.home_leave == r.visits[0].begin - r.visits[0].drive.minutes
    assert r.home_back is not None and r.drive == 20 + 10 + 25 or r.drive == 25 + 10 + 20


# ------------------------------------------------------------------ bot side
class FakeGeo:
    def __init__(self, places):
        self.places = places

    async def locate(self, address, area):
        return self.places.get(address)

    async def geocode(self, q):
        return self.places.get(q)

    async def warm(self, points):
        return 0

    async def leg(self, a, b):
        if a is None or b is None:
            return Leg(30, None, "unknown")
        km = abs(a[0] - b[0]) * 100 + abs(a[1] - b[1]) * 100
        return Leg(int(km * 2) + 5, round(km, 1), "osrm")


class FakeCalendar:
    def __init__(self, removed):
        self.removed = removed

    async def removed_on(self, event_id, day):
        return event_id in self.removed


def world(removed=()):
    db = Database(":memory:")
    day = date(2026, 11, 21)
    with db.session() as s:
        acc = Account(handle="m")
        s.add(acc)
        s.flush()
        rows = []
        for title, addr, hours, decision in (
                ("Alma flat", "Serrano 1", ("10:00", "14:00"), "yes"),
                ("Batres barn", "Gacela 1", ("11:00", "18:00"), "yes"),
                ("Skipped one", "Caribe 5", None, "yes"),
                ("Not going", "Velázquez 9", ("10:00", "14:00"), "maybe")):
            e = Event(account_id=acc.id, title=title, address=addr, dates="[]", status="announced",
                      user_decision=decision)
            e.date_list = [day]
            e.session_list = [{"date": day.isoformat(), "start": hours[0], "end": hours[1],
                               "booking_required": False}] if hours else []
            s.add(e)
            s.flush()
            rows.append(e.id)
        b = Event(account_id=acc.id, title="Booked loft", address="Prado 2", dates="[]", status="announced",
                  user_decision="yes", booked_start=datetime(2026, 11, 21, 11, 0, tzinfo=TZ).astimezone(timezone.utc))
        b.date_list = [day, date(2026, 11, 22)]
        s.add(b)
        s.flush()
        rows.append(b.id)
    places = {"Serrano 1": (40.43, -3.68), "Gacela 1": (40.20, -3.95), "Caribe 5": (40.47, -3.66),
              "Prado 2": (40.41, -3.69)}
    app = SimpleNamespace(bot_data={"calendar": FakeCalendar({rows[2]} if "skip" in removed else set())})
    settings = SimpleNamespace(tz=TZ, allowed_chat_id=1)
    rp = RoutePlanner(app, settings, db, geo=FakeGeo(places),
                      now=lambda: datetime(2026, 11, 20, 12, tzinfo=timezone.utc))
    return rp, day, rows


async def test_build_uses_calendar_markets_and_booked_slot():
    rp, day, rows = world(removed=("skip",))
    plan = await rp.build(day)
    assert [s.title for s in plan.fixed] == ["Booked loft"]
    assert sorted(s.title for s in plan.markets) == ["Alma flat", "Batres barn"]
    assert plan.removed == ["Skipped one"]
    rp.compute(plan)
    assert plan.best is not None
    titles = [v.stop.title for v in plan.best.visits]
    assert "Booked loft" in titles
    booked = next(v for v in plan.best.visits if v.stop.title == "Booked loft")
    assert hm(booked.begin) == "11:00"
    text = rp.route_text(plan, plan.best)
    assert "Booked loft" in text and "google.com/maps/dir" in text and "deleted from your calendar" in text


async def test_unknown_hours_are_assumed_and_flagged():
    rp, day, rows = world()
    plan = await rp.build(day)
    skipped = next(s for s in plan.markets if s.title == "Skipped one")
    assert skipped.windows == [(H("10:00"), H("20:00"))]
    assert any("unknown" in n for n in skipped.notes)


async def test_other_day_of_a_booked_market_is_not_on_the_route():
    rp, _, _ = world()
    plan = await rp.build(date(2026, 11, 22))
    assert not plan.fixed and not plan.markets


def test_lunch_constant_is_around_two():
    (lo, hi), stay = LUNCH
    assert hm(lo) == "13:30" and hm(hi) == "14:30" and stay == 60

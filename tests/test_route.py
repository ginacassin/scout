"""/route: order a day's markets by car around booked slots, opening hours and lunch."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from scout.db import Account, Database, Event
from scout.geo import Leg
from scout.route import LUNCH, RoutePlanner, Stop, hm, parse_home, plan_options, solve, traffic_factor

TZ = ZoneInfo("Europe/Madrid")


def H(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def matrix_leg(m: dict[tuple[int, int], int]):
    def leg(a, b, t=0):
        if a is None or b is None or a == b:
            return Leg(0, 0.0, "same")
        return Leg(m.get((a, b), m.get((b, a), 30)), 1.0, "osrm")
    return leg


def market(i, title, opens="10:00", closes="14:00", stay=30):
    return Stop(f"m{i}", title, "market", i, [(H(opens), H(closes))], stay)


def fixed(i, title, at, stay=30, queue=0):
    return Stop(f"f{i}", title, "fixed", i, stay=stay, start=H(at), leave=H(at) + stay, queue=queue)


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


def test_clashing_bookings_are_named_and_each_alternative_drops_one():
    leg = matrix_leg({(0, 1): 40, (0, 2): 10, (1, 2): 35, (0, 3): 15, (1, 3): 30, (2, 3): 10})
    fixed_stops = [fixed(0, "Batres", "11:00"), fixed(1, "Tres Olivos", "11:00"), fixed(2, "NOK", "12:20")]
    best, options, problem = plan_options(fixed_stops, [market(3, "Flat", "10:00", "19:00")], leg)
    assert best is None and "Batres 11:00 and Tres Olivos 11:00" in problem
    assert len(options) == 2
    assert all(len([d for d in o.dropped if d.kind == "fixed"]) == 1 for o in options)
    assert {o.dropped[0].title for o in options} == {"Batres", "Tres Olivos"}
    assert all("NOK" in order(o) and "Flat" in order(o) for o in options)


def test_home_start_and_return():
    leg = matrix_leg({(0, 1): 10, (0, 2): 20, (1, 2): 25})
    r = solve([market(0, "A", "10:00", "14:00"), market(1, "B", "10:00", "14:00")], leg, home=2)
    assert r.home_leave == r.visits[0].begin - r.visits[0].drive.minutes
    assert r.home_back is not None and r.drive == 20 + 10 + 25 or r.drive == 25 + 10 + 20


# ------------------------------------------------------------------ bot side
class FakeGeo:
    parking = 5
    default_minutes = 30

    def __init__(self, places):
        self.places = places

    async def osrm(self, a, b):
        km = abs(a[0] - b[0]) * 100 + abs(a[1] - b[1]) * 100
        return km * 1.5, km

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



def test_booked_slot_needs_arriving_15_minutes_early():
    leg = matrix_leg({(0, 1): 20})
    m = market(0, "Flat", "10:00", "19:00")
    r = solve([m, fixed(1, "Booked", "11:00", queue=15)], leg, start=H("10:00"))
    assert order(r) == ["Booked", "Flat"]  # 10:00–10:30 then 20′ → 10:50, too late for a 10:45 queue
    r = solve([m, fixed(1, "Booked", "11:00", queue=0)], leg, start=H("10:00"))
    assert order(r) == ["Flat", "Booked"]


def test_traffic_profile():
    fri, sat, sun = date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)
    assert traffic_factor(fri, H("08:30")) > traffic_factor(fri, H("11:00")) > traffic_factor(fri, H("22:00"))
    assert traffic_factor(fri, H("18:30")) >= 1.6
    assert traffic_factor(sat, H("12:00")) > traffic_factor(sat, H("09:00"))
    assert traffic_factor(sun, H("09:00")) < traffic_factor(fri, H("09:00"))


def test_drive_times_follow_the_hour():
    rp, day, _ = world()
    import asyncio
    plan = asyncio.run(rp.build(day))
    plan.day = date(2026, 11, 20)  # a Friday: rush hour vs late evening
    a, b = 0, 1
    assert plan.leg(a, b, H("08:30")).minutes > plan.leg(a, b, H("22:00")).minutes


def test_parse_home():
    assert parse_home("C. del Miño 6, Alcobendas @40.544,-3.6388") == ("C. del Miño 6, Alcobendas", (40.544, -3.6388))
    assert parse_home("Calle Mayor 1") == ("Calle Mayor 1", None)


# ------------------------------------------------------------------ live mode
class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))


def live_world(monkeypatch, at="09:00"):
    rp, day, rows = world()
    clock = {"now": datetime(2026, 11, 21, *map(int, at.split(":")), tzinfo=TZ)}
    rp.now = lambda: clock["now"]
    rp.app.bot = Bot()
    monkeypatch.setenv("ROUTE_HOME", "Home 1 @40.54,-3.64")
    return rp, day, clock


async def test_alerts_before_leaving_and_extend_replans(monkeypatch):
    rp, day, clock = live_world(monkeypatch)
    plan = rp.compute(await rp.build(day))
    assert plan.best is not None and plan.best.home_leave is not None
    rp.activate(plan, plan.best)
    state = rp.active(day.isoformat())
    first = state["visits"][0]

    clock["now"] = datetime.combine(day, datetime.min.time(), TZ) + timedelta(minutes=state["home_leave"] - 5)
    assert await rp.tick() == ["home"]
    assert "Time to leave home" in rp.app.bot.sent[-1][0]

    clock["now"] = datetime.combine(day, datetime.min.time(), TZ) + timedelta(minutes=first["leave"] - 5)
    assert await rp.tick() == [first["key"]]
    text, markup = rp.app.bot.sent[-1]
    assert "Leave" in text and first["next"] in text and "google.com/maps/dir" in text
    labels = [b.text for b in markup.inline_keyboard[0]]
    assert labels == ["+10′", "+20′", "🚗 Leaving now"]
    assert await rp.tick() == []                        # once only

    new = await rp.replan(day.isoformat(), 0, first["leave"] + 20, alert_again=True)
    assert new.origin_title == first["title"] and new.start == first["leave"] + 20
    # 20′ more at the first market means missing the 11:00 booked slot (10:45 to queue): say so, offer the rest
    assert new.best is None and "miss a booked slot" in new.problem and "Booked loft" in new.problem
    assert new.options and new.options[0].dropped[0].title == "Booked loft"
    rp.activate(new, new.options[0])
    state = rp.active(day.isoformat())
    assert state["visits"][0]["leave"] == first["leave"] + 20 and state["visits"][0]["alerted"] is False
    assert [v["key"] for v in state["visits"][1:]]          # the rest of the day re-planned
    clock["now"] += timedelta(minutes=20)
    assert await rp.tick() == [first["key"]]            # reminded again at the new time


async def test_leaving_now_replans_from_here(monkeypatch):
    rp, day, clock = live_world(monkeypatch)
    plan = rp.compute(await rp.build(day))
    rp.activate(plan, plan.best)
    first = rp.active(day.isoformat())["visits"][0]
    new = await rp.replan(day.isoformat(), 0, first["leave"] - 10)
    best = new.best or new.options[0]
    assert best.visits[0].arrive <= first["leave"] - 10 + best.visits[0].drive.minutes
    rp.activate(new, best)
    assert rp.active(day.isoformat())["visits"][0]["alerted"] is True



async def test_after_work_start_straight_to_chosen_first_market(monkeypatch):
    monkeypatch.setenv("ROUTE_HOME", "Home 1 @40.54,-3.64")
    rp, day, rows = world()
    plan = await rp.build(day)
    rp.set_start(plan, H("17:00"), free=True)
    # Alma flat closes 14:00 and the 11:00 booked slot is over: both left out and named
    assert {s.title for s in plan.markets} == {"Batres barn", "Skipped one"}
    assert any("Alma flat" in c for c in plan.closed) and any("Booked loft" in c for c in plan.closed)
    rp.prefs[day.isoformat()] = [next(s.key for s in plan.markets if s.title == "Skipped one")]
    rp.compute(plan)
    r = plan.best or plan.options[0]
    assert r.visits[0].stop.title == "Skipped one"
    assert r.visits[0].begin == H("17:00") and r.home_leave is None    # no drive from home at the start
    assert r.home_back is not None                                       # still ends at home
    text = rp.route_text(plan, r)
    assert "Starting at <b>17:00</b>, straight to the first stop" in text and "Closed by then" in text


async def test_from_opening_keeps_everything(monkeypatch):
    monkeypatch.setenv("ROUTE_HOME", "Home 1 @40.54,-3.64")
    rp, day, rows = world()
    plan = await rp.build(day)
    rp.set_start(plan, None, free=False)
    assert len(plan.markets) == 3 and len(plan.fixed) == 1 and not plan.closed
    text, kb = rp.time_text(plan)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels[0] == "☀️ From opening" and "18:30" in labels

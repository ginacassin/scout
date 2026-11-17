"""Day route planner (/route): the order to visit every market on your calendar for a day, by car,
and a live mode on the day that tells you when to leave and re-plans when you stay longer.

What goes in, for the chosen day:
* Booked slots (Event.booked_start) are fixed: you must be there QUEUE_MINUTES (15) before the slot to
  queue, and stay VISIT_MINUTES or until the slot ends, whichever is later.
* Every other market you said ✅ to that runs that day, unless you deleted that day's entry in Google
  Calendar (= not going). Each needs VISIT_MINUTES inside its opening hours that day; hours that
  aren't known are assumed 10:00–20:00 and flagged.
* Lunch: an hour starting between 13:30 and 14:30, wherever you are at that point.
* Driving: OpenStreetMap routes × a Madrid traffic profile for the hour and weekday you leave
  (rush hours ×1.65–1.7, daytime ×1.25, Saturday shopping hours ×1.35, quiet ×1.1) + PARKING_MINUTES (5).
* Home: the day starts and ends there. ROUTE_HOME ("address @lat,lon" or an address) in the environment;
  /route home <address> overrides it.
* Your preference: markets you tap first are visited first, in the order tapped.

The order is the one with the least driving that fits every opening time and slot (then the earliest
finish). When not everything fits, you get up to three options and pick one.

Live mode: the plan you're shown for a day (or the option you pick) becomes that day's route. On the
day, ROUTE_ALERT_MINUTES (5) before each departure you get "time to leave" with the next stop, its
drive and a Google Maps link, and buttons +10′ / +20′ / 🚗 Leaving now. Each re-plans the rest of the
day from where you are; if something no longer fits you get the options again.

The search is exact: a dynamic programme over (stops visited, last stop, where you are), keeping for
each state only plans that are not both later and longer-driven than another.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from typing import Callable
from urllib.parse import quote

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from .db import Database, Event, utcnow
from .geo import FALLBACK_KMH, SAME_PLACE_KM, SAME_PLACE_MINUTES, Geo, Leg, Point, haversine_km
from .planner import _hm, departure, parse_day, visit_minutes_from_env

log = logging.getLogger(__name__)

HOME_KEY = "route_home"
ACTIVE_KEY = "route_active:{}"
DEFAULT_HOURS = (10 * 60, 20 * 60)
LUNCH = ((13 * 60 + 30, 14 * 60 + 30), 60)          # start window, minutes
LUNCH_FLEX = ((13 * 60, 15 * 60 + 30), 30)
MAX_OPTIONS = 3
MAX_DROP_COMBOS = 80


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


QUEUE_MINUTES = _env_int("QUEUE_MINUTES", 15)
ALERT_MINUTES = _env_int("ROUTE_ALERT_MINUTES", 5)


def traffic_factor(day: date, minute: int) -> float:
    """Typical Madrid traffic when leaving at `minute` (since midnight) on `day`."""
    h = (minute % (24 * 60)) / 60
    wd = day.weekday()
    if wd < 5:
        if 7 <= h < 10:
            return 1.7
        if 13.5 <= h < 15.5:
            return 1.45
        if 17.5 <= h < 20.5:
            return 1.65
        if 10 <= h < 21:
            return 1.25
        return 1.1
    if wd == 5:
        return 1.35 if (11 <= h < 14.5 or 17 <= h < 21) else 1.15
    return 1.25 if 12 <= h < 15 else 1.1


# ============================================================================ the search
@dataclass
class Stop:
    key: str
    title: str
    kind: str                      # fixed | market | lunch
    loc: int | None = None         # index of its point; None = wherever you are (lunch)
    windows: list[tuple[int, int]] = field(default_factory=list)  # market: open–close; lunch: start range
    stay: int = 30
    start: int | None = None       # fixed: slot time
    leave: int | None = None       # fixed: when you go on
    queue: int = 0                 # fixed: be there this much before the slot
    event_id: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def arrive_by(self) -> int | None:
        return None if self.start is None else self.start - self.queue


@dataclass
class Visit:
    stop: Stop
    arrive: int
    begin: int
    leave: int
    drive: Leg | None


@dataclass
class Route:
    visits: list[Visit]
    drive: int
    finish: int
    label: str = ""
    dropped: list[Stop] = field(default_factory=list)
    home_leave: int | None = None
    home_back: Leg | None = None
    lunch: bool = True

    @property
    def end(self) -> int:
        return self.finish + (self.home_back.minutes if self.home_back else 0)


LegFn = Callable[[int | None, int | None, int], Leg]   # from loc, to loc, leaving at (minutes)
FREE = -1   # origin: you make your own way to the first stop (e.g. straight from work) — no first drive


def _first(origin: int | None, home: int | None) -> int | None:
    if origin == FREE:
        return None
    return origin if origin is not None else home


def _enter(stop: Stop, arrive: int) -> tuple[int, int] | None:
    """(begin, leave) at this stop when arriving at `arrive`, or None if it doesn't fit."""
    if stop.kind == "fixed":
        return (stop.start, stop.leave) if arrive <= stop.arrive_by else None
    if stop.kind == "lunch":
        lo, hi = stop.windows[0]
        begin = max(arrive, lo)
        return (begin, begin + stop.stay) if begin <= hi else None
    for lo, hi in sorted(stop.windows):
        begin = max(arrive, lo)
        if begin + stop.stay <= hi:
            return begin, begin + stop.stay
    return None


def solve(stops: list[Stop], leg: LegFn, start: int = 0, home: int | None = None,
          prefs: list[str] | None = None, origin: int | None = None) -> Route | None:
    """Best order visiting every stop, or None when no order fits. You set off from `origin`
    (default: home) at `start` or later, and finish at home when there is one."""
    first_loc = _first(origin, home)
    n = len(stops)
    if n == 0:
        return Route([], 0, start)
    prefs = [p for p in (prefs or []) if any(s.key == p for s in stops)]
    pref_idx = {s.key: i for i, s in enumerate(stops) if s.key in prefs}
    pref_order = [pref_idx[p] for p in prefs]
    fixed = [i for i, s in enumerate(stops) if s.kind == "fixed"]

    # label: (time, drive, parent_state, parent_label_index, stop index, arrive, begin, leg)
    State = tuple[int, int, int | None]   # mask, last, loc
    labels: dict[State, list[tuple]] = {(0, -1, first_loc): [(start, 0, None, None, None, None, None, None)]}
    by_size: list[list[State]] = [[] for _ in range(n + 1)]
    by_size[0].append((0, -1, first_loc))

    def allowed(mask: int, j: int) -> bool:
        if not pref_order or stops[j].kind != "market":
            return True
        if j in pref_order:
            k = pref_order.index(j)
            return all(mask >> p & 1 for p in pref_order[:k])
        return all(mask >> p & 1 for p in pref_order)

    for size in range(n):
        for state in by_size[size]:
            mask, _last, loc = state
            for li, (t, drv, *_rest) in enumerate(labels[state]):
                for j in range(n):
                    if mask >> j & 1 or not allowed(mask, j):
                        continue
                    s = stops[j]
                    lg = leg(loc, s.loc, t) if s.loc is not None else None
                    d = lg.minutes if lg else 0
                    fit = _enter(s, t + d)
                    if fit is None:
                        continue
                    begin, leave = fit
                    new_loc = s.loc if s.loc is not None else loc
                    new_mask = mask | 1 << j
                    # every booked slot still to come must stay reachable
                    if any(not new_mask >> f & 1
                           and leave + leg(new_loc, stops[f].loc, leave).minutes > stops[f].arrive_by
                           for f in fixed):
                        continue
                    key = (new_mask, j, new_loc)
                    lab = (leave, drv + d, state, li, j, t + d, begin, lg)
                    cur = labels.get(key)
                    if cur is None:
                        labels[key] = [lab]
                        by_size[size + 1].append(key)
                    elif not any(o[0] <= leave and o[1] <= drv + d for o in cur):
                        cur[:] = [o for o in cur if not (leave <= o[0] and drv + d <= o[1])] + [lab]
                        del cur[8:]

    best = None
    for state in by_size[n]:
        mask, _last, loc = state
        for li, lab in enumerate(labels[state]):
            back = leg(loc, home, lab[0]) if home is not None else None
            score = (lab[1] + (back.minutes if back else 0), lab[0])
            if best is None or score < best[0]:
                best = (score, state, li, back)
    if best is None:
        return None
    (total, _), state, li, back = best
    visits: list[Visit] = []
    while state is not None and labels[state][li][4] is not None:
        t, _d, pstate, pli, j, arrive, begin, lg = labels[state][li]
        visits.append(Visit(stops[j], arrive, begin, t, lg))
        state, li = pstate, pli
    visits.reverse()
    route = Route(visits, total, visits[-1].leave, home_back=back)
    # Lunch after everything else isn't part of the route: the day is over by then.
    if visits and visits[-1].stop.kind == "lunch":
        visits.pop()
        route.finish = visits[-1].leave if visits else start
        route.lunch = False
        if home is not None and visits:
            route.home_back = leg(visits[-1].stop.loc, home, route.finish)
    if origin is None and home is not None and visits and visits[0].drive:
        v0 = visits[0]
        target = v0.stop.arrive_by if v0.stop.kind == "fixed" else v0.begin
        d = leg(home, v0.stop.loc, target - v0.drive.minutes)   # traffic at the real departure time
        route.home_leave = target - d.minutes
        v0.drive, v0.arrive = d, target
    return route


def plan_options(fixed: list[Stop], markets: list[Stop], leg: LegFn, start: int = 0, home: int | None = None,
                 prefs: list[str] | None = None, origin: int | None = None,
                 lunch: bool = True) -> tuple[Route | None, list[Route], str | None]:
    """(the route, or None; options when not everything fits; a problem to explain)."""
    def lunch_stop(spec) -> list[Stop]:  # noqa: ANN001
        if not lunch:
            return []
        (lo, hi), stay = spec
        return [Stop("lunch", "Lunch", "lunch", None, [(lo, hi)], stay)]

    def run(stops: list[Stop]) -> Route | None:
        return solve(stops, leg, start, home, prefs, origin)

    if fixed and run(fixed) is None:
        return None, *_without_clashing_slots(fixed, markets, leg, start, home, prefs, origin, lunch)
    full = run(fixed + markets + lunch_stop(LUNCH))
    if full is not None:
        return full, [], None
    options: list[Route] = []
    if lunch:
        r = run(fixed + markets + lunch_stop(LUNCH_FLEX))
        if r is not None:
            r.label = "Everything, with a 30′ lunch between 13:00 and 15:30"
            options.append(r)
    drops: list[Route] = []
    for k in range(1, len(markets) + 1):
        for combo in list(itertools.combinations(range(len(markets)), k))[:MAX_DROP_COMBOS]:
            keep = [m for i, m in enumerate(markets) if i not in combo]
            r = run(fixed + keep + lunch_stop(LUNCH))
            if r is not None:
                r.dropped = [markets[i] for i in combo]
                r.label = "Skip " + " and ".join(m.title for m in r.dropped)
                drops.append(r)
        if drops:
            break
    drops.sort(key=lambda r: (r.drive, r.end))
    options += drops[:2]
    if lunch and len(options) < MAX_OPTIONS:
        r = run(fixed + markets)
        if r is not None:
            r.label = "Everything, no lunch break"
            r.lunch = False
            options.append(r)
    return None, options[:MAX_OPTIONS], None if options else "Nothing fits, not even with a market left out."


def clashes(fixed: list[Stop], leg: LegFn) -> list[str]:
    """Booked slots you can't get between, in plain words."""
    out = []
    ordered = sorted(fixed, key=lambda s: s.start)
    for a, b in zip(ordered, ordered[1:]):
        lg = leg(a.loc, b.loc, a.leave)
        need = a.leave + lg.minutes
        if need > b.arrive_by:
            out.append(f"{a.title} {hm(a.start)} and {b.title} {hm(b.start)}: you'd reach the second at "
                       f"{hm(need)} ({lg.minutes}′ drive, {b.queue}′ queue)")
    return out


def _without_clashing_slots(fixed: list[Stop], markets: list[Stop], leg: LegFn, start: int, home: int | None,
                            prefs: list[str] | None, origin: int | None,
                            lunch: bool) -> tuple[list[Route], str]:
    """Booked slots that clash: plans that give up as few of them as possible."""
    reasons = clashes(fixed, leg)
    first_loc = _first(origin, home)
    if first_loc is not None and start > 0:
        f = min(fixed, key=lambda x: x.start)
        lg = leg(first_loc, f.loc, start)
        if start + lg.minutes > f.arrive_by:
            reasons.insert(0, f"leaving at {hm(start)} you'd reach {f.title} at {hm(start + lg.minutes)}, after "
                              f"{hm(f.arrive_by)} ({f.queue}′ queue before the {hm(f.start)} slot)")
            problem = "You'd miss a booked slot: " + "; ".join(reasons)
        else:
            problem = "Your booked slots clash: " + "; ".join(reasons or ["not enough time between them"])
    else:
        problem = "Your booked slots clash: " + "; ".join(reasons or ["not enough time between them"])
    found: list[Route] = []
    for k in range(1, len(fixed) + 1):
        for combo in itertools.combinations(range(len(fixed)), k):
            keep = [f for i, f in enumerate(fixed) if i not in combo]
            if solve(keep, leg, start, home, None, origin) is None:
                continue
            gone = [fixed[i] for i in combo]
            best, options, _ = plan_options(keep, markets, leg, start, home, prefs, origin, lunch)
            r = best or (options[0] if options else None)
            if r is None:
                continue
            extra = f" · {r.label}" if r.label else ""
            r.label = "Skip booked " + " and ".join(f"{g.title} {hm(g.start)}" for g in gone) + extra
            r.dropped = gone + r.dropped
            found.append(r)
        if found:
            break
    found.sort(key=lambda r: (len(r.dropped), r.drive, r.end))
    return found[:MAX_OPTIONS], problem


# ============================================================================ the bot side
def hm(minutes: int) -> str:
    minutes = max(0, minutes)
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def _minutes(dt: datetime, tz) -> int:  # noqa: ANN001
    local = dt.astimezone(tz)
    return local.hour * 60 + local.minute


def parse_home(text: str) -> tuple[str, Point | None]:
    """'C/ Example 1, Town @40.5,-3.6' → (address, point); a plain address → (address, None)."""
    m = re.search(r"@\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", text or "")
    if not m:
        return (text or "").strip(), None
    return text[:m.start()].strip(), (float(m.group(1)), float(m.group(2)))


@dataclass
class DayPlan:
    day: date
    fixed: list[Stop]
    markets: list[Stop]
    removed: list[str]
    points: list[Point | None]
    addresses: list[str]
    home: int | None
    raw: dict[tuple[int, int], tuple[float, float | None, str]]   # (a, b) → (minutes, km, source)
    start: int
    parking: float = 5
    best: Route | None = None
    options: list[Route] = field(default_factory=list)
    problem: str | None = None
    origin: int | None = None          # re-plan: where you are now
    origin_title: str | None = None
    prefix: list[dict] = field(default_factory=list)   # re-plan: the part of the day already done
    lunch_left: bool = True
    free_start: bool = False           # straight to the first stop (after work), no drive from home
    begin_at: int | None = None        # chosen start time (None = from opening)
    closed: list[str] = field(default_factory=list)    # closed by the time you start
    all_fixed: list[Stop] = field(default_factory=list)
    all_markets: list[Stop] = field(default_factory=list)

    def leg(self, a: int | None, b: int | None, t: int) -> Leg:
        if a is None or b is None or a == b or a < 0 or b < 0:
            return Leg(0, 0.0, "same")
        minutes, km, source = self.raw[(a, b)]
        if source == "same":
            return Leg(SAME_PLACE_MINUTES, km, "same")
        if source == "unknown":
            return Leg(math.ceil(minutes), None, "unknown")
        factor = traffic_factor(self.day, t) if source == "osrm" else 1.0
        return Leg(math.ceil(minutes * factor + self.parking), km, source)


class RoutePlanner:
    def __init__(self, app: Application, settings, db: Database, geo: Geo | None = None, now=None):  # noqa: ANN001
        self.app = app
        self.settings = settings
        self.db = db
        self.tz = settings.tz
        self.geo = geo or Geo(db)
        self.visit = visit_minutes_from_env()
        self.now = now or utcnow
        self.prefs: dict[str, list[str]] = {}       # day → stop keys, in the order you tapped
        self.plans: dict[str, DayPlan] = {}
        self._busy = asyncio.Lock()

    def register(self) -> None:
        only_me = filters.Chat(chat_id=self.settings.allowed_chat_id)
        self.app.add_handler(CommandHandler("route", self.cmd_route, filters=only_me))
        self.app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^rt:"))
        if self.app.job_queue is not None:
            self.app.job_queue.run_repeating(self.tick, interval=60, first=30, name="route-alerts")

    # ------------------------------------------------------------ what's on
    def _today(self) -> date:
        return self.now().astimezone(self.tz).date()

    def _now_min(self) -> int:
        local = self.now().astimezone(self.tz)
        return local.hour * 60 + local.minute

    def _calendar(self):  # noqa: ANN202
        return getattr(self.app, "bot_data", {}).get("calendar")

    def events_on(self, day: date) -> list[Event]:
        with self.db.session() as s:
            evs = list(s.scalars(select(Event).where(Event.status != "past")))
        out = []
        for e in evs:
            if e.booked_start is not None:
                if e.booked_start.astimezone(self.tz).date() == day:
                    out.append(e)
                continue  # booked for another day: only that slot is on your calendar
            if e.user_decision == "yes" and day in e.date_list:
                out.append(e)
        return out

    def default_day(self) -> date:
        today = self._today()
        local_now = self.now().astimezone(self.tz)
        for i in range(0, 60):
            d = today + timedelta(days=i)
            if i == 0 and local_now.hour >= 18:
                continue
            if self.events_on(d):
                return d
        return today

    def home_text(self) -> str | None:
        return ((self.db.get_setting(HOME_KEY) or "").strip()
                or os.environ.get("ROUTE_HOME", "").strip() or None)

    async def _home_point(self) -> tuple[str, Point] | None:
        text = self.home_text()
        if not text:
            return None
        address, point = parse_home(text)
        point = point or await self.geo.geocode(address)
        return (address, point) if point else None

    def _hours(self, ev: Event, day: date) -> tuple[list[tuple[int, int]], list[str]]:
        wins, notes = [], []
        for x in ev.session_list:
            if x.get("date") != day.isoformat():
                continue
            a, b = _hm(x.get("start")), _hm(x.get("end"))
            if not a and not b:
                continue
            lo = a.hour * 60 + a.minute if a else DEFAULT_HOURS[0]
            hi = b.hour * 60 + b.minute if b else lo + 240
            if hi <= lo:
                hi += 24 * 60
            wins.append((lo, hi))
            if not b:
                notes.append(f"closing time unknown (assumed {hm(hi)})")
        if not wins:
            wins = [DEFAULT_HOURS]
            notes.append(f"opening hours unknown (assumed {hm(DEFAULT_HOURS[0])}–{hm(DEFAULT_HOURS[1])})")
        return wins, notes

    async def build(self, day: date, check_calendar: bool = True) -> DayPlan:
        cal = self._calendar() if check_calendar else None
        fixed, markets, removed = [], [], []
        points: list[Point | None] = []
        addresses: list[str] = []
        for ev in sorted(self.events_on(day), key=lambda e: e.id):
            if ev.booked_start is None and cal is not None:
                try:
                    if await cal.removed_on(ev.id, day):
                        removed.append(ev.title)
                        continue
                except Exception:  # noqa: BLE001 - a calendar hiccup shouldn't stop the plan
                    log.warning("Calendar check for event %s failed", ev.id, exc_info=True)
            pt = await self.geo.locate(ev.address, ev.area)
            loc = len(points)
            points.append(pt)
            addresses.append(", ".join(x for x in (ev.address, ev.area) if x) or ev.title)
            notes = [] if pt else ["address not found: drive time guessed"]
            if ev.booked_start is not None:
                start = _minutes(ev.booked_start, self.tz)
                end = _minutes(ev.booked_end, self.tz) if ev.booked_end else None
                leave = departure(ev.booked_start, ev.booked_end, timedelta(minutes=self.visit))
                fixed.append(Stop(f"ev{ev.id}", ev.title, "fixed", loc, stay=self.visit, start=start,
                                  leave=_minutes(leave, self.tz), queue=QUEUE_MINUTES, event_id=ev.id,
                                  notes=notes + ([f"slot {hm(start)}–{hm(end)}"] if end else [])))
            else:
                wins, more = self._hours(ev, day)
                if ev.booking_method == "eventbrite" or any(
                        x.get("booking_required") for x in ev.session_list if x.get("date") == day.isoformat()):
                    more.append("🎟 needs a ticket — none booked")
                markets.append(Stop(f"ev{ev.id}", ev.title, "market", loc, wins, self.visit, event_id=ev.id,
                                    notes=notes + more))
        home = None
        hp = await self._home_point()
        if hp:
            home = len(points)
            points.append(hp[1])
            addresses.append(hp[0])
        await self.geo.warm([p for p in points if p])
        raw: dict[tuple[int, int], tuple[float, float | None, str]] = {}
        for a in range(len(points)):
            for b in range(len(points)):
                if a != b:
                    raw[(a, b)] = await self._raw(points[a], points[b])
        start = self._now_min() if day == self._today() else 0
        return DayPlan(day, fixed, markets, removed, points, addresses, home, raw, start,
                       parking=getattr(self.geo, "parking", 5))

    async def _raw(self, a: Point | None, b: Point | None) -> tuple[float, float | None, str]:
        if a is None or b is None:
            return float(getattr(self.geo, "default_minutes", 30)), None, "unknown"
        straight = haversine_km(a, b)
        if straight < SAME_PLACE_KM:
            return 0.0, round(straight, 1), "same"
        routed = await self.geo.osrm(a, b)
        if routed is not None:
            return routed[0], round(routed[1], 1), "osrm"
        return straight / FALLBACK_KMH * 60, round(straight, 1), "estimate"

    def compute(self, plan: DayPlan) -> DayPlan:
        prefs = self.prefs.get(plan.day.isoformat(), [])
        origin = FREE if plan.free_start and plan.origin is None else plan.origin
        plan.best, plan.options, plan.problem = plan_options(plan.fixed, plan.markets, plan.leg, plan.start,
                                                             plan.home, prefs, origin, plan.lunch_left)
        return plan

    def set_start(self, plan: DayPlan, begin_at: int | None, free: bool | None = None) -> None:
        """Start the day at `begin_at` (None = from opening); leave out what's over by then."""
        if not plan.all_fixed and not plan.all_markets:
            plan.all_fixed, plan.all_markets = list(plan.fixed), list(plan.markets)
        plan.begin_at = begin_at
        today = plan.day == self._today()
        plan.start = max(begin_at or 0, self._now_min() if today else 0)
        if free is not None:
            plan.free_start = free
        plan.lunch_left = plan.start <= LUNCH[0][1]   # starting after lunchtime: no lunch break
        t = plan.start
        plan.fixed = [f for f in plan.all_fixed if f.arrive_by >= t]
        plan.markets = [m for m in plan.all_markets if any(hi >= max(lo, t) + m.stay for lo, hi in m.windows)]
        plan.closed = ([f"{f.title} (booked {hm(f.start)})" for f in plan.all_fixed if f not in plan.fixed]
                       + [f"{m.title} (closes {hm(max(hi for _, hi in m.windows))})"
                          for m in plan.all_markets if m not in plan.markets])

    # ------------------------------------------------------------ messages
    @staticmethod
    def _where(plan: DayPlan, loc: int) -> str:
        p = plan.points[loc]
        return f"{p[0]:.6f},{p[1]:.6f}" if p else plan.addresses[loc]

    def _maps_link(self, plan: DayPlan, route: Route) -> str | None:
        locs = [v.stop.loc for v in route.visits if v.stop.loc is not None]
        locs = [x for i, x in enumerate(locs) if i == 0 or locs[i - 1] != x]
        start = plan.origin if plan.origin is not None else (None if plan.free_start else plan.home)
        if start is not None:
            locs = [start] + locs
        if plan.home is not None:
            locs = locs + [plan.home]
        if len(locs) < 2:
            return None
        origin, dest, mid = locs[0], locs[-1], locs[1:-1][:9]
        url = (f"https://www.google.com/maps/dir/?api=1&travelmode=driving&origin={quote(self._where(plan, origin))}"
               f"&destination={quote(self._where(plan, dest))}")
        if mid:
            url += "&waypoints=" + quote("|".join(self._where(plan, x) for x in mid))
        return url

    def route_text(self, plan: DayPlan, route: Route, title: str | None = None) -> str:
        day = plan.day.strftime("%a %-d %b")
        lines = [f"🗺 <b>{escape(title or 'Your route')} — {day}</b>"]
        if plan.origin_title:
            lines.append(f"From {escape(plan.origin_title)}, leaving <b>{hm(plan.start)}</b>")
        elif plan.free_start and route.visits:
            lines.append(f"Starting at <b>{hm(route.visits[0].arrive)}</b>, straight to the first stop")
        elif route.home_leave is not None:
            lines.append(f"🏠 Leave home at <b>{hm(route.home_leave)}</b>")
        prev_leave = plan.start if plan.origin_title else None
        if route.home_leave is not None and not plan.origin_title:
            prev_leave = route.home_leave
        for v in route.visits:
            if v.drive and v.drive.minutes and prev_leave is not None:
                km = f" · {v.drive.km:g} km" if v.drive.km else ""
                guess = " (guessed)" if v.drive.guessed else ""
                lines.append(f"   🚗 {v.drive.minutes}′{km}{guess}")
            s = v.stop
            if prev_leave is not None:
                ready = s.arrive_by if s.kind == "fixed" else v.begin
                spare = ready - v.arrive
                if spare >= 10:
                    lines.append(f"   ⏳ {spare}′ free" + (" (wait for opening)" if s.kind == "market" else ""))
            if s.kind == "lunch":
                lines.append(f"🍽 <b>{hm(v.begin)}–{hm(v.leave)}</b>  Lunch nearby")
            elif s.kind == "fixed":
                lines.append(f"🎟 <b>{hm(v.begin)}–{hm(v.leave)}</b>  {escape(s.title)} <i>(booked — be there by "
                             f"{hm(s.arrive_by)} to queue)</i>")
            else:
                lines.append(f"📍 <b>{hm(v.begin)}–{hm(v.leave)}</b>  {escape(s.title)}")
            if s.kind != "lunch":
                where = plan.addresses[s.loc] if s.loc is not None else ""
                if where:
                    lines.append(f"   {escape(where)}")
                if s.kind == "market":
                    lines.append("   open " + ", ".join(f"{hm(a)}–{hm(b)}" for a, b in s.windows))
                for note in s.notes:
                    lines.append(f"   {escape(note)}" if note.startswith(("🎟", "slot")) else f"   ⚠️ {escape(note)}")
            prev_leave = v.leave
        if route.home_back:
            lines.append(f"   🚗 {route.home_back.minutes}′ home · back at <b>{hm(route.end)}</b>")
        lines.append("")
        lines.append(f"Driving {route.drive}′ in total (with traffic for the time of day, incl. parking)"
                     + ("" if route.lunch or not plan.lunch_left else " · no lunch break in this plan"))
        if route.dropped:
            lines.append("Left out: " + ", ".join(escape(s.title) for s in route.dropped))
        if plan.closed and not plan.origin_title:
            lines.append("Closed by then: " + ", ".join(escape(t) for t in plan.closed))
        if plan.removed and not plan.origin_title:
            lines.append("Not counted (deleted from your calendar): " + ", ".join(escape(t) for t in plan.removed))
        link = self._maps_link(plan, route)
        if link:
            lines.append(f'<a href="{link}">Open in Google Maps</a>')
        return "\n".join(lines)

    def options_text(self, plan: DayPlan) -> str:
        day = plan.day.strftime("%a %-d %b")
        lines = [f"🗺 <b>{day}: not everything fits</b>"]
        if plan.problem:
            lines.append(escape(plan.problem) + ". Pick one:")
        else:
            lines.append(f"{len(plan.markets) + len(plan.fixed)} stops with the opening times, your slots and "
                         f"lunch can't all be done. Pick one:")
        for i, r in enumerate(plan.options):
            order = " → ".join(escape(v.stop.title if v.stop.kind != "lunch" else "🍽") for v in r.visits)
            lines.append(f"\n<b>{chr(65 + i)}. {escape(r.label)}</b>\n   {order}\n   driving {r.drive}′, "
                         f"done {hm(r.end)}")
        if plan.closed and not plan.origin_title:
            lines.append("\nClosed by then: " + ", ".join(escape(t) for t in plan.closed))
        if plan.removed and not plan.origin_title:
            lines.append("\nNot counted (deleted from your calendar): " + ", ".join(escape(t) for t in plan.removed))
        return "\n".join(lines)

    def ask_text(self, plan: DayPlan) -> tuple[str, InlineKeyboardMarkup]:
        iso = plan.day.isoformat()
        prefs = self.prefs.get(iso, [])
        lines = [f"🗺 <b>Route for {plan.day.strftime('%a %-d %b')}</b>"]
        if plan.fixed:
            lines.append("Booked (fixed): " + ", ".join(
                f"{escape(s.title)} {hm(s.start)}" for s in sorted(plan.fixed, key=lambda s: s.start)))
        when = "from opening" if plan.begin_at is None else f"at {hm(plan.start)}"
        lines.append(f"Starting {when}. To fit in: {len(plan.markets)} markets.")
        if plan.closed:
            lines.append("Closed by then: " + ", ".join(escape(t) for t in plan.closed))
        if plan.removed:
            lines.append("Not counted (deleted from your calendar): " + ", ".join(escape(t) for t in plan.removed))
        lines.append("\nWhere do you want to start? Tap it (and any others in order), then <b>Plan route</b>.")
        rows = []
        for s in plan.markets:
            n = prefs.index(s.key) + 1 if s.key in prefs else None
            rows.append([InlineKeyboardButton(f"{n}️⃣ {s.title}"[:60] if n else s.title[:60],
                                              callback_data=f"rt:p:{iso}:{s.key}")])
        if plan.home is not None:
            rows.append([InlineKeyboardButton(("✅ " if not plan.free_start else "") + "🏠 From home",
                                              callback_data=f"rt:s:{iso}:home"),
                         InlineKeyboardButton(("✅ " if plan.free_start else "") + "📍 Straight to 1st",
                                              callback_data=f"rt:s:{iso}:free")])
        rows.append([InlineKeyboardButton("🚗 Plan route" if prefs else "🚗 No preference — plan it",
                                          callback_data=f"rt:go:{iso}")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def time_text(self, plan: DayPlan) -> tuple[str, InlineKeyboardMarkup]:
        iso = plan.day.isoformat()
        text = (f"🗺 <b>Route for {plan.day.strftime('%a %-d %b')}</b>\n"
                f"{len(plan.all_fixed or plan.fixed)} booked, {len(plan.all_markets or plan.markets)} other markets."
                f"\n\nWhen do you start? (any other time: <code>/route {plan.day.strftime('%-d/%-m')} 18:15</code>)")
        times = [17 * 60, 17 * 60 + 30, 18 * 60, 18 * 60 + 30, 19 * 60, 19 * 60 + 30]
        rows = [[InlineKeyboardButton("☀️ From opening", callback_data=f"rt:t:{iso}:open")]]
        if plan.day == self._today():
            rows[0].append(InlineKeyboardButton("Now", callback_data=f"rt:t:{iso}:{self._now_min()}"))
            times = [t for t in times if t > self._now_min()]
        rows += [[InlineKeyboardButton(hm(t), callback_data=f"rt:t:{iso}:{t}") for t in times[i:i + 3]]
                 for i in range(0, len(times), 3)]
        return text, InlineKeyboardMarkup(rows)

    async def _after_start(self, plan: DayPlan, edit) -> None:  # noqa: ANN001
        """Start chosen: ask where to start (2+ markets) or plan straight away."""
        if not plan.fixed and not plan.markets:
            text = f"🗺 Nothing left to visit by then on {plan.day.strftime('%a %-d %b')}."
            if plan.closed:
                text += "\nClosed by then: " + ", ".join(plan.closed)
            await edit.edit_message_text(text)
            return
        if len(plan.markets) >= 2 or (plan.markets and plan.home is not None and plan.begin_at is not None):
            text, kb = self.ask_text(plan)
            await edit.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return
        await self._send_plan(await asyncio.to_thread(self.compute, plan), edit=edit)

    async def _send_plan(self, plan: DayPlan, edit=None, title: str | None = None) -> None:  # noqa: ANN001
        iso = plan.day.isoformat()
        self.plans[iso] = plan
        markup = None
        if plan.best is not None:
            self.activate(plan, plan.best)
            text = self.route_text(plan, plan.best, title)
            if plan.day == self._today() or plan.day == self._today() + timedelta(days=1):
                text += f"\n\n🔔 On the day I'll tell you {ALERT_MINUTES}′ before each time to leave."
        elif plan.options:
            text = self.options_text(plan)
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                f"{chr(65 + i)}", callback_data=f"rt:o:{iso}:{i}") for i in range(len(plan.options))]])
        else:
            text = f"🗺 <b>{plan.day.strftime('%a %-d %b')}</b>\n{escape(plan.problem or 'Nothing to plan.')}"
        if edit is not None:
            await edit.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        else:
            await self.app.bot.send_message(self.settings.allowed_chat_id, text, parse_mode="HTML",
                                            reply_markup=markup, disable_web_page_preview=True)

    # ------------------------------------------------------------ live mode
    def activate(self, plan: DayPlan, route: Route) -> None:
        """This becomes the day's route: alerts follow it."""
        visits = [dict(v) for v in plan.prefix]
        if visits:  # where you are now: its next stop is the first of the new plan
            here = visits[-1]
            if route.visits:
                v0 = route.visits[0]
                here["next"] = v0.stop.title if v0.stop.kind != "lunch" else "lunch"
                here["next_drive"] = v0.drive.minutes if v0.drive else 0
                here["next_dest"] = self._where(plan, v0.stop.loc) if v0.stop.loc is not None else None
            elif plan.home is not None:
                here["next"], here["next_dest"] = "home", self._where(plan, plan.home)
                here["next_drive"] = route.home_back.minutes if route.home_back else 0
            else:
                here["next"] = None
        for i, v in enumerate(route.visits):
            nxt = route.visits[i + 1] if i + 1 < len(route.visits) else None
            loc = v.stop.loc
            if loc is None:  # lunch: you're where the previous stop was
                loc = next((x.stop.loc for x in reversed(route.visits[:i]) if x.stop.loc is not None), plan.origin)
            if nxt is not None:
                nxt_title = nxt.stop.title if nxt.stop.kind != "lunch" else "lunch"
                nxt_drive = nxt.drive.minutes if nxt.drive else 0
                nxt_dest = self._where(plan, nxt.stop.loc) if nxt.stop.loc is not None else None
            elif plan.home is not None:
                nxt_title, nxt_drive = "home", (route.home_back.minutes if route.home_back else 0)
                nxt_dest = self._where(plan, plan.home)
            else:
                nxt_title, nxt_drive, nxt_dest = None, 0, None
            visits.append({"key": v.stop.key, "title": v.stop.title, "kind": v.stop.kind, "begin": v.begin,
                           "leave": v.leave, "loc_addr": plan.addresses[loc] if loc is not None else None,
                           "next": nxt_title, "next_drive": nxt_drive, "next_dest": nxt_dest, "alerted": False})
        home_leave = route.home_leave if not plan.prefix else None
        state = {"day": plan.day.isoformat(), "visits": visits, "home_leave": home_leave,
                 "home_alerted": False, "first": visits[0]["title"] if visits else None,
                 "first_dest": (self._where(plan, route.visits[0].stop.loc)
                                if route.visits and route.visits[0].stop.loc is not None else None),
                 "first_drive": route.visits[0].drive.minutes if route.visits and route.visits[0].drive else 0}
        self.db.set_setting(ACTIVE_KEY.format(plan.day.isoformat()), json.dumps(state))

    def active(self, iso: str) -> dict | None:
        raw = self.db.get_setting(ACTIVE_KEY.format(iso))
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def _save_active(self, iso: str, state: dict) -> None:
        self.db.set_setting(ACTIVE_KEY.format(iso), json.dumps(state))

    @staticmethod
    def _go_link(dest: str | None) -> str:
        return (f'\n<a href="https://www.google.com/maps/dir/?api=1&travelmode=driving&destination={quote(dest)}">'
                f"Directions with live traffic</a>") if dest else ""

    def _alert_buttons(self, iso: str, i: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("+10′", callback_data=f"rt:x:{iso}:{i}:10"),
            InlineKeyboardButton("+20′", callback_data=f"rt:x:{iso}:{i}:20"),
            InlineKeyboardButton("🚗 Leaving now", callback_data=f"rt:n:{iso}:{i}"),
        ]])

    async def tick(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> list[str]:
        """Every minute: send the "time to leave" alerts of today's route."""
        iso = self._today().isoformat()
        state = self.active(iso)
        if not state:
            return []
        now = self._now_min()
        sent: list[str] = []
        hl = state.get("home_leave")
        if hl is not None and not state.get("home_alerted") and hl - ALERT_MINUTES <= now <= hl + 30:
            state["home_alerted"] = True
            self._save_active(iso, state)
            text = (f"🏠 Time to leave home at <b>{hm(hl)}</b> → {escape(state.get('first') or '')} "
                    f"({state.get('first_drive', 0)}′ by car){self._go_link(state.get('first_dest'))}")
            await self._send(text)
            sent.append("home")
        for i, v in enumerate(state["visits"]):
            if v.get("alerted") or v.get("next") is None:
                continue
            if v["leave"] - ALERT_MINUTES <= now <= v["leave"] + 30:
                v["alerted"] = True
                self._save_active(iso, state)
                where = "lunch" if v["kind"] == "lunch" else escape(v["title"])
                text = (f"⏰ Leave {where} at <b>{hm(v['leave'])}</b> → next: <b>{escape(v['next'])}</b> "
                        f"({v['next_drive']}′ by car){self._go_link(v.get('next_dest'))}")
                await self._send(text, self._alert_buttons(iso, i))
                sent.append(v["key"])
        return sent

    async def _send(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self.app.bot.send_message(self.settings.allowed_chat_id, text, parse_mode="HTML",
                                            reply_markup=markup, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.exception("Route alert failed")

    async def replan(self, iso: str, i: int, leave: int, alert_again: bool = False) -> DayPlan | None:
        """You leave stop i at `leave`: plan the rest of the day from there."""
        state = self.active(iso)
        if not state or i >= len(state["visits"]):
            return None
        day = date.fromisoformat(iso)
        plan = await self.build(day, check_calendar=False)
        by_key = {s.key: s for s in plan.fixed + plan.markets}
        here = state["visits"][i]
        rest = [v["key"] for v in state["visits"][i + 1:]]
        loc_key = next((v["key"] for v in reversed(state["visits"][:i + 1]) if v["kind"] != "lunch"), None)
        plan.origin = by_key[loc_key].loc if loc_key in by_key else plan.home
        plan.origin_title = "lunch" if here["kind"] == "lunch" else here["title"]
        plan.fixed = [by_key[k] for k in rest if k in by_key and by_key[k].kind == "fixed"]
        plan.markets = [by_key[k] for k in rest if k in by_key and by_key[k].kind == "market"]
        plan.lunch_left = "lunch" in rest
        plan.start = leave
        done = [dict(v) for v in state["visits"][:i + 1]]
        done[-1]["leave"] = leave
        done[-1]["alerted"] = not alert_again   # staying longer: remind again at the new time
        plan.prefix = done
        self.prefs.pop(iso, None)
        return await asyncio.to_thread(self.compute, plan)

    # ------------------------------------------------------------ handlers
    async def cmd_route(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        args = list(ctx.args or []) if ctx is not None else []
        if args and args[0].lower() in ("home", "casa"):
            rest = " ".join(args[1:]).strip()
            if not rest:
                home = self.home_text()
                await msg.reply_html(f"🏠 Home: <b>{escape(parse_home(home)[0])}</b>" if home else
                                     "No home set. <code>/route home C/ Example 1, Madrid</code> makes the day "
                                     "start and end there.")
            elif rest.lower() in ("off", "none", "clear"):
                self.db.set_setting(HOME_KEY, "")
                await msg.reply_text("Home override cleared." + (" Using the default home." if self.home_text() else ""))
            elif parse_home(rest)[1] is None and await self.geo.geocode(rest) is None:
                await msg.reply_text("I couldn't find that address on the map. Try adding the town.")
            else:
                self.db.set_setting(HOME_KEY, rest)
                await msg.reply_html(f"🏠 Home set: <b>{escape(parse_home(rest)[0])}</b>.")
            return
        begin_at = None
        if args and _hm(args[-1]) and ":" in args[-1]:
            t = _hm(args.pop())
            begin_at = t.hour * 60 + t.minute
        if args:
            day = parse_day(" ".join(args), self._today())
            if day is None:
                await msg.reply_text("Usage: /route [day] [HH:MM] — e.g. /route saturday, /route 21/11, "
                                     "/route tomorrow 18:30")
                return
        else:
            day = self.default_day()
        note = await msg.reply_text(f"Working out the route for {day.strftime('%a %-d %b')}…")
        try:
            plan = await asyncio.wait_for(self.build(day), timeout=90)
        except (asyncio.TimeoutError, TimeoutError):
            await note.edit_text("Working out the drive times took too long; try again in a minute.")
            return
        if not plan.fixed and not plan.markets:
            extra = (" (deleted from your calendar: " + ", ".join(plan.removed) + ")") if plan.removed else ""
            await note.edit_text(f"No markets on your calendar for {day.strftime('%a %-d %b')}{extra}.")
            return
        self.plans[day.isoformat()] = plan
        self.prefs.pop(day.isoformat(), None)
        if begin_at is not None:
            self.set_start(plan, begin_at, free=True)
            await self._after_start(plan, note)
            return
        self.set_start(plan, None, free=False)
        text, kb = self.time_text(plan)
        await note.edit_text(text, parse_mode="HTML", reply_markup=kb)

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        parts = (q.data or "").split(":")
        action, iso = parts[1], parts[2]
        if action in ("x", "n"):
            await self._on_live(q, action, iso, parts)
            return
        plan = self.plans.get(iso)
        if plan is None:
            await q.answer("That plan is old — send /route again.", show_alert=True)
            return
        if action == "t":
            val = parts[3]
            begin_at = None if val == "open" else int(val)
            self.set_start(plan, begin_at, free=begin_at is not None)
            await q.answer()
            await self._after_start(plan, q)
            return
        if action == "s":
            plan.free_start = parts[3] == "free"
            await q.answer()
            text, kb = self.ask_text(plan)
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return
        if action == "p":
            key = parts[3]
            prefs = self.prefs.setdefault(iso, [])
            prefs.remove(key) if key in prefs else prefs.append(key)
            await q.answer()
            text, kb = self.ask_text(plan)
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif action == "go":
            await q.answer("Planning…")
            await self._send_plan(await asyncio.to_thread(self.compute, plan), edit=q)
        elif action == "o":
            i = int(parts[3])
            if i >= len(plan.options):
                await q.answer("That option is gone — send /route again.", show_alert=True)
                return
            await q.answer("This is your route now")
            r = plan.options[i]
            self.activate(plan, r)
            await self.app.bot.send_message(self.settings.allowed_chat_id,
                                            self.route_text(plan, r, f"Option {chr(65 + i)}"), parse_mode="HTML",
                                            disable_web_page_preview=True)

    async def _on_live(self, q, action: str, iso: str, parts: list[str]) -> None:  # noqa: ANN001
        i = int(parts[3])
        state = self.active(iso)
        if not state or i >= len(state["visits"]):
            await q.answer("That route is no longer active.", show_alert=True)
            return
        if action == "x":
            extra = int(parts[4])
            leave = max(state["visits"][i]["leave"], self._now_min()) + extra
            await q.answer(f"OK, {extra}′ more — re-planning…")
        else:
            leave = self._now_min()
            await q.answer("Re-planning from now…")
        try:
            await q.edit_message_reply_markup(None)
        except Exception:  # noqa: BLE001 - an old message may not be editable
            pass
        async with self._busy:
            plan = await self.replan(iso, i, leave, alert_again=action == "x")
        if plan is not None:
            await self._send_plan(plan, title="Updated route")

    async def task_text(self, args: list[str]) -> str:
        """`python -m scout.cron route [day] [HH:MM] [first-event-id]`: the plan as plain text (not activated).
        With a time you go straight to the first stop (after work)."""
        args = list(args)
        first = args.pop() if len(args) >= 2 and args[-1].isdigit() and len(args[-1]) < 6 else None
        begin_at = None
        if args and ":" in args[-1] and _hm(args[-1]):
            t = _hm(args.pop())
            begin_at = t.hour * 60 + t.minute
        day = parse_day(" ".join(args), self._today()) if args else self.default_day()
        if day is None:
            return "usage: route [day] [HH:MM] [first-event-id]"
        plan = await self.build(day)
        if not plan.fixed and not plan.markets:
            return f"no markets on {day}"
        self.set_start(plan, begin_at, free=begin_at is not None)
        if first:
            self.prefs[day.isoformat()] = [f"ev{first}"]
        await asyncio.to_thread(self.compute, plan)
        self.prefs.pop(day.isoformat(), None)
        if plan.best is not None:
            html = self.route_text(plan, plan.best)
        elif plan.options:
            html = self.options_text(plan) + "\n\n" + "\n\n".join(
                self.route_text(plan, r, f"Option {chr(65 + i)}") for i, r in enumerate(plan.options))
        else:
            html = plan.problem or "nothing to plan"
        return re.sub(r"<[^>]+>", "", html).replace("&amp;", "&").replace("&#x27;", "'")


def setup_route(app: Application, settings, db: Database, geo: Geo | None = None, tasks=None) -> RoutePlanner:  # noqa: ANN001
    rp = RoutePlanner(app, settings, db, geo=geo)
    rp.register()
    app.bot_data["route"] = rp
    if tasks is not None:
        tasks.register("route", rp.task_text, "Print the best route for a day: route [day]", None, takes_args=True)
    return rp

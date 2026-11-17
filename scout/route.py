"""Day route planner (/route): the order to visit every market on your calendar for a day, by car.

What goes in, for the chosen day:
* Booked slots (Event.booked_start) are fixed: you arrive by the slot time and stay VISIT_MINUTES or
  until the slot ends, whichever is later. Their order is the order of the slots.
* Every other market you said ✅ to that runs that day, unless you deleted that day's entry in Google
  Calendar (= not going). Each needs VISIT_MINUTES inside its opening hours that day; hours that
  aren't known are assumed 10:00–20:00 and flagged.
* Lunch: an hour starting between 13:30 and 14:30, wherever you are at that point.
* Driving: OpenStreetMap routes × TRAVEL_FACTOR (1.3) + PARKING_MINUTES (5) per leg (scout/geo.py).
  With a home address (/route home <address>) the day starts and ends there.
* Your preference: markets you tap first are visited first, in the order tapped.

The order is the one with the least driving that fits every opening time and slot (then the earliest
finish). When not everything fits, you get up to three options — a shorter or later lunch, skipping
lunch, or leaving out the market(s) that cost the most — and pick one.

The search is exact: a dynamic programme over (markets visited, last stop, where you are), keeping
for each state only plans that are not both later and longer-driven than another.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
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
from .geo import Geo, Leg, Point
from .planner import _hm, departure, parse_day, visit_minutes_from_env

log = logging.getLogger(__name__)

HOME_KEY = "route_home"
DEFAULT_HOURS = (10 * 60, 20 * 60)
LUNCH = ((13 * 60 + 30, 14 * 60 + 30), 60)          # start window, minutes
LUNCH_FLEX = ((13 * 60, 15 * 60 + 30), 30)
MAX_OPTIONS = 3
MAX_DROP_COMBOS = 80


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
    event_id: int | None = None
    notes: list[str] = field(default_factory=list)


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


LegFn = Callable[[int | None, int | None], Leg]   # loc → loc (None = no known start)


def _enter(stop: Stop, arrive: int) -> tuple[int, int] | None:
    """(begin, leave) at this stop when arriving at `arrive`, or None if it doesn't fit."""
    if stop.kind == "fixed":
        return (stop.start, stop.leave) if arrive <= stop.start else None
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
          prefs: list[str] | None = None) -> Route | None:
    """Best order visiting every stop, or None when no order fits."""
    n = len(stops)
    if n == 0:
        return Route([], 0, start)
    prefs = [p for p in (prefs or []) if any(s.key == p for s in stops)]
    pref_idx = {s.key: i for i, s in enumerate(stops) if s.key in prefs}
    pref_order = [pref_idx[p] for p in prefs]
    fixed = [i for i, s in enumerate(stops) if s.kind == "fixed"]

    # label: (time, drive, parent_state, parent_label_index, stop index, arrive, begin, leg)
    State = tuple[int, int, int | None]   # mask, last, loc
    labels: dict[State, list[tuple]] = {(0, -1, home): [(start, 0, None, None, None, None, None, None)]}
    by_size: list[list[State]] = [[] for _ in range(n + 1)]
    by_size[0].append((0, -1, home))

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
                    lg = leg(loc, s.loc) if s.loc is not None else None
                    d = lg.minutes if lg else 0
                    fit = _enter(s, t + d)
                    if fit is None:
                        continue
                    begin, leave = fit
                    new_loc = s.loc if s.loc is not None else loc
                    new_mask = mask | 1 << j
                    # every booked slot still to come must stay reachable
                    if any(not new_mask >> f & 1 and leave + leg(new_loc, stops[f].loc).minutes > stops[f].start
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
            back = leg(loc, home) if home is not None else None
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
    if home is not None and visits and visits[0].drive:
        route.home_leave = visits[0].begin - visits[0].drive.minutes
    # Lunch after everything else isn't part of the route: the day is over by then.
    if visits and visits[-1].stop.kind == "lunch":
        visits.pop()
        route.finish = visits[-1].leave if visits else start
        route.lunch = False
    return route


def plan_options(fixed: list[Stop], markets: list[Stop], leg: LegFn, start: int = 0, home: int | None = None,
                 prefs: list[str] | None = None) -> tuple[Route | None, list[Route], str | None]:
    """(the route, or None; options when not everything fits; a problem that no option can solve)."""
    def lunch(spec) -> Stop:  # noqa: ANN001
        (lo, hi), stay = spec
        return Stop("lunch", "Lunch", "lunch", None, [(lo, hi)], stay)

    if fixed and solve(fixed, leg, start, home) is None:
        return None, *_without_clashing_slots(fixed, markets, leg, start, home, prefs)
    full = solve(fixed + markets + [lunch(LUNCH)], leg, start, home, prefs)
    if full is not None:
        return full, [], None
    options: list[Route] = []
    r = solve(fixed + markets + [lunch(LUNCH_FLEX)], leg, start, home, prefs)
    if r is not None:
        r.label = "Everything, with a 30′ lunch between 13:00 and 15:30"
        options.append(r)
    drops: list[Route] = []
    for k in range(1, len(markets) + 1):
        combos = list(itertools.combinations(range(len(markets)), k))[:MAX_DROP_COMBOS]
        for combo in combos:
            keep = [m for i, m in enumerate(markets) if i not in combo]
            r = solve(fixed + keep + [lunch(LUNCH)], leg, start, home, prefs)
            if r is not None:
                r.dropped = [markets[i] for i in combo]
                r.label = "Skip " + " and ".join(m.title for m in r.dropped)
                drops.append(r)
        if drops:
            break
    drops.sort(key=lambda r: (r.drive, r.end))
    options += drops[:2]
    if len(options) < MAX_OPTIONS:
        r = solve(fixed + markets, leg, start, home, prefs)
        if r is not None:
            r.label = "Everything, no lunch break"
            r.lunch = False
            options.append(r)
    return None, options[:MAX_OPTIONS], None if options else "Nothing fits that day, not even with a market left out."


def clashes(fixed: list[Stop], leg: LegFn) -> list[str]:
    """Booked slots you can't get between, in plain words."""
    out = []
    ordered = sorted(fixed, key=lambda s: s.start)
    for a, b in zip(ordered, ordered[1:]):
        need = a.leave + leg(a.loc, b.loc).minutes
        if need > b.start:
            out.append(f"{a.title} {hm(a.start)} and {b.title} {hm(b.start)}: you'd reach the second at "
                       f"{hm(need)} ({leg(a.loc, b.loc).minutes}′ drive)")
    return out


def _without_clashing_slots(fixed: list[Stop], markets: list[Stop], leg: LegFn, start: int, home: int | None,
                            prefs: list[str] | None) -> tuple[list[Route], str]:
    """Booked slots that clash: plans that give up as few of them as possible."""
    problem = "Your booked slots clash: " + "; ".join(clashes(fixed, leg) or ["not enough time between them"])
    found: list[Route] = []
    for k in range(1, len(fixed)):
        for combo in itertools.combinations(range(len(fixed)), k):
            keep = [f for i, f in enumerate(fixed) if i not in combo]
            if solve(keep, leg, start, home) is None:
                continue
            gone = [fixed[i] for i in combo]
            best, options, _ = plan_options(keep, markets, leg, start, home, prefs)
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
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes(dt: datetime, tz) -> int:  # noqa: ANN001
    local = dt.astimezone(tz)
    return local.hour * 60 + local.minute


@dataclass
class DayPlan:
    day: date
    fixed: list[Stop]
    markets: list[Stop]
    removed: list[str]
    points: list[Point | None]
    addresses: list[str]
    home: int | None
    legs: dict[tuple[int | None, int | None], Leg]
    start: int
    best: Route | None = None
    options: list[Route] = field(default_factory=list)
    problem: str | None = None


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

    def register(self) -> None:
        only_me = filters.Chat(chat_id=self.settings.allowed_chat_id)
        self.app.add_handler(CommandHandler("route", self.cmd_route, filters=only_me))
        self.app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^rt:"))

    # ------------------------------------------------------------ what's on
    def _today(self) -> date:
        return self.now().astimezone(self.tz).date()

    def _calendar(self):  # noqa: ANN202
        return self.app.bot_data.get("calendar")

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

    def _home(self) -> str | None:
        return (self.db.get_setting(HOME_KEY) or "").strip() or None

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

    async def build(self, day: date) -> DayPlan:
        cal = self._calendar()
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
                                  leave=_minutes(leave, self.tz), event_id=ev.id,
                                  notes=notes + ([f"slot {hm(start)}–{hm(end)}"] if end else [])))
            else:
                wins, more = self._hours(ev, day)
                if ev.booking_method == "eventbrite" or any(
                        x.get("booking_required") for x in ev.session_list if x.get("date") == day.isoformat()):
                    more.append("🎟 needs a ticket — none booked")
                markets.append(Stop(f"ev{ev.id}", ev.title, "market", loc, wins, self.visit, event_id=ev.id,
                                    notes=notes + more))
        home = None
        home_addr = self._home()
        if home_addr:
            hp = await self.geo.geocode(home_addr)
            if hp:
                home = len(points)
                points.append(hp)
                addresses.append(home_addr)
        await self.geo.warm([p for p in points if p])
        legs: dict[tuple[int | None, int | None], Leg] = {}
        idx: list[int | None] = list(range(len(points)))
        for a in idx:
            for b in idx:
                if a != b:
                    legs[(a, b)] = await self.geo.leg(points[a], points[b])
        start = 0
        if day == self._today():
            local = self.now().astimezone(self.tz)
            start = local.hour * 60 + local.minute
        return DayPlan(day, fixed, markets, removed, points, addresses, home, legs, start)

    def _leg_fn(self, plan: DayPlan) -> LegFn:
        zero = Leg(0, 0.0, "same")

        def leg(a: int | None, b: int | None) -> Leg:
            if a is None or b is None or a == b:
                return zero
            return plan.legs[(a, b)]
        return leg

    def compute(self, plan: DayPlan) -> DayPlan:
        prefs = self.prefs.get(plan.day.isoformat(), [])
        plan.best, plan.options, plan.problem = plan_options(plan.fixed, plan.markets, self._leg_fn(plan),
                                                             plan.start, plan.home, prefs)
        return plan

    # ------------------------------------------------------------ messages
    def _maps_link(self, plan: DayPlan, route: Route) -> str | None:
        def where(loc: int) -> str:
            p = plan.points[loc]
            return f"{p[0]:.6f},{p[1]:.6f}" if p else plan.addresses[loc]
        locs = [v.stop.loc for v in route.visits if v.stop.loc is not None]
        locs = [x for i, x in enumerate(locs) if i == 0 or locs[i - 1] != x]
        if plan.home is not None:
            locs = [plan.home] + locs + [plan.home]
        if len(locs) < 2:
            return None
        origin, dest, mid = locs[0], locs[-1], locs[1:-1][:9]
        url = (f"https://www.google.com/maps/dir/?api=1&travelmode=driving&origin={quote(where(origin))}"
               f"&destination={quote(where(dest))}")
        if mid:
            url += "&waypoints=" + quote("|".join(where(x) for x in mid))
        return url

    def route_text(self, plan: DayPlan, route: Route, title: str | None = None) -> str:
        day = plan.day.strftime("%a %-d %b")
        lines = [f"🗺 <b>{escape(title or 'Your route')} — {day}</b>"]
        if route.home_leave is not None:
            lines.append(f"🏠 Leave home at <b>{hm(route.home_leave)}</b>")
        prev_leave = None
        for v in route.visits:
            if v.drive and v.drive.minutes and prev_leave is not None:
                km = f" · {v.drive.km:g} km" if v.drive.km else ""
                guess = " (guessed)" if v.drive.guessed else ""
                lines.append(f"   🚗 {v.drive.minutes}′{km}{guess}")
            if prev_leave is not None:
                spare = v.begin - v.arrive
                if spare >= 10:
                    lines.append(f"   ⏳ {spare}′ free" + (" (wait for opening)" if v.stop.kind == "market" else ""))
            s = v.stop
            if s.kind == "lunch":
                lines.append(f"🍽 <b>{hm(v.begin)}–{hm(v.leave)}</b>  Lunch nearby")
            else:
                mark = "🎟 " if s.kind == "fixed" else "📍 "
                tag = " <i>(booked)</i>" if s.kind == "fixed" else ""
                lines.append(f"{mark}<b>{hm(v.begin)}–{hm(v.leave)}</b>  {escape(s.title)}{tag}")
                where = plan.addresses[s.loc] if s.loc is not None else ""
                if where:
                    lines.append(f"   {escape(where)}")
                if s.kind == "market":
                    lines.append("   open " + ", ".join(f"{hm(a)}–{hm(b)}" for a, b in s.windows))
                for note in s.notes:
                    lines.append(f"   ⚠️ {escape(note)}" if not note.startswith(("🎟", "slot")) else f"   {escape(note)}")
            prev_leave = v.leave
        if route.home_back:
            lines.append(f"   🚗 {route.home_back.minutes}′ home · back at <b>{hm(route.end)}</b>")
        lines.append("")
        lines.append(f"Driving {route.drive}′ in total (incl. parking)"
                     + ("" if route.lunch else " · no lunch break in this plan"))
        if route.dropped:
            lines.append("Left out: " + ", ".join(escape(s.title) for s in route.dropped))
        if plan.removed:
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
            lines.append(f"{len(plan.markets) + len(plan.fixed)} markets with the opening times, your slots and an "
                         f"hour's lunch around 14:00 can't all be done. Pick one:")
        for i, r in enumerate(plan.options):
            order = " → ".join(escape(v.stop.title if v.stop.kind != "lunch" else "🍽") for v in r.visits)
            lines.append(f"\n<b>{chr(65 + i)}. {escape(r.label)}</b>\n   {order}\n   driving {r.drive}′, "
                         f"done {hm(r.end)}")
        if plan.removed:
            lines.append("\nNot counted (deleted from your calendar): " + ", ".join(escape(t) for t in plan.removed))
        return "\n".join(lines)

    def ask_text(self, plan: DayPlan) -> tuple[str, InlineKeyboardMarkup]:
        iso = plan.day.isoformat()
        prefs = self.prefs.get(iso, [])
        lines = [f"🗺 <b>Route for {plan.day.strftime('%a %-d %b')}</b>"]
        if plan.fixed:
            lines.append("Booked (fixed): " + ", ".join(
                f"{escape(s.title)} {hm(s.start)}" for s in sorted(plan.fixed, key=lambda s: s.start)))
        lines.append(f"To fit in: {len(plan.markets)} markets.")
        if plan.removed:
            lines.append("Not counted (deleted from your calendar): " + ", ".join(escape(t) for t in plan.removed))
        lines.append("\nAny you want to see first? Tap them in order, then <b>Plan route</b>.")
        rows = []
        for s in plan.markets:
            n = prefs.index(s.key) + 1 if s.key in prefs else None
            rows.append([InlineKeyboardButton(f"{n}️⃣ {s.title}"[:60] if n else s.title[:60],
                                              callback_data=f"rt:p:{iso}:{s.key}")])
        rows.append([InlineKeyboardButton("🚗 Plan route" if prefs else "🚗 No preference — plan it",
                                          callback_data=f"rt:go:{iso}")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def _send_plan(self, plan: DayPlan, edit=None) -> None:  # noqa: ANN001
        self.plans[plan.day.isoformat()] = plan
        markup = None
        if plan.best is not None:
            text = self.route_text(plan, plan.best)
        elif plan.options:
            text = self.options_text(plan)
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                f"{chr(65 + i)}", callback_data=f"rt:o:{plan.day.isoformat()}:{i}") for i in range(len(plan.options))]])
        else:
            text = f"🗺 <b>{plan.day.strftime('%a %-d %b')}</b>\n{escape(plan.problem or 'Nothing to plan.')}"
        if edit is not None:
            await edit.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        else:
            await self.app.bot.send_message(self.settings.allowed_chat_id, text, parse_mode="HTML",
                                            reply_markup=markup, disable_web_page_preview=True)

    # ------------------------------------------------------------ handlers
    async def cmd_route(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        args = list(ctx.args or []) if ctx is not None else []
        if args and args[0].lower() in ("home", "casa"):
            rest = " ".join(args[1:]).strip()
            if not rest:
                home = self._home()
                await msg.reply_html(f"🏠 Home: <b>{escape(home)}</b> (<code>/route home off</code> to forget)"
                                     if home else "No home set. <code>/route home C/ Example 1, Madrid</code> "
                                                  "makes the day start and end there.")
            elif rest.lower() in ("off", "none", "clear"):
                self.db.set_setting(HOME_KEY, "")
                await msg.reply_text("Home forgotten: routes start at the first market.")
            elif await self.geo.geocode(rest) is None:
                await msg.reply_text("I couldn't find that address on the map. Try adding the town.")
            else:
                self.db.set_setting(HOME_KEY, rest)
                await msg.reply_html(f"🏠 Home set: <b>{escape(rest)}</b>. Routes now start and end there.")
            return
        if args:
            day = parse_day(" ".join(args), self._today())
            if day is None:
                await msg.reply_text("Usage: /route [day] — e.g. /route saturday, /route 21/11, /route tomorrow")
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
        if len(plan.markets) >= 2:
            text, kb = self.ask_text(plan)
            await note.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        await note.delete()
        await self._send_plan(await asyncio.to_thread(self.compute, plan))

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        parts = (q.data or "").split(":")
        action, iso = parts[1], parts[2]
        plan = self.plans.get(iso)
        if plan is None:
            await q.answer("That plan is old — send /route again.", show_alert=True)
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
            await q.answer()
            r = plan.options[i]
            await self.app.bot.send_message(self.settings.allowed_chat_id,
                                            self.route_text(plan, r, f"Option {chr(65 + i)}"), parse_mode="HTML",
                                            disable_web_page_preview=True)


    async def task_text(self, args: list[str]) -> str:
        """`python -m scout.cron route [day]`: the plan as plain text (no preference)."""
        day = parse_day(" ".join(args), self._today()) if args else self.default_day()
        if day is None:
            return "usage: route [day]"
        plan = await self.build(day)
        if not plan.fixed and not plan.markets:
            return f"no markets on {day}"
        await asyncio.to_thread(self.compute, plan)
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


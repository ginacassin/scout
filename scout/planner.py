"""Slot planner: book timed Eventbrite slots without clashing with the markets you already booked.

For an event about to be auto-booked on day D at venue V, every confirmed booking B (venue W) on D
forbids a range of slot *start* times:

    lo = B.start - VISIT - drive(V->W)          (start later than lo and you can't make it to B)
    hi = depart(B) + drive(W->V)                (start earlier than hi and you can't get here from B)
    depart(B) = max(B.start + VISIT, B.end)     (booked start = arrival; stay VISIT or until the slot ends)

A slot [s, e) is allowed iff for every window: s >= hi  or  s + max(0, (e - s) - VISIT) <= lo
(i.e. you leave this market at max(s + VISIT, e) and still reach B by its start).
The booker applies exactly this rule (booker/scoutbooker/slots.py), using the plain constraint object

    {"date": "2026-11-13", "earliest": "10:00", "latest": "13:00", "avoid": [["10:05", "12:10"]],
     "prefer": "earliest", "visit_minutes": 30, "tz": "Europe/Madrid"}

`earliest`/`latest` come from the market's opening sessions that need a booking (Event.session_list).
Whenever a booking is confirmed (hook booking_confirmed) the other armed jobs on that day are re-posted
to the booker with fresh constraints. `/plan [date]` shows the day's route.

Env: VISIT_MINUTES (30); travel buffers live in scout/geo.py (TRAVEL_FACTOR, PARKING_MINUTES).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html import escape

from sqlalchemy import select
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from . import hooks
from .db import BookingWatch, Database, Event, utcnow
from .geo import Geo, Leg, Point

log = logging.getLogger(__name__)

ACTIVE_JOB_STATES = (None, "", "scheduled")


def visit_minutes_from_env() -> int:
    try:
        return max(0, int(os.environ.get("VISIT_MINUTES", "30")))
    except ValueError:
        return 30


def _hm(value: str | None) -> time | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.h](\d{2})\s*", value or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return time(int(m.group(1)), int(m.group(2)))


def fmt_hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def floor_min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def ceil_min(dt: datetime) -> datetime:
    f = floor_min(dt)
    return f if f == dt else f + timedelta(minutes=1)


def departure(start: datetime, end: datetime | None, visit: timedelta) -> datetime:
    return max(start + visit, end or start)


@dataclass
class Window:
    lo: datetime
    hi: datetime
    title: str
    start: datetime
    stay: int          # minutes at that market
    drive_in: Leg      # from that market to the new one
    drive_out: Leg     # from the new one to that market

    def explain(self, tz) -> str:  # noqa: ANN001
        guess = " (drive time guessed)" if self.drive_in.guessed or self.drive_out.guessed else ""
        other = f", {self.drive_out.minutes}′ the other way" if self.drive_out.minutes != self.drive_in.minutes else ""
        km = f" ≈{self.drive_in.km:g} km" if self.drive_in.km else ""
        return (f"Avoiding {fmt_hm(self.lo.astimezone(tz))}–{fmt_hm(self.hi.astimezone(tz))}: you're at "
                f"{self.title} {fmt_hm(self.start.astimezone(tz))} + {self.stay}′ + {self.drive_in.minutes}′ drive"
                f"{km}{other}{guess}")


def forbidden_window(b_start: datetime, b_end: datetime | None, title: str, drive_in: Leg, drive_out: Leg,
                     visit: timedelta) -> Window:
    dep = departure(b_start, b_end, visit)
    lo = b_start - visit - timedelta(minutes=drive_out.minutes)
    hi = dep + timedelta(minutes=drive_in.minutes)
    return Window(lo, hi, title, b_start, round((dep - b_start).total_seconds() / 60), drive_in, drive_out)


def slot_allowed(s: datetime, e: datetime | None, windows: list[tuple[datetime, datetime]],
                 visit: timedelta) -> bool:
    """The shared rule (see module doc). windows are (lo, hi) of slot starts."""
    overrun = max(timedelta(0), (e - s) - visit) if e else timedelta(0)
    return all(s >= hi or s + overrun <= lo for lo, hi in windows)


def merge(windows: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[list[datetime]] = []
    for lo, hi in sorted(windows):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def booking_sessions(ev: Event, day: date) -> list[dict]:
    """Sessions on `day` that need a booking (all timed sessions that day if none is flagged)."""
    same_day = [s for s in ev.session_list if s.get("date") == day.isoformat()]
    flagged = [s for s in same_day if s.get("booking_required")]
    return flagged or same_day


def plan_day(ev: Event, today: date) -> date | None:
    """The day the auto-booker is aiming at: the first upcoming day with a booking-required session,
    else the first upcoming day of the market."""
    days = sorted({date.fromisoformat(s["date"]) for s in ev.session_list
                   if s.get("booking_required") and s.get("date")})
    days = [d for d in days if d >= today] or [d for d in ev.date_list if d >= today]
    return days[0] if days else None


@dataclass
class Plan:
    event_id: int
    day: date | None
    constraints: dict | None
    reasons: list[str] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.reasons)


class Planner:
    def __init__(self, settings, db: Database, geo: Geo | None = None, visit_minutes: int | None = None,  # noqa: ANN001
                 autobook=None, now=None):  # noqa: ANN001
        self.settings = settings
        self.tz = settings.tz
        self.db = db
        self.geo = geo or Geo(db)
        self.visit = timedelta(minutes=visit_minutes if visit_minutes is not None else visit_minutes_from_env())
        self.autobook = autobook
        self.now = now or utcnow

    # ------------------------------------------------------------ data access
    def _today(self) -> date:
        return self.now().astimezone(self.tz).date()

    def _local_day(self, dt: datetime) -> date:
        return dt.astimezone(self.tz).date()

    def booked_on(self, day: date, exclude: int | None = None) -> list[Event]:
        with self.db.session() as s:
            evs = list(s.scalars(select(Event).where(Event.booked_start.is_not(None))))
        return sorted((e for e in evs if e.id != exclude and self._local_day(e.booked_start) == day),
                      key=lambda e: e.booked_start)

    def armed_on(self, day: date) -> list[tuple[Event, BookingWatch]]:
        with self.db.session() as s:
            rows = list(s.execute(select(Event, BookingWatch).join(BookingWatch, BookingWatch.event_id == Event.id)
                                  .where(BookingWatch.autobook.is_(True))))
        today = self._today()
        return [(e, w) for e, w in rows
                if e.booked_start is None and w.autobook_state in ACTIVE_JOB_STATES and plan_day(e, today) == day]

    async def _point(self, ev: Event) -> Point | None:
        return await self.geo.locate(ev.address, ev.area)

    # ------------------------------------------------------------ constraints
    async def constraints_for(self, event_id: int, day: date | None = None) -> Plan:
        with self.db.session() as s:
            ev = s.get(Event, event_id)
        if ev is None:
            return Plan(event_id, None, None, ["Unknown event."])
        day = day or plan_day(ev, self._today())
        if day is None:
            return Plan(event_id, None, None, ["No date known for this market: any slot is fine."])
        tz = self.tz
        cons: dict = {"date": day.isoformat(), "earliest": None, "latest": None, "avoid": [],
                      "prefer": "earliest", "visit_minutes": int(self.visit.total_seconds() // 60),
                      "tz": getattr(tz, "key", str(tz))}
        reasons: list[str] = []
        sess = booking_sessions(ev, day)
        starts = [t for t in (_hm(s.get("start")) for s in sess) if t]
        ends = [t for t in (_hm(s.get("end")) for s in sess) if t]
        if starts:
            cons["earliest"] = min(starts).strftime("%H:%M")
        if ends:
            cons["latest"] = max(ends).strftime("%H:%M")
        if starts or ends:
            reasons.append(f"Opening hours that need a booking: {cons['earliest'] or '?'}–{cons['latest'] or '?'}")

        windows: list[Window] = []
        others = self.booked_on(day, exclude=event_id)
        if others:
            here = await self._point(ev)
            for b in others:
                there = await self._point(b)
                drive_in = await self.geo.leg(there, here)
                drive_out = await self.geo.leg(here, there)
                windows.append(forbidden_window(b.booked_start, b.booked_end, b.title, drive_in, drive_out,
                                                self.visit))
        day_start = datetime.combine(day, time(0, 0), tz)
        day_end = datetime.combine(day, time(23, 59), tz)
        for lo, hi in merge([(w.lo, w.hi) for w in windows]):
            lo, hi = max(floor_min(lo), day_start), min(ceil_min(hi), day_end)
            if lo < hi:
                cons["avoid"].append([fmt_hm(lo.astimezone(tz)), fmt_hm(hi.astimezone(tz))])
        reasons += [w.explain(tz) for w in windows]
        if not windows:
            reasons.append("No other bookings that day: taking the earliest free slot.")
        return Plan(event_id, day, cons, reasons, windows)

    async def constraints(self, event_id: int) -> dict | None:
        return (await self.constraints_for(event_id)).constraints

    # ---------------------------------------------------------------- re-plan
    async def on_booking_confirmed(self, event_id: int, **_kw) -> None:  # noqa: ANN003
        """A slot was confirmed: push fresh constraints for every other armed job on that day."""
        with self.db.session() as s:
            ev = s.get(Event, event_id)
        if ev is None or ev.booked_start is None or self.autobook is None:
            return
        day = self._local_day(ev.booked_start)
        for other, _w in self.armed_on(day):
            if other.id == event_id:
                continue
            try:
                toast, _ = await self.autobook.arm(other.id)
                log.info("Re-planned event %s after booking %s: %s", other.id, event_id, toast)
            except Exception:  # noqa: BLE001
                log.exception("Re-plan of event %s failed", other.id)

    # ------------------------------------------------------------------ route
    async def route_text(self, day: date) -> str:
        tz = self.tz
        lines = [f"🗺 <b>Your market route — {day.strftime('%a %-d %b')}</b>"]
        booked = self.booked_on(day)
        if not booked:
            lines.append("No confirmed bookings that day yet.")
        prev: Event | None = None
        prev_pt: Point | None = None
        for b in booked:
            pt = await self._point(b)
            if prev is not None:
                leg = await self.geo.leg(prev_pt, pt)
                dep = departure(prev.booked_start, prev.booked_end, self.visit)
                arrive = dep + timedelta(minutes=leg.minutes)
                spare = math.floor((b.booked_start - arrive).total_seconds() / 60)
                km = f", {leg.km:g} km" if leg.km else ""
                guess = " (guessed)" if leg.guessed else ""
                if spare >= 0:
                    lines.append(f"   🚗 {leg.minutes}′ drive{km}{guess} · leave {fmt_hm(dep.astimezone(tz))}"
                                 f" · {spare}′ to spare")
                else:
                    lines.append(f"   🚗 {leg.minutes}′ drive{km}{guess} · ⚠️ <b>conflict</b>: you'd arrive "
                                 f"{fmt_hm(arrive.astimezone(tz))}, {-spare}′ late")
            end = b.booked_end.astimezone(tz).strftime("–%H:%M") if b.booked_end else ""
            where = escape(b.address or b.area or "address unknown")
            lines.append(f"<b>{fmt_hm(b.booked_start.astimezone(tz))}{end}</b>  {escape(b.title)} ✅"
                         f"\n   📍 {where}")
            prev, prev_pt = b, pt
        armed = self.armed_on(day)
        if armed:
            lines += ["", "🤖 <b>Armed, not booked yet</b>"]
            for ev, w in sorted(armed, key=lambda r: r[1].opens_at or utcnow()):
                plan = await self.constraints_for(ev.id, day)
                c = plan.constraints or {}
                opens = w.opens_at.astimezone(tz).strftime("%a %-d %b %H:%M") if w.opens_at else "?"
                rng = f"{c.get('earliest') or 'any'}–{c.get('latest') or 'any'}"
                avoid = ", ".join(f"{a}–{b}" for a, b in c.get("avoid", []))
                lines.append(f"• {escape(ev.title)} — opens {opens}\n   earliest slot {rng}"
                             + (f", avoiding {avoid}" if avoid else ""))
                lines += [f"   <i>{escape(r)}</i>" for r in plan.reasons if r.startswith("Avoiding")]
        return "\n".join(lines)

    def default_day(self) -> date:
        today = self._today()
        days = [self._local_day(e.booked_start) for e in self.booked_on_any()]
        with self.db.session() as s:
            rows = list(s.scalars(select(Event).join(BookingWatch, BookingWatch.event_id == Event.id)
                                  .where(BookingWatch.autobook.is_(True))))
        days += [d for d in (plan_day(e, today) for e in rows) if d]
        upcoming = sorted(d for d in days if d >= today)
        return upcoming[0] if upcoming else today

    def booked_on_any(self) -> list[Event]:
        with self.db.session() as s:
            return list(s.scalars(select(Event).where(Event.booked_start.is_not(None))))

    async def cmd_plan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        arg = " ".join(ctx.args or []) if ctx is not None else ""
        if arg.strip():
            day = parse_day(arg, self._today())
            if day is None:
                await msg.reply_text("Usage: /plan [date] — e.g. /plan 13/11, /plan 2026-11-13, /plan viernes, "
                                     "/plan tomorrow")
                return
        else:
            day = self.default_day()
        try:
            text = await asyncio.wait_for(self.route_text(day), timeout=45)
        except (asyncio.TimeoutError, TimeoutError):
            text = "Working out the drive times took too long; try again in a minute."
        await msg.reply_html(text, disable_web_page_preview=True)


WEEKDAYS = {"mon": 0, "monday": 0, "lun": 0, "lunes": 0, "tue": 1, "tuesday": 1, "mar": 1, "martes": 1,
            "wed": 2, "wednesday": 2, "mie": 2, "mié": 2, "miercoles": 2, "miércoles": 2,
            "thu": 3, "thursday": 3, "jue": 3, "jueves": 3, "fri": 4, "friday": 4, "vie": 4, "viernes": 4,
            "sat": 5, "saturday": 5, "sab": 5, "sáb": 5, "sabado": 5, "sábado": 5,
            "sun": 6, "sunday": 6, "dom": 6, "domingo": 6}


def parse_day(text: str, today: date) -> date | None:
    t = text.strip().lower()
    if t in ("today", "hoy"):
        return today
    if t in ("tomorrow", "mañana", "manana"):
        return today + timedelta(days=1)
    if t in WEEKDAYS:
        return today + timedelta(days=(WEEKDAYS[t] - today.weekday()) % 7)
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})(?:[/.-](\d{1,2})(?:[/.-](\d{2,4}))?)?", t)
    if not m:
        return None
    d, mo, y = int(m.group(1)), m.group(2), m.group(3)
    try:
        if y:
            return date(int(y) + (2000 if len(y) == 2 else 0), int(mo), d)
        if mo:
            cand = date(today.year, int(mo), d)
            return cand if cand >= today - timedelta(days=30) else date(today.year + 1, int(mo), d)
        cand_month, cand_year = today.month, today.year
        for _ in range(2):  # day of this month, or next month if it already passed
            try:
                cand = date(cand_year, cand_month, d)
                if cand >= today:
                    return cand
            except ValueError:
                pass
            cand_month, cand_year = (1, cand_year + 1) if cand_month == 12 else (cand_month + 1, cand_year)
    except ValueError:
        return None
    return None


def setup_planner(app: Application, settings, db: Database, autobook=None, geo: Geo | None = None) -> Planner:  # noqa: ANN001
    planner = Planner(settings, db, geo=geo, autobook=autobook)
    if autobook is not None:
        autobook.planner = planner
    hooks.on("booking_confirmed", planner.on_booking_confirmed)
    app.add_handler(CommandHandler("plan", planner.cmd_plan, filters=filters.Chat(chat_id=settings.allowed_chat_id)))
    return planner

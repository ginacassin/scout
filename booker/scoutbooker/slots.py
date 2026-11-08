"""Timed-entry slots: parse labels like "Viernes 13 · 10:00–10:30" and pick the one that fits the route.

Pure functions only (unit-tested). The constraint object comes from Scout's planner:

    {"date": "2026-11-13", "earliest": "10:00", "latest": "13:00", "avoid": [["10:05", "12:10"]],
     "prefer": "earliest", "visit_minutes": 30, "tz": "Europe/Madrid"}

`avoid` windows are ranges of slot *start* times that clash with another booking. A slot [s, e) fits iff
    s >= hi  or  s + max(0, (e - s) - visit) <= lo        for every [lo, hi] in avoid
(the same rule as scout/planner.py: you leave at max(s + visit, e) and still reach the next market).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MONTHS = {
    "enero": 1, "ene": 1, "january": 1, "jan": 1,
    "febrero": 2, "feb": 2, "february": 2,
    "marzo": 3, "mar": 3, "march": 3,
    "abril": 4, "abr": 4, "april": 4, "apr": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "jun": 6, "june": 6,
    "julio": 7, "jul": 7, "july": 7,
    "agosto": 8, "ago": 8, "august": 8, "aug": 8,
    "septiembre": 9, "setiembre": 9, "sept": 9, "sep": 9, "set": 9, "september": 9,
    "octubre": 10, "oct": 10, "october": 10,
    "noviembre": 11, "nov": 11, "november": 11,
    "diciembre": 12, "dic": 12, "december": 12, "dec": 12,
}
WEEKDAYS = (r"lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|lun|mar|mi[eé]|jue|vie|s[aá]b|dom|"
            r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun")
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

_T = r"(\d{1,2})(?:[:.h](\d{2})(?![./]?\d))?\s*(a\.?\s?m\.?|p\.?\s?m\.?|h(?:rs?)?\b)?"
RANGE_RE = re.compile(rf"(?<![\d/.:-]){_T}\s*(?:-|–|—|a|to|hasta|al)\s*{_T}", re.I)
SINGLE_RE = re.compile(r"(?<![\d/.:-])(\d{1,2})(?:[:.](\d{2})(?![./]?\d)\s*(a\.?\s?m\.?|p\.?\s?m\.?|h\b)?|\s*(a\.?\s?m\.?|p\.?\s?m\.?|h\b))",
                       re.I)
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NUM_DATE_RE = re.compile(r"(?<![\d:.])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|º)?\s*(?:de\s+)?({_MONTH_ALT})\.?\b", re.I)
MONTH_DAY_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
WEEKDAY_DAY_RE = re.compile(rf"\b({WEEKDAYS})\.?,?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
WEEKDAY_INDEX = {"lun": 0, "mon": 0, "mar": 1, "tue": 1, "mie": 2, "mié": 2, "wed": 2, "jue": 3, "thu": 3,
                 "vie": 4, "fri": 4, "sab": 5, "sáb": 5, "sat": 5, "dom": 6, "sun": 6}
UNAVAILABLE_RE = re.compile(r"\b(?:agotad[oa]s?|sold out|completo|no disponible|unavailable|full|lleno|"
                            r"sin plazas|no quedan)\b", re.I)


@dataclass
class Slot:
    label: str
    start: datetime | None
    end: datetime | None
    available: bool = True
    index: int = 0          # position of the control/button on the page
    kind: str = ""          # select | stepper | pick

    def short(self, tz: ZoneInfo | None = None) -> str:
        if self.start is None:
            return self.label.strip()[:40]
        s = self.start.astimezone(tz) if tz else self.start
        out = s.strftime("%a %-d %H:%M")
        if self.end:
            out += "–" + (self.end.astimezone(tz) if tz else self.end).strftime("%H:%M")
        return out


def _hour(h: str, m: str | None, ampm: str | None) -> time | None:
    hh, mm = int(h), int(m or 0)
    ap = (ampm or "").lower().replace(".", "").replace(" ", "")
    if ap.startswith("p") and hh < 12:
        hh += 12
    elif ap.startswith("a") and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    return time(hh, mm)


def _has_marker(m: str | None, ap: str | None) -> bool:
    return bool(m) or bool(ap)


def parse_times(label: str) -> tuple[time | None, time | None]:
    """(start, end) from a label. A bare number ('13') is never a time: it needs ':mm', am/pm or 'h'."""
    text = label or ""
    for m in RANGE_RE.finditer(text):
        h1, m1, a1, h2, m2, a2 = m.groups()
        if not (_has_marker(m1, a1) or _has_marker(m2, a2)):
            continue
        ap1 = a1 if a1 and not a1.lower().startswith("h") else None
        ap2 = a2 if a2 and not a2.lower().startswith("h") else None
        t2 = _hour(h2, m2, ap2)
        t1 = _hour(h1, m1, ap1 or ap2)
        if ap2 and not ap1 and t1 and t2 and t1 > t2:
            t1 = _hour(h1, m1, None)  # "11:30 - 12:30pm"
        if t1 and t2:
            return t1, t2
    for m in SINGLE_RE.finditer(text):
        h, mm, ap, ap_only = m.groups()
        ampm = next((a for a in (ap_only, ap) if a and not a.lower().startswith("h")), None)
        t = _hour(h, mm, ampm)
        if t:
            return t, None
    return None, None


def _closest_year(month: int, day: int, ref: date) -> date | None:
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - ref).days) < abs((best - ref).days):
            best = d
    return best


def parse_date(label: str, ref: date) -> date | None:
    """A date mentioned in the label, resolved near `ref` (the day we're aiming at, or today)."""
    text = label or ""
    if m := ISO_DATE_RE.search(text):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    if m := DAY_MONTH_RE.search(text):
        return _closest_year(MONTHS[m.group(2).lower()], int(m.group(1)), ref)
    if m := MONTH_DAY_RE.search(text):
        return _closest_year(MONTHS[m.group(1).lower()], int(m.group(2)), ref)
    if m := NUM_DATE_RE.search(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if y:
            try:
                return date(int(y) + (2000 if len(y) == 2 else 0), mo, d)
            except ValueError:
                return None
        return _closest_year(mo, d, ref) if 1 <= mo <= 12 else None
    if m := WEEKDAY_DAY_RE.search(text):
        day = int(m.group(2))
        wd = WEEKDAY_INDEX.get(m.group(1).lower()[:3])
        best = None
        for delta in (-1, 0, 1):  # same day number in the month of ref, or a neighbouring month
            y, mo = ref.year, ref.month + delta
            if mo == 0:
                y, mo = y - 1, 12
            elif mo == 13:
                y, mo = y + 1, 1
            try:
                d = date(y, mo, day)
            except ValueError:
                continue
            score = (wd is not None and d.weekday() != wd, abs((d - ref).days))
            if best is None or score < best[0]:
                best = (score, d)
        return best[1] if best else None
    return None


def parse_slot(label: str, ref: date, tz: ZoneInfo, index: int = 0, kind: str = "",
               available: bool | None = None) -> Slot | None:
    """None when the label has no time in it (not a timed slot)."""
    t1, t2 = parse_times(label)
    if t1 is None:
        return None
    day = parse_date(label, ref) or ref
    start = datetime.combine(day, t1, tz)
    end = datetime.combine(day, t2, tz) if t2 else None
    if end is not None and end <= start:
        end = None
    avail = (not UNAVAILABLE_RE.search(label or "")) if available is None else available
    return Slot(label=" ".join((label or "").split())[:120], start=start, end=end, available=avail, index=index,
                kind=kind)


def _at(day: date, hm: str | None, tz: ZoneInfo) -> datetime | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", hm or "")
    if not m:
        return None
    return datetime.combine(day, time(int(m.group(1)), int(m.group(2))), tz)


def constraint_day(cons: dict | None) -> date | None:
    try:
        return date.fromisoformat(cons["date"]) if cons and cons.get("date") else None
    except (TypeError, ValueError):
        return None


def check(slot: Slot, cons: dict | None, tz: ZoneInfo) -> tuple[bool, str]:
    """(fits, why-not)."""
    if not slot.available:
        return False, "sold out"
    if slot.start is None:
        return False, "no time"
    if not cons:
        return True, ""
    day = constraint_day(cons)
    start = slot.start.astimezone(tz)
    end = slot.end.astimezone(tz) if slot.end else None
    if day and start.date() != day:
        return False, "other day"
    ref = day or start.date()
    earliest, latest = _at(ref, cons.get("earliest"), tz), _at(ref, cons.get("latest"), tz)
    if earliest and start < earliest:
        return False, f"before {cons['earliest']}"
    if latest and ((end or start) > latest or start >= latest):
        return False, f"after {cons['latest']}"
    visit = timedelta(minutes=int(cons.get("visit_minutes") or 30))
    overrun = max(timedelta(0), (end - start) - visit) if end else timedelta(0)
    for pair in cons.get("avoid") or []:
        try:
            lo, hi = _at(ref, pair[0], tz), _at(ref, pair[1], tz)
        except (IndexError, TypeError):
            continue
        if lo is None or hi is None:
            continue
        if not (start >= hi or start + overrun <= lo):
            return False, f"clashes with {pair[0]}–{pair[1]}"
    return True, ""


def choose(slots: list[Slot], cons: dict | None, tz: ZoneInfo) -> tuple[Slot | None, list[tuple[Slot, str]]]:
    """Earliest (or latest, if cons.prefer == 'latest') slot that fits. Returns (chosen, [(slot, why-not)])."""
    timed = [s for s in slots if s.start is not None]
    reverse = bool(cons and cons.get("prefer") == "latest")
    timed.sort(key=lambda s: s.start, reverse=reverse)
    rejected = []
    for s in timed:
        ok, why = check(s, cons, tz)
        if ok:
            return s, rejected
        rejected.append((s, why))
    return None, rejected


def describe(rejected: list[tuple[Slot, str]], tz: ZoneInfo, limit: int = 450) -> str:
    if not rejected:
        return "no timed slots were shown"
    parts = [f"{s.short(tz)} ({why})" for s, why in rejected]
    out = "Slots seen: " + ", ".join(parts)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def date_patterns(day: date) -> list[str]:
    """JS-compatible regex sources that match a date-picker label for `day` (Spanish + English)."""
    es = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"][day.month - 1]
    en = day.strftime("%B").lower()
    d = day.day
    return [
        day.isoformat(),
        rf"\b{d}\s*(?:de\s+)?(?:{es[:3]}|{en[:3]})",
        rf"(?:{en[:3]}|{es[:3]})[a-z]*\.?\s+{d}\b",
        rf"\b0?{d}/0?{day.month}\b",
    ]

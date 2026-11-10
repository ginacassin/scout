"""Per-slot ticket availability for timed-entry Eventbrite events, normalised to SlotInfo.

Eventbrite shows timed slots in one of three ways, and we read whichever the event uses:
- a series: every slot is a child event (API /series/{id}/events/, or one JSON-LD Event per slot);
- one event with a ticket class per slot, named like "Viernes 13 · 10:00–10:30" (API ticket_classes,
  or one JSON-LD Offer per class);
- one event, one ticket: a single slot spanning the event.

Times inside labels are parsed with the booker's parser (booker/scoutbooker/slots.py), so the bot and
the booker read slot names the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from booker.scoutbooker.slots import parse_date, parse_times

MADRID = ZoneInfo("Europe/Madrid")


@dataclass
class SlotInfo:
    start: datetime | None  # aware UTC; None when Eventbrite doesn't say (event-level only)
    end: datetime | None
    label: str
    available: bool
    free: bool | None = None
    exact: bool = True  # False: whole-event availability, the slot times aren't visible

    @property
    def key(self) -> str:
        return self.start.isoformat() if self.start else f"label:{self.label.strip().lower()[:60]}"

    def short(self, tz: ZoneInfo = MADRID) -> str:
        if self.start is None:
            return self.label.strip()[:40] or "a slot"
        s = self.start.astimezone(tz)
        out = s.strftime("%a %-d %b %H:%M")
        if self.end and self.end.astimezone(tz).date() == s.date():
            out += "–" + self.end.astimezone(tz).strftime("%H:%M")
        return out


def _parse_dt(value: Any) -> datetime | None:  # noqa: ANN401
    from .client import parse_dt  # local import: client imports this module

    return parse_dt(value)


def _name(obj: dict) -> str:
    n = obj.get("name")
    if isinstance(n, dict):
        n = n.get("text")
    return str(n or "")


def _times_on(label: str, day: date | None, tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    """Start/end from a label like 'Sábado 14 · 10:00–10:30', on its own date or `day`."""
    t1, t2 = parse_times(label)
    if t1 is None:
        return None, None
    d = parse_date(label, day or datetime.now(tz).date()) or day
    if d is None:
        return None, None
    start = datetime.combine(d, t1, tzinfo=tz)
    end = datetime.combine(d, t2, tzinfo=tz) if t2 else None
    if end and end <= start:
        end += timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc) if end else None


def _ld_available(offers: list[dict]) -> bool | None:
    avail = [str(o.get("availability") or "").rsplit("/", 1)[-1].lower() for o in offers]
    avail = [a for a in avail if a]
    if not avail:
        return None
    return any(a in ("instock", "limitedavailability", "onlineonly") for a in avail)


# ------------------------------------------------------------------------- API
def slots_from_api(event: dict, classes: list[dict] | None, children: list[dict] | None = None,
                   tz: ZoneInfo = MADRID) -> list[SlotInfo]:
    if children:
        out = []
        for c in children:
            ta = c.get("ticket_availability") or {}
            ok = bool(ta.get("has_available_tickets")) and not ta.get("is_sold_out")
            ss = str((c.get("event_sales_status") or {}).get("sales_status") or "").lower()
            if ss in ("sold_out", "sales_ended", "not_yet_on_sale", "event_ended"):
                ok = False
            out.append(SlotInfo(_parse_dt(c.get("start")), _parse_dt(c.get("end")), _name(c), ok, c.get("is_free")))
        return sorted(out, key=lambda s: (s.start is None, s.start or datetime.max.replace(tzinfo=timezone.utc)))

    start = _parse_dt(event.get("start"))
    day = start.astimezone(tz).date() if start else None
    classes = [c for c in classes or [] if not c.get("hidden")]
    timed = []
    for c in classes:
        s, e = _times_on(_name(c), day, tz)
        ok = str(c.get("on_sale_status") or "").upper() == "AVAILABLE"
        timed.append(SlotInfo(s, e, _name(c), ok, c.get("free")))
    if timed and all(t.start for t in timed):
        return sorted(timed, key=lambda s: s.start)
    ta = event.get("ticket_availability") or {}
    ok = any(t.available for t in timed) if timed else bool(ta.get("has_available_tickets")) and not ta.get("is_sold_out")
    free = event.get("is_free")
    if free is None and timed:
        free = all(bool(t.free) for t in timed)
    return [SlotInfo(start, _parse_dt(event.get("end")), _name(event), ok, free, exact=False)]


# ------------------------------------------------------------- JSON-LD fallback
def _price_zero(o: dict) -> bool | None:
    for key in ("price", "lowPrice", "highPrice"):
        if key in o and o[key] not in (None, ""):
            try:
                return float(str(o[key]).replace(",", ".")) == 0
            except ValueError:
                return None
    return None


def _free(offers: list[dict], ev: dict) -> bool | None:
    if ev.get("isAccessibleForFree") is True:
        return True
    flags = [f for f in (_price_zero(o) for o in offers) if f is not None]
    return all(flags) if flags else None


def slots_from_ld(events: list[dict], tz: ZoneInfo = MADRID) -> list[SlotInfo]:
    from .client import _offers

    dated = [(e, _parse_dt(e.get("startDate")), _parse_dt(e.get("endDate"))) for e in events]
    dated = [d for d in dated if d[1]]
    if len(dated) > 1:
        # A parent "series" event spans all its children; keep the children (the slots).
        children = [d for d in dated if any(o is not d and o[1] <= d[1] and (o[2] or o[1]) >= (d[2] or d[1])
                                            and ((o[2] or o[1]) - o[1]) > ((d[2] or d[1]) - d[1]) for o in dated)]
        rows = children or dated
        seen: dict[str, SlotInfo] = {}
        for e, s, en in rows:
            offers = _offers(e)
            slot = SlotInfo(s, en, _name(e), bool(_ld_available(offers)), _free(offers, e))
            prev = seen.get(slot.key)
            if prev is None or (slot.available and not prev.available):
                seen[slot.key] = slot
        return sorted(seen.values(), key=lambda x: x.start)
    if not dated:
        return []
    e, start, end = dated[0]
    offers = _offers(e)
    day = start.astimezone(tz).date()
    timed = []
    for o in offers:
        label = str(o.get("name") or "")
        if not label:
            continue
        s, en = _times_on(label, day, tz)
        if s is None:
            continue
        ok = _ld_available([o])
        timed.append(SlotInfo(s, en, label, bool(ok), _price_zero(o)))
    if timed:
        return sorted(timed, key=lambda x: x.start)
    return [SlotInfo(start, end, _name(e), bool(_ld_available(offers)), _free(offers, e), exact=False)]

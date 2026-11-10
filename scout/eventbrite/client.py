"""Eventbrite API v3 client with a public-page JSON-LD fallback, normalised to TicketStatus."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

API = "https://www.eventbriteapi.com/v3"
USER_AGENT = "Mozilla/5.0 (compatible; ScoutBot/1.0)"
TIMEOUT = 15.0
MADRID = ZoneInfo("Europe/Madrid")
API_HOURLY_BUDGET = 900  # Eventbrite allows ~1000/hour; keep headroom
PAGE_CACHE_SECONDS = 60

LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)

STATES = ("not_open", "open", "sold_out", "ended", "unknown")


@dataclass
class TicketStatus:
    is_free: bool | None
    sales_start: datetime | None  # aware UTC
    state: str  # not_open | open | sold_out | ended | unknown
    url: str | None
    start: datetime | None = None  # event start, aware
    end: datetime | None = None
    name: str | None = None
    source: str = "api"  # api | page | none


@dataclass
class EBEvent:
    id: str
    url: str | None
    start: datetime | None
    end: datetime | None
    name: str | None = None


def default_event_url(eid: str) -> str:
    return f"https://www.eventbrite.es/e/{eid}"


def parse_dt(value: Any, default_tz=MADRID) -> datetime | None:  # noqa: ANN001
    """Accepts '2026-11-10T09:00:00Z', '...+01:00', naive local strings, or API {utc, local} dicts."""
    if not value:
        return None
    if isinstance(value, dict):
        if value.get("utc"):
            return parse_dt(value["utc"], timezone.utc)
        tz = default_tz
        if value.get("timezone"):
            try:
                tz = ZoneInfo(value["timezone"])
            except Exception:  # noqa: BLE001
                pass
        return parse_dt(value.get("local"), tz)
    if not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def _types(obj: dict) -> list[str]:
    t = obj.get("@type")
    return [t] if isinstance(t, str) else list(t or [])


def ld_objects(html: str) -> list[dict]:
    """Every JSON-LD object on a page, flattening lists, @graph and ItemList entries."""
    out: list[dict] = []

    def walk(node: Any) -> None:  # noqa: ANN401
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            out.append(node)
            for key in ("@graph", "itemListElement"):
                if key in node:
                    walk(node[key])
            if "item" in node and isinstance(node["item"], dict):
                walk(node["item"])

    for raw in LD_RE.findall(html or ""):
        try:
            walk(json.loads(raw.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def ld_events(html: str) -> list[dict]:
    return [o for o in ld_objects(html) if any(t.endswith("Event") for t in _types(o))]


def _offers(ev: dict) -> list[dict]:
    offers = ev.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    flat: list[dict] = []
    for o in offers:
        if not isinstance(o, dict):
            continue
        inner = o.get("offers")
        if inner:
            flat += [i for i in (inner if isinstance(inner, list) else [inner]) if isinstance(i, dict)]
        flat.append(o)
    return flat


def _price_zero(o: dict) -> bool | None:
    for key in ("price", "lowPrice", "highPrice"):
        if key in o and o[key] not in (None, ""):
            try:
                return float(str(o[key]).replace(",", ".")) == 0
            except ValueError:
                return None
    return None


def status_from_ld(ev: dict, url: str | None, now: datetime | None = None) -> TicketStatus:
    now = now or datetime.now(timezone.utc)
    offers = _offers(ev)
    avail = [str(o.get("availability") or "").rsplit("/", 1)[-1].lower() for o in offers]
    valid_from = [d for d in (parse_dt(o.get("validFrom")) for o in offers) if d]
    free_flags = [f for f in (_price_zero(o) for o in offers) if f is not None]
    is_free = (all(free_flags) if free_flags else None)
    if ev.get("isAccessibleForFree") is True:
        is_free = True
    end = parse_dt(ev.get("endDate"))
    if any(a in ("instock", "limitedavailability", "onlineonly") for a in avail):
        state = "open"
    elif any(a in ("preorder", "presale") for a in avail):
        state = "not_open"
    elif avail and all(a in ("soldout", "outofstock") for a in avail if a):
        state = "sold_out"
    elif valid_from and min(valid_from) > now:
        state = "not_open"
    else:
        state = "unknown"
    if end and end < now:
        state = "ended"
    return TicketStatus(
        is_free=is_free,
        sales_start=min(valid_from) if valid_from else None,
        state=state,
        url=ev.get("url") or url,
        start=parse_dt(ev.get("startDate")),
        end=end,
        name=ev.get("name"),
        source="page",
    )


def status_from_api(event: dict, classes: list[dict] | None, now: datetime | None = None) -> TicketStatus:
    now = now or datetime.now(timezone.utc)
    url = event.get("url")
    start = parse_dt(event.get("start"))
    end = parse_dt(event.get("end"))
    name = (event.get("name") or {}).get("text") if isinstance(event.get("name"), dict) else event.get("name")
    is_free = event.get("is_free")
    ess = event.get("event_sales_status") or {}
    ta = event.get("ticket_availability") or {}
    classes = classes or []

    state = "unknown"
    sales_start: datetime | None = None
    statuses = [str(c.get("on_sale_status") or "").upper() for c in classes]
    if classes:
        if is_free is None:
            is_free = all(bool(c.get("free")) for c in classes)
        if "AVAILABLE" in statuses:
            state = "open"
        elif "NOT_YET_ON_SALE" in statuses:
            state = "not_open"
            starts = [parse_dt(c.get("sales_start"), timezone.utc) for c in classes
                      if str(c.get("on_sale_status") or "").upper() == "NOT_YET_ON_SALE"]
            starts = [s for s in starts if s]
            sales_start = min(starts) if starts else None
        elif statuses and all(s == "SOLD_OUT" for s in statuses):
            state = "sold_out"
        elif "SOLD_OUT" in statuses:
            state = "sold_out"
        else:
            ends = [parse_dt(c.get("sales_end"), timezone.utc) for c in classes]
            if ends and all(e and e < now for e in ends):
                state = "ended"
        if sales_start is None:
            starts = [s for s in (parse_dt(c.get("sales_start"), timezone.utc) for c in classes) if s]
            sales_start = min(starts) if starts else None

    if state == "unknown":
        ss = str(ess.get("sales_status") or "").lower()
        state = {
            "on_sale": "open", "not_yet_on_sale": "not_open", "sold_out": "sold_out",
            "sales_ended": "ended", "event_ended": "ended",
        }.get(ss, "unknown")
        if state == "unknown" and ta:
            if ta.get("is_sold_out"):
                state = "sold_out"
            elif ta.get("has_available_tickets"):
                state = "open"
    if sales_start is None:
        sales_start = parse_dt(ess.get("start_sales_date"))
    if end and end < now:
        state = "ended"
    return TicketStatus(is_free=is_free, sales_start=sales_start, state=state, url=url,
                        start=start, end=end, name=name, source="api")


class EventbriteClient:
    def __init__(self, token: str | None = None, http: httpx.AsyncClient | None = None):
        self.token = (token or "").strip()
        self.http = http or httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self._calls: list[float] = []
        self._api_off_until = 0.0
        self._page_cache: dict[str, tuple[float, str, str]] = {}

    # ---------------------------------------------------------------- plumbing
    def _api_ok(self) -> bool:
        if not self.token or time.monotonic() < self._api_off_until:
            return False
        cutoff = time.monotonic() - 3600
        self._calls = [t for t in self._calls if t > cutoff]
        if len(self._calls) >= API_HOURLY_BUDGET:
            log.warning("Eventbrite API hourly budget reached; using public pages")
            return False
        return True

    async def _api(self, path: str, params: dict | None = None) -> dict | None:
        """Returns JSON, or None on 403/404 (caller falls back). Raises on transport errors."""
        self._calls.append(time.monotonic())
        r = await self.http.get(f"{API}{path}", params=params,
                                headers={"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT})
        if r.status_code == 401:
            log.warning("Eventbrite token rejected (401); API off for 6 h")
            self._api_off_until = time.monotonic() + 6 * 3600
            return None
        if r.status_code in (403, 404):
            log.info("Eventbrite API %s → %s; falling back to public page", path, r.status_code)
            return None
        if r.status_code == 429:
            log.warning("Eventbrite rate limit hit; API off for 15 min")
            self._api_off_until = time.monotonic() + 900
            return None
        r.raise_for_status()
        return r.json()

    async def fetch_page(self, url: str) -> tuple[str, str]:
        """GET a public page (following redirects). Returns (final_url, html). Cached briefly."""
        hit = self._page_cache.get(url)
        if hit and time.monotonic() - hit[0] < PAGE_CACHE_SECONDS:
            return hit[1], hit[2]
        r = await self.http.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=TIMEOUT)
        final = str(r.url)
        html = r.text if r.status_code < 400 else ""
        self._page_cache[url] = (time.monotonic(), final, html)
        return final, html

    # -------------------------------------------------------------- public API
    async def status(self, eid: str, url: str | None = None) -> TicketStatus:
        url = url or default_event_url(eid)
        try:
            if self._api_ok():
                ev = await self._api(f"/events/{eid}/", {"expand": "ticket_availability,event_sales_status"})
                if ev is not None:
                    tc = await self._api(f"/events/{eid}/ticket_classes/")
                    st = status_from_api(ev, (tc or {}).get("ticket_classes"))
                    st.url = st.url or url
                    return st
            return await self.status_from_page(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Eventbrite status for %s failed: %s", eid, exc)
            return TicketStatus(None, None, "unknown", url, source="none")

    async def status_from_page(self, url: str) -> TicketStatus:
        final, html = await self.fetch_page(url)
        events = ld_events(html)
        if not events:
            return TicketStatus(None, None, "unknown", final or url, source="none")
        return status_from_ld(events[0], final or url)

    async def slot_states(self, eid: str, url: str | None = None) -> list["SlotInfo"] | None:
        """Availability of every timed slot (see slots.py). None when Eventbrite couldn't be read."""
        from .slots import slots_from_api, slots_from_ld

        url = url or default_event_url(eid)
        try:
            if self._api_ok():
                ev = await self._api(f"/events/{eid}/", {"expand": "ticket_availability,event_sales_status"})
                if ev is not None:
                    children = None
                    series = ev.get("series_id") if (ev.get("is_series") or ev.get("is_series_parent")) else None
                    if series:
                        data = await self._api(f"/series/{series}/events/",
                                               {"time_filter": "current_future",
                                                "expand": "ticket_availability,event_sales_status"})
                        children = (data or {}).get("events") or None
                    classes = None
                    if not children:
                        tc = await self._api(f"/events/{eid}/ticket_classes/")
                        classes = (tc or {}).get("ticket_classes")
                    return slots_from_api(ev, classes, children)
            # Public page: no cache here, a released ticket can go within minutes.
            self._page_cache.pop(url, None)
            _, html = await self.fetch_page(url)
            events = ld_events(html)
            return slots_from_ld(events) if events else None
        except Exception as exc:  # noqa: BLE001
            log.warning("Eventbrite slots for %s failed: %s", eid, exc)
            return None

    async def event_info(self, eid: str, url: str | None = None) -> EBEvent:
        url = url or default_event_url(eid)
        try:
            if self._api_ok():
                ev = await self._api(f"/events/{eid}/")
                if ev is not None:
                    name = ev.get("name")
                    return EBEvent(eid, ev.get("url") or url, parse_dt(ev.get("start")), parse_dt(ev.get("end")),
                                   name.get("text") if isinstance(name, dict) else name)
            st = await self.status_from_page(url)
            return EBEvent(eid, st.url or url, st.start, st.end, st.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Eventbrite event info for %s failed: %s", eid, exc)
            return EBEvent(eid, url, None, None)

    async def organizer_events(self, org_id: str) -> list[EBEvent] | None:
        """Upcoming events via the API, or None when the API can't be used (caller scrapes the page)."""
        if not self._api_ok():
            return None
        try:
            data = await self._api(f"/organizers/{org_id}/events/",
                                   {"status": "live,started", "order_by": "start_asc"})
        except Exception as exc:  # noqa: BLE001
            log.warning("Eventbrite organizer %s listing failed: %s", org_id, exc)
            return None
        if data is None:
            return None
        out = []
        for e in data.get("events") or []:
            name = e.get("name")
            out.append(EBEvent(str(e.get("id")), e.get("url"), parse_dt(e.get("start")), parse_dt(e.get("end")),
                               name.get("text") if isinstance(name, dict) else name))
        return out

    async def aclose(self) -> None:
        await self.http.aclose()

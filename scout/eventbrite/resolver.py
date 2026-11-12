"""Find the Eventbrite event behind an Instagram market.

Links seen in the wild: vanity subdomains that redirect to an event (x.eventbrite.es), organizer
pages (/o/<id>), direct event URLs (/e/<slug>-<id> or /e/<id>) and Linktree pages. Bio links swap
per market, so a candidate whose dates don't overlap the market's dates is rejected.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .client import EBEvent, EventbriteClient, ld_events, parse_dt

log = logging.getLogger(__name__)

MADRID = ZoneInfo("Europe/Madrid")
EVENT_ID_RE = re.compile(r"/e/(?:[^/?#]*-)?(\d{6,})(?:[/?#]|$)")
ORG_ID_RE = re.compile(r"/o/(?:[^/?#]*-)?(\d{5,})(?:[/?#]|$)")
EB_HOST_RE = re.compile(r"(?:^|\.)eventbrite\.[a-z]{2,3}(?:\.[a-z]{2})?$")
URL_RE = re.compile(r"""https?://[^\s"'<>\\)]+""")
LINK_PAGES = ("linktr.ee", "beacons.ai", "lnk.bio", "bio.link", "linkin.bio", "taplink.cc")
MAX_CANDIDATES = 8


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_eventbrite(url: str | None) -> bool:
    return bool(EB_HOST_RE.search(_host(url or "")))


def is_link_page(url: str | None) -> bool:
    h = _host(url or "")
    return any(h == d or h.endswith("." + d) for d in LINK_PAGES)


def event_id_from_url(url: str | None) -> str | None:
    if not url or not is_eventbrite(url):
        return None
    m = EVENT_ID_RE.search(urlparse(url).path + "/")
    return m.group(1) if m else None


def organizer_id_from_url(url: str | None) -> str | None:
    if not url or not is_eventbrite(url):
        return None
    m = ORG_ID_RE.search(urlparse(url).path + "/")
    return m.group(1) if m else None


def canonical_event_url(eid: str, url: str | None) -> str:
    """Strip tracking query (?aff=...) but keep the organiser's own domain."""
    if url and event_id_from_url(url) == eid:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    return f"https://www.eventbrite.es/e/{eid}"


def extract_urls(html: str) -> list[str]:
    seen: list[str] = []
    for raw in URL_RE.findall(unescape(html or "").replace("\\u002F", "/").replace("\\/", "/")):
        u = raw.rstrip(".,;")
        if u not in seen:
            seen.append(u)
    return seen


def candidate_links(booking_url: str | None, bio_links: list[str]) -> list[str]:
    """Eventbrite and link-page URLs, the post's own link first."""
    out: list[str] = []
    for u in [booking_url, *bio_links]:
        if u and (is_eventbrite(u) or is_link_page(u)) and u not in out:
            out.append(u)
    return out


def has_eventbrite_signal(booking_method: str | None, booking_url: str | None, bio_links: list[str]) -> bool:
    if booking_method == "eventbrite":
        return True
    if is_eventbrite(booking_url):
        return True
    return any(is_eventbrite(u) for u in bio_links)


@dataclass
class Resolved:
    eventbrite_id: str
    url: str


def _local_date(dt: datetime | None) -> date | None:
    return dt.astimezone(MADRID).date() if dt else None


def overlaps(cand: EBEvent, dates: list[date]) -> bool | None:
    """True/False when the candidate's dates are known, None when they aren't."""
    s, e = _local_date(cand.start), _local_date(cand.end or cand.start)
    if s is None:
        return None
    if not dates:
        return (e or s) >= datetime.now(MADRID).date()
    return s <= max(dates) and (e or s) >= min(dates)


class Resolver:
    def __init__(self, client: EventbriteClient):
        self.client = client
        self._rejected: set[tuple] = set()  # (candidate ids, dates) already reported: log each once

    async def resolve(self, dates: list[date], booking_url: str | None, bio_links: list[str]) -> Resolved | None:
        try:
            return await self._resolve(dates, candidate_links(booking_url, bio_links))
        except Exception:  # noqa: BLE001
            log.exception("Eventbrite resolve failed for %s / %s", booking_url, bio_links)
            return None

    async def _resolve(self, dates: list[date], links: list[str]) -> Resolved | None:
        cands: list[EBEvent] = []
        for link in links:
            if len(cands) >= MAX_CANDIDATES:
                break
            try:
                cands += await self._expand(link, depth=0)
            except Exception as exc:  # noqa: BLE001
                log.info("Could not follow %s: %s", link, exc)
        uniq: dict[str, EBEvent] = {}
        for c in cands:
            uniq.setdefault(c.id, c)
        cands = list(uniq.values())[:MAX_CANDIDATES]
        if not cands:
            return None

        # Fill in dates we don't know yet (one API call, or one page fetch, each).
        for c in cands:
            if c.start is None:
                info = await self.client.event_info(c.id, c.url)
                c.start, c.end = info.start, info.end
                c.url = c.url or info.url
                c.name = c.name or info.name

        good = [c for c in cands if overlaps(c, dates) is True]
        if good:
            good.sort(key=lambda c: c.start)
            best = good[0]
        else:
            unknown = [c for c in cands if overlaps(c, dates) is None]
            if not unknown:
                key = (tuple(sorted(c.id for c in cands)), tuple(dates))
                level = logging.DEBUG if key in self._rejected else logging.INFO
                self._rejected.add(key)
                log.log(level, "Eventbrite candidates %s don't match dates %s (logged once)",
                        [c.id for c in cands], [d.isoformat() for d in dates])
                return None
            best = unknown[0]  # first link (post's own, then bio) when dates can't be checked
        return Resolved(best.id, canonical_event_url(best.id, best.url))

    async def _expand(self, link: str, depth: int) -> list[EBEvent]:
        eid = event_id_from_url(link)
        if eid:
            return [EBEvent(eid, link, None, None)]
        org = organizer_id_from_url(link)
        if org:
            return await self._organizer(org, link)
        if is_link_page(link):
            if depth > 0:
                return []
            _, html = await self.client.fetch_page(link)
            out: list[EBEvent] = []
            for u in extract_urls(html):
                if is_eventbrite(u) and u != link:
                    out += await self._expand(u, depth + 1)
                    if len(out) >= MAX_CANDIDATES:
                        break
            return out
        if not is_eventbrite(link):
            return []
        # Vanity subdomain or other eventbrite page: follow redirects and look where we land.
        final, html = await self.client.fetch_page(link)
        eid = event_id_from_url(final)
        if eid:
            evs = ld_events(html)
            if evs:
                return [EBEvent(eid, final, parse_dt(evs[0].get("startDate")), parse_dt(evs[0].get("endDate")),
                                evs[0].get("name"))]
            return [EBEvent(eid, final, None, None)]
        org = organizer_id_from_url(final)
        if org:
            return await self._organizer(org, final, html)
        return self._events_in_html(html)

    async def _organizer(self, org_id: str, url: str, html: str | None = None) -> list[EBEvent]:
        listed = await self.client.organizer_events(org_id)
        if listed is not None:
            return listed
        if html is None:
            _, html = await self.client.fetch_page(url)
        return self._events_in_html(html)

    @staticmethod
    def _events_in_html(html: str) -> list[EBEvent]:
        out: dict[str, EBEvent] = {}
        for e in ld_events(html):
            eid = event_id_from_url(e.get("url"))
            if eid:
                out[eid] = EBEvent(eid, e.get("url"), parse_dt(e.get("startDate")), parse_dt(e.get("endDate")),
                                   e.get("name"))
        for u in extract_urls(html):
            eid = event_id_from_url(u)
            if eid and eid not in out:
                out[eid] = EBEvent(eid, u, None, None)
            if len(out) >= MAX_CANDIDATES:
                break
        return list(out.values())

"""Where markets are and how long the drive between them takes (for the slot planner).

* Geocoding: Nominatim (OpenStreetMap), limited to Spain and a box around the Comunidad de Madrid,
  at most one request per second, cached forever in SQLite (misses are retried after a week).
* Driving time: the public OSRM demo server, cached in SQLite.
* Madrid buffer: drive_minutes = osrm_minutes * TRAVEL_FACTOR (1.3) + PARKING_MINUTES (10).
* Fallbacks: no route -> straight-line distance at 25 km/h + parking; no coordinates at all ->
  DEFAULT_DRIVE_MINUTES (30), flagged "unknown" so the planner can say it guessed.

Everything is async httpx with short timeouts; nothing here blocks the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import Boolean, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, Database, UTCDateTime, utcnow

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{a_lon},{a_lat};{b_lon},{b_lat}"
USER_AGENT = "ScoutBot/1.0 (personal use)"
# left, top, right, bottom (lon/lat) around the Comunidad de Madrid
MADRID_VIEWBOX = "-4.58,41.17,-3.05,39.88"
FALLBACK_KMH = 25.0
SAME_PLACE_KM = 0.3
SAME_PLACE_MINUTES = 5
MISS_RETRY = timedelta(days=7)
HTTP_TIMEOUT = 8.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class GeoCache(Base):
    __tablename__ = "geo_cache"

    query: Mapped[str] = mapped_column(String(400), primary_key=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    display: Mapped[str | None] = mapped_column(String(400), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RouteCache(Base):
    __tablename__ = "route_cache"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)  # "lat,lon;lat,lon" rounded to 5 dp
    minutes: Mapped[float] = mapped_column(Float)  # raw OSRM duration
    km: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


Point = tuple[float, float]  # (lat, lon)


@dataclass(frozen=True)
class Leg:
    minutes: int          # door-to-door estimate incl. traffic factor and parking
    km: float | None
    source: str           # osrm | estimate | same | unknown

    @property
    def guessed(self) -> bool:
        return self.source in ("estimate", "unknown")


def haversine_km(a: Point, b: Point) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _norm(q: str) -> str:
    return " ".join((q or "").lower().split())


def _route_key(a: Point, b: Point) -> str:
    return f"{a[0]:.5f},{a[1]:.5f};{b[0]:.5f},{b[1]:.5f}"


class Geo:
    def __init__(self, db: Database, transport: httpx.AsyncBaseTransport | None = None,
                 travel_factor: float | None = None, parking_minutes: float | None = None,
                 default_minutes: float | None = None, min_interval_s: float = 1.0):
        self.db = db
        self.transport = transport
        self.travel_factor = travel_factor if travel_factor is not None else _env_float("TRAVEL_FACTOR", 1.3)
        self.parking = parking_minutes if parking_minutes is not None else _env_float("PARKING_MINUTES", 10)
        self.default_minutes = (default_minutes if default_minutes is not None
                                else _env_float("DEFAULT_DRIVE_MINUTES", 30))
        self.min_interval_s = min_interval_s
        self._nominatim_lock = asyncio.Lock()
        self._last_nominatim = 0.0
        Base.metadata.create_all(db.engine, tables=[GeoCache.__table__, RouteCache.__table__])

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=self.transport,
                                 headers={"User-Agent": USER_AGENT, "Accept-Language": "es"})

    # ------------------------------------------------------------- geocoding
    async def geocode(self, query: str) -> Point | None:
        q = _norm(query)
        if not q:
            return None
        with self.db.session() as s:
            row = s.get(GeoCache, q)
            if row is not None and (row.ok or utcnow() - row.fetched_at < MISS_RETRY):
                return (row.lat, row.lon) if row.ok else None
        point, display = await self._nominatim(query)
        if point is None and display == "error":
            return None  # network trouble: don't cache, try again next time
        with self.db.session() as s:
            row = s.get(GeoCache, q) or GeoCache(query=q)
            row.ok = point is not None
            row.lat, row.lon = point if point else (None, None)
            row.display = display
            row.fetched_at = utcnow()
            s.merge(row)
        return point

    async def _nominatim(self, query: str) -> tuple[Point | None, str | None]:
        params = {"q": query, "format": "jsonv2", "countrycodes": "es", "viewbox": MADRID_VIEWBOX,
                  "bounded": "1", "limit": "1"}
        async with self._nominatim_lock:  # Nominatim policy: max 1 request per second
            wait = self.min_interval_s - (time.monotonic() - self._last_nominatim)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self._client() as c:
                    r = await c.get(NOMINATIM_URL, params=params)
                if r.status_code != 200:
                    log.warning("Nominatim HTTP %s for %r", r.status_code, query)
                    return None, "error"
                data = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Nominatim failed for %r: %s", query, type(exc).__name__)
                return None, "error"
            finally:
                self._last_nominatim = time.monotonic()
        if not isinstance(data, list) or not data:
            return None, None
        try:
            return (float(data[0]["lat"]), float(data[0]["lon"])), str(data[0].get("display_name", ""))[:400]
        except (KeyError, TypeError, ValueError):
            return None, None

    async def locate(self, address: str | None, area: str | None) -> Point | None:
        """Best point for a venue: 'address, area', then the address alone, then just the area."""
        address, area = (address or "").strip(), (area or "").strip()
        tries = []
        if address and area and _norm(area) not in _norm(address):
            tries.append(f"{address}, {area}")
        if address:
            tries.append(address)
        if area:
            tries.append(area)
        for q in tries:
            p = await self.geocode(q)
            if p is not None:
                return p
        return None

    # --------------------------------------------------------------- routing
    async def osrm(self, a: Point, b: Point) -> tuple[float, float] | None:
        """(minutes, km) by car without any buffer, or None."""
        key = _route_key(a, b)
        with self.db.session() as s:
            row = s.get(RouteCache, key)
            if row is not None:
                return row.minutes, row.km
        url = OSRM_URL.format(a_lon=f"{a[1]:.6f}", a_lat=f"{a[0]:.6f}", b_lon=f"{b[1]:.6f}", b_lat=f"{b[0]:.6f}")
        try:
            async with self._client() as c:
                r = await c.get(url, params={"overview": "false"})
            data = r.json() if r.status_code == 200 else {}
            route = data["routes"][0] if data.get("code") == "Ok" and data.get("routes") else None
            if route is None:
                log.warning("OSRM gave no route (HTTP %s, %s)", r.status_code, data.get("code"))
                return None
            minutes, km = float(route["duration"]) / 60.0, float(route["distance"]) / 1000.0
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("OSRM failed: %s", type(exc).__name__)
            return None
        with self.db.session() as s:
            s.merge(RouteCache(key=key, minutes=minutes, km=km, fetched_at=utcnow()))
        return minutes, km

    def buffered(self, osrm_minutes: float) -> int:
        return math.ceil(osrm_minutes * self.travel_factor + self.parking)

    async def leg(self, a: Point | None, b: Point | None) -> Leg:
        if a is None or b is None:
            return Leg(math.ceil(self.default_minutes), None, "unknown")
        straight = haversine_km(a, b)
        if straight < SAME_PLACE_KM:
            return Leg(SAME_PLACE_MINUTES, round(straight, 1), "same")
        routed = await self.osrm(a, b)
        if routed is not None:
            return Leg(self.buffered(routed[0]), round(routed[1], 1), "osrm")
        return Leg(math.ceil(straight / FALLBACK_KMH * 60 + self.parking), round(straight, 1), "estimate")

    async def drive(self, a_addr: str | None, a_area: str | None, b_addr: str | None, b_area: str | None) -> Leg:
        a = await self.locate(a_addr, a_area)
        b = await self.locate(b_addr, b_area)
        return await self.leg(a, b)

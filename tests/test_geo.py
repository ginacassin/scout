"""Geocoding + driving times with httpx.MockTransport (no network)."""

import time

import httpx
import pytest

from scout.db import Database
from scout.geo import Geo, GeoCache, haversine_km

SOL = (40.4168, -3.7038)
CHAMBERI = (40.4340, -3.7036)
FUENLABRADA = (40.2842, -3.7942)


class Fake:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.places: dict[str, tuple[float, float]] = {}
        self.osrm_status = 200
        self.osrm_seconds = 1200.0  # 20 min
        self.osrm_meters = 6400.0
        self.fail_nominatim = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["User-Agent"] == "ScoutBot/1.0 (personal use)"
        if request.url.host == "nominatim.openstreetmap.org":
            if self.fail_nominatim:
                raise httpx.ConnectTimeout("slow")
            p = request.url.params
            assert p["format"] == "jsonv2" and p["countrycodes"] == "es" and p["bounded"] == "1"
            assert p["viewbox"] == "-4.58,41.17,-3.05,39.88" and "email" not in p
            hit = self.places.get(p["q"])
            body = [{"lat": str(hit[0]), "lon": str(hit[1]), "display_name": p["q"]}] if hit else []
            return httpx.Response(200, json=body)
        if request.url.host == "router.project-osrm.org":
            assert request.url.path.startswith("/route/v1/driving/")
            assert request.url.params["overview"] == "false"
            if self.osrm_status != 200:
                return httpx.Response(self.osrm_status, text="busy")
            return httpx.Response(200, json={"code": "Ok", "routes": [
                {"duration": self.osrm_seconds, "distance": self.osrm_meters}]})
        return httpx.Response(404)

    def count(self, host: str) -> int:
        return sum(1 for r in self.calls if r.url.host == host)


@pytest.fixture
def geo(tmp_path):
    fake = Fake()
    g = Geo(Database(tmp_path / "g.db"), transport=httpx.MockTransport(fake.handler), travel_factor=1.3,
            parking_minutes=10, default_minutes=30, min_interval_s=0)
    g.fake = fake
    return g


async def test_geocode_and_cache_hit(geo):
    geo.fake.places["Calle de Clara del Rey 12, Madrid"] = CHAMBERI
    assert await geo.geocode("Calle de Clara del Rey 12, Madrid") == CHAMBERI
    assert await geo.geocode("  calle de clara del rey 12,   MADRID ") == CHAMBERI  # normalised cache key
    assert geo.fake.count("nominatim.openstreetmap.org") == 1


async def test_miss_is_cached_but_network_error_is_not(geo):
    assert await geo.geocode("Nowhere street 1") is None
    assert await geo.geocode("Nowhere street 1") is None
    assert geo.fake.count("nominatim.openstreetmap.org") == 1
    geo.fake.fail_nominatim = True
    assert await geo.geocode("Calle Mayor 1") is None
    geo.fake.fail_nominatim = False
    geo.fake.places["Calle Mayor 1"] = SOL
    assert await geo.geocode("Calle Mayor 1") == SOL
    with geo.db.session() as s:
        assert s.get(GeoCache, "calle mayor 1").ok


async def test_locate_falls_back_to_area(geo):
    geo.fake.places["Fuenlabrada"] = FUENLABRADA
    assert await geo.locate("Polígono Cobo Calleja nave 99", "Fuenlabrada") == FUENLABRADA
    qs = [r.url.params["q"] for r in geo.fake.calls]
    assert qs == ["Polígono Cobo Calleja nave 99, Fuenlabrada", "Polígono Cobo Calleja nave 99", "Fuenlabrada"]
    assert await geo.locate(None, None) is None


async def test_osrm_leg_with_madrid_buffer_and_cache(geo):
    leg = await geo.leg(SOL, CHAMBERI)
    assert leg.source == "osrm" and leg.km == 6.4
    assert leg.minutes == 36  # 20 * 1.3 + 10
    await geo.leg(SOL, CHAMBERI)
    assert geo.fake.count("router.project-osrm.org") == 1


async def test_osrm_failure_falls_back_to_haversine(geo):
    geo.fake.osrm_status = 503
    leg = await geo.leg(SOL, FUENLABRADA)
    km = haversine_km(SOL, FUENLABRADA)
    assert leg.source == "estimate" and leg.guessed
    assert leg.minutes == int(-(-(km / 25 * 60 + 10) // 1))  # ceil
    assert 15 < km < 18


async def test_unknown_and_same_place(geo):
    leg = await geo.leg(None, SOL)
    assert (leg.minutes, leg.source) == (30, "unknown")
    leg = await geo.leg(SOL, (SOL[0] + 0.001, SOL[1]))
    assert leg.source == "same" and leg.minutes == 5
    assert geo.fake.count("router.project-osrm.org") == 0


async def test_drive_end_to_end(geo):
    geo.fake.places["Calle Mayor 1"] = SOL
    geo.fake.places["Chamberí"] = CHAMBERI
    leg = await geo.drive("Calle Mayor 1", None, None, "Chamberí")
    assert leg.source == "osrm" and leg.minutes == 36


async def test_nominatim_rate_limit(tmp_path):
    fake = Fake()
    g = Geo(Database(tmp_path / "g.db"), transport=httpx.MockTransport(fake.handler), min_interval_s=0.2)
    t0 = time.monotonic()
    for q in ("a 1", "b 2", "c 3"):
        await g.geocode(q)
    assert time.monotonic() - t0 >= 0.4

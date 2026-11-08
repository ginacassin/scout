"""Replays real post sequences seen on 7 Nov 2026 through the pipeline.

The parser is faked with what Gemini should return (see tests/fixtures/test_battery.json),
so these tests check the matching, merging, filtering and alert decisions.
`scripts/eval_parser.py` checks the Gemini side against the same battery.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scout.config import Settings
from scout.db import Database, Event
from scout.parser.schema import (
    BookingMethod, BookingStatus, ItemKind, ParsedEvent, ParseResult, Region, Session,
)
from scout.pipeline import Candidate, Scout

TZ = ZoneInfo("Europe/Madrid")


def ev(title, dates=(), address=None, area=None, region=Region.madrid, booking=BookingMethod.free_entry,
       opens=None, status=BookingStatus.unknown, sessions=(), change=None, link_in_bio=False, url=None, organizer=None):
    return ParsedEvent(
        title=title, dates=list(dates), sessions=[Session(**s) for s in sessions], address=address, area=area,
        region=region, organizer=organizer, booking_method=booking, booking_url=url, booking_opens_at=opens,
        booking_scope=None, booking_status=status, link_in_bio=link_in_bio, price=None, summary_en="Furniture and lamps.",
        change=change, confidence=0.9,
    )


class FakeParser:
    def __init__(self):
        self.answers: dict[str, ParseResult] = {}

    async def parse(self, **kw):
        return self.answers[kw["caption"]]


class FakeNotifier:
    def __init__(self):
        self.new: list[tuple[str, str]] = []
        self.updates: list[tuple[str, list[str]]] = []
        self.warnings: list[str] = []

    async def new_event(self, event, kind):
        self.new.append((event.title, kind))

    async def event_update(self, event, changes):
        self.updates.append((event.title, changes))

    async def warn(self, text):
        self.warnings.append(text)


@pytest.fixture
def world(tmp_path: Path):
    settings = Settings(
        bot_token="x", webhook_url="https://bots.rabbia.net/scoutbot", port=8000, allowed_chat_id=1,
        gemini_api_key="x", gemini_model="m", ig_provider="apify", apify_token="x", eventbrite_token="",
        gcal_sa_json="", gcal_calendar_id="", tz=TZ, data_dir=tmp_path,
    )
    db = Database(":memory:")
    parser, notifier = FakeParser(), FakeNotifier()
    scout = Scout(settings, db, source=None, parser=parser, notifier=notifier)
    return scout, db, parser, notifier


def post(scout, db, handle, sc, when, caption, result: ParseResult, pinned=False, coauthors=None):
    acc, _ = db.add_account(handle)
    scout.parser.answers[caption] = result
    return Candidate(account=acc, kind="post", ig_id=f"post:{sc}", url=f"https://www.instagram.com/p/{sc}/",
                     posted_at=when, caption=caption, image_urls=[], pinned=pinned, coauthors=coauthors)


def at(day, hour=12, month=11):
    return datetime(2026, month, day, hour, tzinfo=TZ).astimezone(timezone.utc)


async def run(scout, cand, now):
    """Process with a fixed 'now' by temporarily overriding utcnow inside the pipeline."""
    import scout.pipeline as p
    orig = p.utcnow
    p.utcnow = lambda: now
    try:
        return await scout.process(cand)
    finally:
        p.utcnow = orig


@pytest.mark.asyncio
async def test_mercadillosencasa_announce_then_reminders(world):
    scout, db, parser, notifier = world
    chamberi = dict(dates=["2026-11-06", "2026-11-07", "2026-11-08"], address="C/ Fernández de la Hoz 78, 6H, Chamberí, Madrid",
                    organizer="circularhome.es")
    m4 = post(scout, db, "mercadillosencasa", "DfHtc9COcUh", at(30, month=10), "M4",
              ParseResult(kind=ItemKind.new_event, events=[ev("Mercadillo en Casa — Fernández de la Hoz", **chamberi)]),
              coauthors=["circularhome.es"])
    m2 = post(scout, db, "mercadillosencasa", "DfNfaiyOSRp", at(1), "M2",
              ParseResult(kind=ItemKind.reminder, events=[ev("Mercadillo en Casa Chamberí", **chamberi)]))
    m1 = post(scout, db, "mercadillosencasa", "Dfa1vtwoFoP", at(6), "M1",
              ParseResult(kind=ItemKind.reminder, events=[ev("Mercadillos en Casa Chamberí", **chamberi)]))
    await run(scout, m4, at(30, month=10))
    await run(scout, m2, at(1))
    await run(scout, m1, at(6))
    assert len(notifier.new) == 1  # one alert, reminders merged silently
    with db.session() as s:
        assert s.query(Event).count() == 1


@pytest.mark.asyncio
async def test_roundup_post_creates_four_events(world):
    scout, db, parser, notifier = world
    events = [ev(f"Vecinal {town}", dates=["2026-11-01"], area=town) for town in ("Alcobendas", "Coslada", "Aranjuez", "Navacerrada")]
    c = post(scout, db, "mercadillosencasa", "DfJ1qx5uAoI", at(31, month=10), "M3",
             ParseResult(kind=ItemKind.new_event, events=events))
    await run(scout, c, at(31, month=10))
    assert len(notifier.new) == 4


@pytest.mark.asyncio
async def test_unrelated_and_old_pinned_are_skipped(world):
    scout, db, parser, notifier = world
    thanks = post(scout, db, "mercadillosencasa", "DfDU2h-ox3K", at(28, month=10), "M5", ParseResult(kind=ItemKind.unrelated, events=[]))
    pinned = post(scout, db, "arquitecturadelorden", "C8AUSI8tBhT", datetime(2024, 6, 9, tzinfo=timezone.utc), "A1",
                  ParseResult(kind=ItemKind.unrelated, events=[]), pinned=True)
    await run(scout, thanks, at(28, month=10))
    await run(scout, pinned, at(7))
    assert notifier.new == []
    with db.session() as s:
        from scout.db import SourceItem
        row = s.query(SourceItem).filter_by(ig_id="post:C8AUSI8tBhT").one()
        assert row.skipped_reason == "old_pinned" and row.parsed_json is None  # Gemini not called


@pytest.mark.asyncio
async def test_arquitectura_naluz_and_alicante(world):
    scout, db, parser, notifier = world
    naluz = dict(dates=["2026-10-29", "2026-10-30", "2026-10-31"], address="Calle Galileo 74, 28015 Madrid")
    a3 = post(scout, db, "arquitecturadelorden", "De4GaIlAmsW", at(24, month=10), "A3",
              ParseResult(kind=ItemKind.new_event, events=[ev("Puertas Abiertas Naluz", **naluz, booking=BookingMethod.eventbrite,
                                                               opens="2026-10-27T10:00:00", link_in_bio=True,
                                                               status=BookingStatus.not_open)]))
    a4 = post(scout, db, "arquitecturadelorden", "De_nSy2DguP", at(27, month=10), "A4",
              ParseResult(kind=ItemKind.new_event, events=[ev("Puertas Abiertas Vistahermosa", dates=["2026-10-30", "2026-10-31", "2026-11-01"],
                                                               address="Calle del Chopo 6, 03015 Alicante", region=Region.outside)]))
    a5 = post(scout, db, "arquitecturadelorden", "DfEebusOcO_", at(29, month=10), "A5",
              ParseResult(kind=ItemKind.reminder, events=[ev("Puertas Abiertas Naluz", **naluz, booking=BookingMethod.eventbrite)]))
    a7 = post(scout, db, "arquitecturadelorden", "DfFzseGCZjw", at(29, 21, month=10), "A7",
              ParseResult(kind=ItemKind.update, events=[ev("Puertas Abiertas Naluz", **naluz, booking=BookingMethod.eventbrite,
                                                               status=BookingStatus.cancellations, change="cancellations")]))
    a9 = post(scout, db, "arquitecturadelorden", "DfU9M_sjuj2", at(4), "A9",
              ParseResult(kind=ItemKind.new_event, events=[ev("Puertas Abiertas Vistahermosa — second round", dates=["2026-11-06", "2026-11-07"],
                                                               address="Calle del Chopo 6, 03015 Alicante", region=Region.outside)]))
    await run(scout, a3, at(24, month=10))
    with db.session() as s:
        s.query(Event).filter_by(title="Puertas Abiertas Naluz").one().user_decision = "yes"
    for c, when in ((a4, at(27, month=10)), (a5, at(29, month=10)), (a7, at(29, 21, month=10)), (a9, at(4))):
        await run(scout, c, when)

    assert [t for t, _ in notifier.new] == ["Puertas Abiertas Naluz"]  # Alicante filtered, reminder merged
    assert len(notifier.updates) == 1 and "cancellations" in notifier.updates[0][1][0]
    with db.session() as s:
        alicante = s.query(Event).filter(Event.region == "outside").all()
        assert len(alicante) == 2  # second round is its own event, not merged
        naluz_ev = s.query(Event).filter_by(title="Puertas Abiertas Naluz").one()
        assert naluz_ev.booking_opens_at.astimezone(TZ).hour == 10
        assert naluz_ev.link_in_bio


@pytest.mark.asyncio
async def test_home_market_dated_without_address_then_confirmed_for_maybe(world):
    scout, db, parser, notifier = world
    l2 = post(scout, db, "homemarket_lmh", "DfW2UDiCN96", at(5), "L2",
              ParseResult(kind=ItemKind.new_event, events=[ev("Home Market — warehouse in Cobo Calleja",
                                                               dates=["2026-11-13", "2026-11-14", "2026-11-15"], area="Cobo Calleja (Fuenlabrada)")]))
    l3 = post(scout, db, "homemarket_lmh", "DfaOqPTCFHH", at(6), "L3",
              ParseResult(kind=ItemKind.update, events=[ev("Home Market Cobo Calleja", dates=["2026-11-13", "2026-11-14", "2026-11-15"],
                                                            address="Pol. Ind. Cobo Calleja, C. Villafranca del Bierzo 56, Fuenlabrada",
                                                            area="Cobo Calleja (Fuenlabrada)", booking=BookingMethod.eventbrite,
                                                            opens="2026-11-08T20:00:00", link_in_bio=True)]))
    await run(scout, l2, at(5))
    with db.session() as s:
        e = s.query(Event).one()
        assert e.status == "announced" and e.address is None
        e.user_decision = "maybe"
    await run(scout, l3, at(6))
    assert len(notifier.new) == 1
    title, changes = notifier.updates[0]
    assert any(c.startswith("address") for c in changes)
    assert any("booking opens" in c for c in changes)
    with db.session() as s:
        e = s.query(Event).one()
        assert e.status == "confirmed"
        assert e.booking_opens_at.astimezone(TZ).hour == 20


@pytest.mark.asyncio
async def test_circular_teaser_then_announcement_is_one_market(world):
    scout, db, parser, notifier = world
    c5 = post(scout, db, "thecircularmarket.es", "DfDPod-D62a", at(28, month=10), "C5",
              ParseResult(kind=ItemKind.teaser, events=[ev("The Circular Market — a house with stories", region=Region.unknown)]))
    c1 = post(scout, db, "thecircularmarket.es", "DfZkLmIDRJk", at(6), "C1",
              ParseResult(kind=ItemKind.new_event, events=[ev("Mercadillo Clara del Rey", dates=["2026-11-13", "2026-11-14", "2026-11-15"],
                                                               address="C/ Clara del Rey 14, 6.º A, 28002 Madrid", booking=BookingMethod.eventbrite,
                                                               url="https://claradelrey.eventbrite.com", opens="2026-11-10T10:00:00")]),
              pinned=True)
    await run(scout, c5, at(28, month=10))
    with db.session() as s:
        s.query(Event).one().user_decision = "maybe"
    await run(scout, c1, at(6))
    assert len(notifier.new) == 1  # the teaser
    assert notifier.updates and any(c.startswith("date announced") for c in notifier.updates[0][1])


@pytest.mark.asyncio
async def test_past_announcement_and_orphan_followup_do_not_alert(world):
    scout, db, parser, notifier = world
    late = post(scout, db, "almamarket_home", "X1", at(7), "late",
                ParseResult(kind=ItemKind.new_event, events=[ev("Old market", dates=["2026-11-01"])]))
    orphan = post(scout, db, "almamarket_home", "X2", at(7), "orphan",
                  ParseResult(kind=ItemKind.last_day, events=[ev("Último día", change="50% off")]))
    await run(scout, late, at(7))
    await run(scout, orphan, at(7))
    assert notifier.new == []


@pytest.mark.asyncio
async def test_seen_items_are_never_processed_twice(world):
    scout, db, parser, notifier = world
    c = post(scout, db, "my_market_mad", "DfVUfq2O2l0", at(4), "Y3",
             ParseResult(kind=ItemKind.new_event, events=[ev("My Market — Martínez Villergas", dates=["2026-11-13", "2026-11-14", "2026-11-15"],
                                                              booking=BookingMethod.eventbrite, opens="2026-11-09T00:00:00")]))
    assert await run(scout, c, at(4)) == 1
    assert await run(scout, c, at(4)) == 0
    assert len(notifier.new) == 1
    with db.session() as s:
        assert s.query(Event).one().booking_opens_at.astimezone(TZ).hour == 0  # midnight opening kept


@pytest.mark.asyncio
async def test_my_market_bio_then_post_with_different_area_wording_is_one_market(world):
    scout, db, parser, notifier = world
    dates = ["2026-11-13", "2026-11-14", "2026-11-15"]
    bio = post(scout, db, "my_market_mad", "BIO1", at(4), "bio",
               ParseResult(kind=ItemKind.new_event, events=[ev("Martínez Villergas Market", dates=dates, area="Martínez Villergas")]))
    y3 = post(scout, db, "my_market_mad", "DfVUfq2O2l0", at(4, 13), "Y3b",
              ParseResult(kind=ItemKind.new_event, events=[ev("Market Martínez Villergas", dates=dates,
                                                               address="C/ Caribe 5 y 7, esquina C/ Martínez Villergas",
                                                               area="Ciudad Lineal", booking=BookingMethod.eventbrite)]))
    await run(scout, bio, at(4))
    await run(scout, y3, at(4, 13))
    assert len(notifier.new) == 1
    with db.session() as s:
        e = s.query(Event).one()
        assert e.address and e.booking_method == "eventbrite"


def test_dedupe_merges_existing_duplicates(world):
    scout, db, parser, notifier = world
    acc, _ = db.add_account("my_market_mad")
    with db.session() as s:
        a = Event(account_id=acc.id, title="Martínez Villergas Market", area="Martínez Villergas", user_decision="pending")
        a.date_list = [datetime(2026, 11, d).date() for d in (13, 14, 15)]
        b = Event(account_id=acc.id, title="Market Martínez Villergas", address="C/ Caribe 5 y 7",
                  booking_method="eventbrite", user_decision="yes")
        b.date_list = [datetime(2026, 11, d).date() for d in (13, 14, 15)]
        c = Event(account_id=acc.id, title="Vecinal Coslada", area="Coslada")
        c.date_list = [datetime(2026, 11, 13).date()]
        s.add_all([a, b, c])
    assert scout.dedupe_events() == 1
    with db.session() as s:
        keep = s.get(Event, 1)
        assert keep.address == "C/ Caribe 5 y 7" and keep.user_decision == "yes" and keep.booking_method == "eventbrite"
        assert s.get(Event, 2).status == "past" and s.get(Event, 3).status != "past"

"""Duplicate markets: the real cases of 8 Nov 2026 (#8/#9 my_market_mad, #5/#6 homemarket_lmh), the cases
that must stay separate (weekend roundup, Madrid vs Alicante, second round), the Gemini tie-breaker
(faked, no network), the backfill, in-place card refresh and /merge."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from scout import hooks
from scout.bot.admin import AdminTools, register_admin
from scout.config import Settings
from scout.db import Account, BookingWatch, Database, Event, SourceItem
from scout.dedupe import Matcher, SameMarketVerdict, upgrade_address
from scout.matching import (
    DIFFERENT, MAYBE, SAME, Facts, address_contains, address_specificity, compare, find_match, fold,
    is_estate_only, street_tokens, towns_in,
)
from scout.parser.schema import BookingMethod, ItemKind, ParseResult, Region
from scout.pipeline import Scout
from test_pipeline_real_cases import TZ, FakeNotifier, FakeParser, at, ev, post, run

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
WEEKEND = ["2026-11-13", "2026-11-14", "2026-11-15"]
WEEKEND_D = {date(2026, 11, 13), date(2026, 11, 14), date(2026, 11, 15)}
VILLERGAS_FULL = "C/ Caribe 5 y 7 esq c/ Martínez Villergas, escalera B piso 14 derecha, Edificio Maya"


class FakeTie:
    def __init__(self, answer: bool = True, fail: bool = False, confidence: float = 0.9):
        self.answer, self.fail, self.confidence = answer, fail, confidence
        self.calls: list[tuple[str, str, str | None]] = []

    async def same_market(self, a, b, caption):
        self.calls.append((a["title"], b["title"], caption))
        if self.fail:
            raise RuntimeError("gemini down")
        return SameMarketVerdict(same_market=self.answer, confidence=self.confidence, reason="fake")


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear()
    yield
    hooks.clear()


def make_world(tmp_path: Path, tie=None):
    settings = Settings(
        bot_token="1:x", webhook_url="", port=8000, allowed_chat_id=42, gemini_api_key="x", gemini_model="m",
        ig_provider="apify", apify_token="x", eventbrite_token="", gcal_sa_json="", gcal_calendar_id="", tz=TZ,
        data_dir=tmp_path,
    )
    db = Database(":memory:")
    scout = Scout(settings, db, source=None, parser=FakeParser(), notifier=FakeNotifier(), tiebreaker=tie)
    return scout, db, settings


def events(db):
    with db.session() as s:
        return list(s.query(Event).order_by(Event.id))


def live(db):
    return [e for e in events(db) if e.status != "past"]


# ---------------------------------------------------------------- pure signals
def test_street_normalisation_and_containment():
    assert fold("C/ Martínez Villergas") == "c martinez villergas"
    assert street_tokens(VILLERGAS_FULL) == {"caribe", "martinez", "villergas", "maya"}
    assert street_tokens("Calle de Martínez Villergas") == {"martinez", "villergas"}
    assert address_contains("Calle de Martínez Villergas", VILLERGAS_FULL)
    assert address_contains("Pol. Ind. Cobo Calleja", "Polígono industrial Cobo Calleja")
    assert not address_contains("Avenida Olímpica", "Avenida de Madrid 11")
    assert is_estate_only("Polígono industrial Cobo Calleja")
    assert not is_estate_only("C. Villafranca del Bierzo, 56")
    assert address_specificity("Polígono industrial Cobo Calleja") == 1
    assert address_specificity("Calle de Martínez Villergas") == 2
    assert address_specificity(VILLERGAS_FULL) == 3


def test_towns_skip_street_names():
    assert towns_in("Polígono industrial Cobo Calleja", skip_streets=True) == {"fuenlabrada"}
    assert towns_in("Avenida de Madrid 15", skip_streets=True) == set()
    assert towns_in("Calle de Toledo 3, Alcobendas", skip_streets=True) == {"alcobendas"}
    assert towns_in("Home Market — Warehouse in Fuenlabrada") == {"fuenlabrada"}


def test_address_upgrade_prefers_specific_and_keeps_estate():
    assert upgrade_address("Calle de Martínez Villergas", VILLERGAS_FULL) == (VILLERGAS_FULL, True)
    assert upgrade_address(VILLERGAS_FULL, "Calle de Martínez Villergas") == (VILLERGAS_FULL, False)
    assert upgrade_address("Polígono industrial Cobo Calleja", "C. Villafranca del Bierzo, 56") == (
        "Polígono industrial Cobo Calleja, C. Villafranca del Bierzo, 56", True)


def F(title, address=None, area=None, region="madrid", dates=WEEKEND_D, brands=("my_market_mad",), created=NOW):
    return Facts(title, set(dates), address, area, region, created, set(brands))


def test_compare_real_cases():
    v = compare(F("Market Martínez Villergas", VILLERGAS_FULL), F("Martínez Villergas Market", "Calle de Martínez Villergas"), NOW)
    assert v.verdict == SAME
    hm = ("homemarket_lmh",)
    v = compare(F("Home Market — Warehouse in Fuenlabrada", "C. Villafranca del Bierzo, 56", brands=hm),
                F("Home Market at Cobo Calleja Industrial Estate", "Polígono industrial Cobo Calleja", brands=hm), NOW)
    assert v.verdict == MAYBE and v.lean  # Gemini decides; without it, merge


def test_compare_must_stay_separate():
    me = ("mercadillosencasa",)
    d1 = {date(2026, 11, 1)}
    roundup = [("Vecinal Alcobendas", "Avenida Olímpica", "Alcobendas"), ("Vecinal Coslada", "Avenida de Madrid 11", "Coslada"),
               ("Vintage de Aranjuez", "Calle de la Reina", "Aranjuez"), ("Rastro de Navacerrada", "Avenida de Madrid 15", "Navacerrada")]
    for i, a in enumerate(roundup):
        for b in roundup[i + 1:]:
            assert compare(F(*a, dates=d1, brands=me), F(*b, dates=d1, brands=me), NOW).verdict == DIFFERENT
    # same street, other door
    assert compare(F("Rastro", "Avenida de Madrid 15", dates=d1, brands=me),
                   F("Rastro", "Avenida de Madrid 11", dates=d1, brands=me), NOW).verdict == DIFFERENT
    ar = ("arquitecturadelorden",)
    assert compare(F("Puertas Abiertas Vistahermosa", "Calle del Chopo 6, 03015 Alicante", region="outside", brands=ar),
                   F("Puertas Abiertas Naluz", "Calle Galileo 74, 28015 Madrid", brands=ar), NOW).verdict == DIFFERENT
    second = {date(2026, 11, 20), date(2026, 11, 21)}
    assert compare(F("Market Martínez Villergas — second round", VILLERGAS_FULL, dates=second),
                   F("Market Martínez Villergas", VILLERGAS_FULL), NOW).verdict == DIFFERENT


def test_find_match_deterministic_prefers_oldest_sure_match():
    a = SimpleNamespace(id=1, title="Martínez Villergas Market", address="Calle de Martínez Villergas", area=None,
                        created_at=NOW - timedelta(days=3), date_list=sorted(WEEKEND_D), region="madrid")
    b = SimpleNamespace(id=2, title="Market Martínez Villergas", address=VILLERGAS_FULL, area=None,
                        created_at=NOW - timedelta(days=1), date_list=sorted(WEEKEND_D), region="madrid")
    parsed = ev("My Market Martínez Villergas", dates=WEEKEND, address=VILLERGAS_FULL)
    assert find_match(parsed, [b, a], NOW, brands={"my_market_mad"}) is a


# ---------------------------------------------------------------- live pipeline
@pytest.mark.asyncio
async def test_my_market_villergas_reannounced_is_one_event_without_gemini(tmp_path):
    tie = FakeTie()
    scout, db, _ = make_world(tmp_path, tie)
    p8 = post(scout, db, "my_market_mad", "Y8", at(4), "y8",
              ParseResult(kind=ItemKind.new_event, events=[ev("Martínez Villergas Market", dates=WEEKEND,
                                                               address="Calle de Martínez Villergas")]))
    p9 = post(scout, db, "my_market_mad", "Y9", at(5), "y9",
              ParseResult(kind=ItemKind.update, events=[ev("Market Martínez Villergas", dates=WEEKEND, address=VILLERGAS_FULL,
                                                            booking=BookingMethod.eventbrite)]))
    await run(scout, p8, at(4))
    await run(scout, p9, at(5))
    assert len(scout.notifier.new) == 1 and tie.calls == []
    (e,) = events(db)
    assert e.address == VILLERGAS_FULL and e.booking_method == "eventbrite"


@pytest.mark.asyncio
@pytest.mark.parametrize("tie, merged, calls", [
    (FakeTie(answer=True), True, 1),
    (FakeTie(answer=False), False, 1),
    (FakeTie(fail=True), True, 1),  # Gemini down: deterministic lean decides
    (None, True, 0),
])
async def test_home_market_estate_vs_street_uses_tiebreaker(tmp_path, tie, merged, calls):
    scout, db, _ = make_world(tmp_path, tie)
    p5 = post(scout, db, "homemarket_lmh", "H5", at(5), "h5",
              ParseResult(kind=ItemKind.new_event, events=[ev("Home Market at Cobo Calleja Industrial Estate", dates=WEEKEND,
                                                               address="Polígono industrial Cobo Calleja")]))
    p6 = post(scout, db, "homemarket_lmh", "H6", at(6), "Abrimos las puertas de una nave en Fuenlabrada",
              ParseResult(kind=ItemKind.update, events=[ev("Home Market — Warehouse in Fuenlabrada", dates=WEEKEND,
                                                            address="C. Villafranca del Bierzo, 56")]))
    await run(scout, p5, at(5))
    await run(scout, p6, at(6))
    if tie is not None:
        assert len(tie.calls) == calls
        assert tie.calls[0][2] == "Abrimos las puertas de una nave en Fuenlabrada"  # newest caption sent
    if merged:
        (e,) = events(db)
        assert e.address == "Polígono industrial Cobo Calleja, C. Villafranca del Bierzo, 56"
        assert len(scout.notifier.new) == 1
    else:
        assert len(events(db)) == 2


@pytest.mark.asyncio
async def test_verdicts_are_cached_per_pair(tmp_path):
    tie = FakeTie()
    _, db, _ = make_world(tmp_path)
    m = Matcher(db, tie)
    hm = ("homemarket_lmh",)
    old = F("Home Market at Cobo Calleja Industrial Estate", "Polígono industrial Cobo Calleja", brands=hm)
    new = F("Home Market — Warehouse in Fuenlabrada", "C. Villafranca del Bierzo, 56", brands=hm)
    cmp = compare(new, old, NOW)
    assert await m.decide(cmp, old, new, 5, "post:X#1", None) == (True, "gemini")
    assert await m.decide(cmp, old, new, 5, "post:X#1", None) == (True, "cache")
    assert len(tie.calls) == 1


@pytest.mark.asyncio
async def test_roundup_alicante_and_second_round_stay_separate(tmp_path):
    tie = FakeTie(answer=True)  # would merge anything it is asked about
    scout, db, _ = make_world(tmp_path, tie)
    towns = [("Alcobendas", "Avenida Olímpica"), ("Coslada", "Avenida de Madrid 11"), ("Aranjuez", "Calle de la Reina"),
             ("Navacerrada", "Avenida de Madrid 15")]
    roundup = post(scout, db, "mercadillosencasa", "M3", at(13), "m3", ParseResult(kind=ItemKind.new_event, events=[
        ev(f"Neighbourhood market {t}", dates=["2026-11-15"], address=a, area=t) for t, a in towns]))
    madrid = post(scout, db, "arquitecturadelorden", "A1", at(9), "a1", ParseResult(kind=ItemKind.new_event, events=[
        ev("Puertas Abiertas Naluz", dates=WEEKEND, address="Calle Galileo 74, 28015 Madrid")]))
    alicante = post(scout, db, "arquitecturadelorden", "A2", at(10), "a2", ParseResult(kind=ItemKind.new_event, events=[
        ev("Puertas Abiertas Vistahermosa", dates=WEEKEND, address="Calle del Chopo 6, 03015 Alicante", region=Region.outside)]))
    second = post(scout, db, "arquitecturadelorden", "A3", at(16), "a3", ParseResult(kind=ItemKind.new_event, events=[
        ev("Puertas Abiertas Naluz — second round", dates=["2026-11-20", "2026-11-21"], address="Calle Galileo 74, 28015 Madrid")]))
    for c, when in ((roundup, at(13)), (madrid, at(9)), (alicante, at(10)), (second, at(16))):
        await run(scout, c, when)
    assert len(events(db)) == 7
    assert tie.calls == []


# ---------------------------------------------------------------- backfill
def _add(s, acc, title, address=None, area=None, days=WEEKEND_D, created=None, **kw):
    e = Event(account_id=acc.id, title=title, address=address, area=area, region="madrid", **kw)
    e.date_list = sorted(days)
    e.created_at = created or NOW
    s.add(e)
    s.flush()
    return e.id


@pytest.mark.asyncio
async def test_backfill_merges_real_duplicates_and_keeps_roundup(tmp_path):
    tie = FakeTie(answer=True)
    scout, db, _ = make_world(tmp_path, tie)
    merged_hooks: list[tuple[int, int]] = []

    async def on_merged(keep_id, merged_id):
        merged_hooks.append((keep_id, merged_id))

    hooks.on("events_merged", on_merged)
    for h in ("homemarket_lmh", "my_market_mad", "mercadillosencasa"):
        db.add_account(h)
    with db.session() as s:
        acc = {a.handle: a for a in s.query(Account)}
        e5 = _add(s, acc["homemarket_lmh"], "Home Market at Cobo Calleja Industrial Estate",
                  "Polígono industrial Cobo Calleja", created=NOW - timedelta(days=4), telegram_message_id=500)
        e6 = _add(s, acc["homemarket_lmh"], "Home Market — Warehouse in Fuenlabrada", "C. Villafranca del Bierzo, 56",
                  created=NOW - timedelta(days=3), booking_method="eventbrite", telegram_message_id=600)
        e8 = _add(s, acc["my_market_mad"], "Martínez Villergas Market", "Calle de Martínez Villergas",
                  created=NOW - timedelta(days=4), telegram_message_id=800,
                  notes=json.dumps([{"at": "2026-11-05T10:00:00", "text": "green lamp"}]))
        e9 = _add(s, acc["my_market_mad"], "Market Martínez Villergas", VILLERGAS_FULL, created=NOW - timedelta(days=2),
                  user_decision="yes", telegram_message_id=900,
                  notes=json.dumps([{"at": "2026-11-06T10:00:00", "text": "bring cash"}]))
        rid = [_add(s, acc["mercadillosencasa"], f"Vecinal {t}", area=t, days={date(2026, 11, 15)})
               for t in ("Alcobendas", "Coslada", "Aranjuez", "Navacerrada")]
        s.add(BookingWatch(event_id=e9, eventbrite_id="2004504034527", state="waiting"))
        s.add(SourceItem(account_id=acc["my_market_mad"].id, ig_id="post:Y9", kind="post", caption="Market…",
                         event_ids=json.dumps([e9])))
        s.add(SourceItem(account_id=acc["homemarket_lmh"].id, ig_id="post:H6", kind="post", caption="nave Fuenlabrada",
                         event_ids=json.dumps([e6])))

    assert await scout.dedupe_events() == 2
    assert sorted(merged_hooks) == sorted([(e5, e6), (e8, e9)])
    assert len(tie.calls) == 1 and tie.calls[0][2] == "nave Fuenlabrada"  # only #5/#6 was inconclusive
    with db.session() as s:
        k8, d9 = s.get(Event, e8), s.get(Event, e9)
        assert k8.address == VILLERGAS_FULL and k8.user_decision == "yes" and k8.telegram_message_id == 800
        assert [n["text"] for n in k8.note_list] == ["green lamp", "bring cash"]
        assert d9.status == "past" and d9.user_decision == "filtered"
        assert "duplicate of" in d9.change_log
        assert s.query(BookingWatch).one().event_id == e8
        assert json.loads(s.query(SourceItem).filter_by(ig_id="post:Y9").one().event_ids) == [e8]
        k5 = s.get(Event, e5)
        assert k5.address == "Polígono industrial Cobo Calleja, C. Villafranca del Bierzo, 56"
        assert k5.booking_method == "eventbrite"
        assert all(s.get(Event, r).status != "past" for r in rid)
    # idempotent, and no new Gemini call
    assert await scout.dedupe_events() == 0
    assert len(tie.calls) == 1


@pytest.mark.asyncio
async def test_backfill_drops_duplicate_watch_when_kept_has_one(tmp_path):
    scout, db, _ = make_world(tmp_path)
    db.add_account("my_market_mad")
    with db.session() as s:
        acc = s.query(Account).one()
        a = _add(s, acc, "Martínez Villergas Market", "Calle de Martínez Villergas", created=NOW - timedelta(days=2))
        b = _add(s, acc, "Market Martínez Villergas", VILLERGAS_FULL, created=NOW - timedelta(days=1),
                 booked_start=NOW, booked_via="manual")
        s.add_all([BookingWatch(event_id=a, state="waiting"), BookingWatch(event_id=b, state="live")])
    assert await scout.dedupe_events() == 1
    with db.session() as s:
        w = s.query(BookingWatch).one()
        assert w.event_id == a and w.state == "waiting"
        assert s.get(Event, a).booked_via == "manual"


# ---------------------------------------------------------------- cards
def fake_bot():
    bot = MagicMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_update_edits_pending_card_in_place(tmp_path):
    scout, db, settings = make_world(tmp_path)
    bot = fake_bot()
    tools = AdminTools(db, lambda: bot, 42, TZ)
    tools.register_hooks()
    l2 = post(scout, db, "homemarket_lmh", "L2", at(5), "l2", ParseResult(kind=ItemKind.new_event, events=[
        ev("Home Market — warehouse in Cobo Calleja", dates=WEEKEND, area="Cobo Calleja (Fuenlabrada)")]))
    l3 = post(scout, db, "homemarket_lmh", "L3", at(6), "l3", ParseResult(kind=ItemKind.update, events=[
        ev("Home Market Cobo Calleja", dates=WEEKEND, address="Pol. Ind. Cobo Calleja, C. Villafranca del Bierzo 56, Fuenlabrada",
           booking=BookingMethod.eventbrite, opens="2026-11-08T20:00:00")]))
    await run(scout, l2, at(5))
    with db.session() as s:
        s.query(Event).one().telegram_message_id = 77
    await run(scout, l3, at(6))
    assert len(scout.notifier.new) == 1 and scout.notifier.updates == []  # no second card, no update message
    kw = bot.edit_message_caption.await_args.kwargs
    assert kw["message_id"] == 77 and "Villafranca del Bierzo" in kw["caption"] and "Updated" in kw["caption"]
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data.startswith("d:")


@pytest.mark.asyncio
async def test_text_card_fallback_and_answered_cards_untouched(tmp_path):
    scout, db, _ = make_world(tmp_path)
    bot = fake_bot()
    bot.edit_message_caption.side_effect = BadRequest("Bad Request: there is no caption in the message to edit")
    tools = AdminTools(db, lambda: bot, 42, TZ)
    db.add_account("my_market_mad")
    with db.session() as s:
        acc = s.query(Account).one()
        pend = _add(s, acc, "Pending market", telegram_message_id=10)
        yes = _add(s, acc, "Going market", telegram_message_id=11, user_decision="yes")
    await tools.on_event_updated(pend, changes=["address: X"])
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 10
    bot.edit_message_text.reset_mock()
    await tools.on_event_updated(yes, changes=["address: X"])
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_merged_duplicate_card_loses_its_buttons(tmp_path):
    scout, db, _ = make_world(tmp_path)
    bot = fake_bot()
    tools = AdminTools(db, lambda: bot, 42, TZ)
    db.add_account("my_market_mad")
    with db.session() as s:
        acc = s.query(Account).one()
        a = _add(s, acc, "Keep me", telegram_message_id=1, user_decision="yes")
        b = _add(s, acc, "Drop me", telegram_message_id=2)
    await tools.on_events_merged(a, merged_id=b)
    kw = bot.edit_message_caption.await_args_list[0].kwargs
    assert kw["message_id"] == 2 and kw["reply_markup"] is None and "merged" in kw["caption"]
    assert len(bot.edit_message_caption.await_args_list) == 1  # kept card answered: left alone


# ---------------------------------------------------------------- /merge
@pytest.mark.asyncio
async def test_merge_command_confirm_flow(tmp_path):
    scout, db, settings = make_world(tmp_path)
    bot = fake_bot()
    tools = AdminTools(db, lambda: bot, 42, TZ)
    seen: list[tuple[int, int]] = []

    async def on_merged(keep_id, merged_id):
        seen.append((keep_id, merged_id))

    hooks.on("events_merged", on_merged)
    db.add_account("homemarket_lmh")
    with db.session() as s:
        acc = s.query(Account).one()
        a = _add(s, acc, "Home Market at Cobo Calleja", "Polígono industrial Cobo Calleja")
        b = _add(s, acc, "Home Market — Fuenlabrada", "C. Villafranca del Bierzo, 56", user_decision="maybe")

    upd = MagicMock()
    upd.effective_message.reply_text = AsyncMock()
    await tools.cmd_merge(upd, SimpleNamespace(args=["1"]))
    assert "Usage" in upd.effective_message.reply_text.await_args.args[0]
    await tools.cmd_merge(upd, SimpleNamespace(args=[str(a), str(b)]))
    text = upd.effective_message.reply_text.await_args.args[0]
    kb = upd.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert f"#{a}" in text and f"#{b}" in text
    data = kb.inline_keyboard[0][0].callback_data
    assert data == f"mg:ok:{a}:{b}"

    def cb(data, chat=42):
        q = MagicMock()
        q.data = data
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        u = MagicMock()
        u.callback_query = q
        u.effective_chat = SimpleNamespace(id=chat)
        return u, q

    u, q = cb(data, chat=999)  # stranger: ignored
    await tools.on_callback(u, None)
    assert live(db).__len__() == 2
    u, q = cb(data)
    await tools.on_callback(u, None)
    assert seen == [(a, b)]
    assert "Merged" in q.edit_message_text.await_args.args[0]
    (kept,) = live(db)
    assert kept.id == a and kept.user_decision == "maybe"
    assert kept.address == "Polígono industrial Cobo Calleja, C. Villafranca del Bierzo, 56"
    u, q = cb(data)  # second tap
    await tools.on_callback(u, None)
    assert "already merged" in q.edit_message_text.await_args.args[0]
    assert seen == [(a, b)]


def test_register_admin_wires_command_and_prefix(tmp_path):
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler

    _, db, settings = make_world(tmp_path)
    app = Application.builder().token("1:x").build()
    register_admin(app, settings, db, None)
    handlers = app.handlers[0]
    assert any(isinstance(h, CommandHandler) and "merge" in h.commands for h in handlers)
    assert any(isinstance(h, CallbackQueryHandler) and h.pattern.match("mg:ok:1:2") for h in handlers)

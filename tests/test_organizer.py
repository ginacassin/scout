"""Reposts: the market's organiser is found and linked next to the account that reposted it."""

from __future__ import annotations

import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from scout.bot import templates as T
from scout.db import Account, Database, Event
from scout.gcal import describe
from scout.pipeline import Candidate, organizer_for
from scout.sources.apify import parse_story

TZ = ZoneInfo("Europe/Madrid")


def raw_story(owner="homemarket_lmh", owner_id="64726417075", **extra):
    return {"id": "4025439411194938480", "username": owner, "owner": {"id": owner_id, "username": owner},
            "taken_at": 1762600000, "image_versions": {"items": [{"url": "https://x/img.jpg"}]}, **extra}


def test_reshared_story_names_its_author():
    # shape of a real story from 10 Nov 2026
    st = parse_story(raw_story(reel_mentions=[
        {"display_type": "mention_reshare", "user": {"username": "clarapvillalon"}},
        {"display_type": "mention_username", "user": {"username": "homemarket_lmh"}},
        {"display_type": "mention_username", "user": {"username": "elperalinrd"}},
    ]))
    assert st.reshared_from == "clarapvillalon"
    assert st.mentions == ["elperalinrd"]  # never the account itself


def test_reshared_post_links_the_original_but_not_own_posts():
    other = parse_story(raw_story(owner="mercadillosmad", owner_id="111", story_feed_media=[
        {"media_code": "Dfds2sYo22u", "media_compound_str": "4025570914495327662_21071847018"}]))
    assert other.shared_post_url == "https://www.instagram.com/p/Dfds2sYo22u/"
    own = parse_story(raw_story(owner="arquitecturadelorden", owner_id="28248235", story_feed_media=[
        {"media_code": "DfemIe5DsoH", "media_compound_str": "4025822825608563207_28248235"}]))
    assert own.shared_post_url is None


def cand(**kw):
    acc = SimpleNamespace(handle="mercadillosmad", id=1, last_bio=None, bio_links=[])
    return Candidate(account=acc, kind="story", ig_id="story:1", url="https://ig/story/1", posted_at=None,
                     caption="", image_urls=[], **kw)


def test_organizer_for():
    p = SimpleNamespace(organizer="@AlmaMarket_Home")
    assert organizer_for(p, cand(shared_post_url="https://ig/p/X/")) == ("almamarket_home", "https://ig/p/X/")
    assert organizer_for(SimpleNamespace(organizer=None), cand(reshared_from="clarapvillalon")) == ("clarapvillalon", None)
    assert organizer_for(SimpleNamespace(organizer="mercadillosmad"), cand()) == (None, None)  # its own market
    assert organizer_for(SimpleNamespace(organizer=None), cand(shared_post_url="https://ig/p/X/")) == (None, None)


def ev(**kw):
    e = Event(id=7, account_id=1, title="Alma Market — Chamberí", dates=json.dumps(["2026-11-14"]),
              source_url="https://www.instagram.com/stories/mercadillosmad/1/", **kw)
    return e


def test_card_buttons_and_calendar_show_both():
    e = ev(organizer="almamarket_home", organizer_url="https://www.instagram.com/p/Dfds2sYo22u/")
    cap = T.event_caption(e, "mercadillosmad", TZ)
    assert "<i>by @almamarket_home · via @mercadillosmad</i>" in cap
    row = T.decision_keyboard(e).inline_keyboard[1]
    assert [(b.text, b.url) for b in row] == [
        ("📷 @almamarket_home", "https://www.instagram.com/p/Dfds2sYo22u/"),
        ("🔁 Repost", "https://www.instagram.com/stories/mercadillosmad/1/")]
    d = describe(e, "mercadillosmad", TZ)
    assert "📸 Organiser @almamarket_home: https://www.instagram.com/almamarket_home/" in d
    assert "Their post: https://www.instagram.com/p/Dfds2sYo22u/" in d
    assert "🔁 Seen via @mercadillosmad: https://www.instagram.com/stories/mercadillosmad/1/" in d
    assert "(by @almamarket_home · via @mercadillosmad)" in T.short_line(e, "mercadillosmad")


def test_own_market_looks_as_before():
    e = ev()
    assert "<i>@mercadillosmad</i>" in T.event_caption(e, "mercadillosmad", TZ)
    assert T.decision_keyboard(e).inline_keyboard[1][0].text == "📷 Instagram"
    assert describe(e, "mercadillosmad", TZ).startswith("📸 Instagram @mercadillosmad: https://")


def test_startup_drops_organizer_equal_to_the_poster(tmp_path):
    path = tmp_path / "s.db"
    db = Database(str(path))
    with db.session() as s:
        a = Account(handle="almamarket_home")
        s.add(a)
        s.flush()
        s.add(Event(account_id=a.id, title="A", dates="[]", organizer="AlmaMarket_Home"))
        s.add(Event(account_id=a.id, title="B", dates="[]", organizer="clarapvillalon"))
    db2 = Database(str(path))
    with db2.session() as s:
        assert sorted((e.title, e.organizer) for e in s.query(Event)) == [("A", None), ("B", "clarapvillalon")]

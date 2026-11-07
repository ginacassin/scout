from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scout.bot.handlers import parse_handle
from scout.bot.templates import event_caption, schedule_lines
from scout.db import Event
from scout.sources.apify import parse_profile, parse_story

TZ = ZoneInfo("Europe/Madrid")


def test_parse_handle_variants():
    assert parse_handle("@almamarket_home") == "almamarket_home"
    assert parse_handle("https://www.instagram.com/thecircularmarket.es/") == "thecircularmarket.es"
    assert parse_handle("https://instagram.com/My_Market_Mad?igsh=abc") == "my_market_mad"
    assert parse_handle("https://www.instagram.com/p/DfZkLmIDRJk/") is None
    assert parse_handle("not a handle!") is None


def test_parse_profile_from_apify_shape():
    raw = {
        "username": "almamarket_home", "fullName": "Alma Market Home", "biography": "Organizamos Markets…",
        "externalUrls": [{"url": "https://marketrafaelcalvo.eventbrite.es"}, {"url": "http://www.almamarkethome.com"}],
        "latestPosts": [
            {"shortCode": "DfZOf_Olnpd", "type": "Sidecar", "caption": "Ya podéis reservar…", "timestamp": "2026-11-06T09:00:00.000Z",
             "ownerUsername": "almamarket_home", "isPinned": False, "locationName": "Barrio De Chamberí, Madrid",
             "childPosts": [{"displayUrl": "https://cdn/1.jpg"}, {"displayUrl": "https://cdn/2.jpg"}]},
            {"shortCode": "OLD", "type": "Video", "timestamp": "2025-01-01T00:00:00Z", "isPinned": True, "displayUrl": "https://cdn/v.jpg",
             "coauthorProducers": [{"username": "mercadillosencasa"}]},
        ],
    }
    snap = parse_profile(raw)
    assert snap.bio_links[0] == "https://marketrafaelcalvo.eventbrite.es"
    p0, p1 = snap.posts
    assert p0.media_type == "carousel" and p0.image_urls == ["https://cdn/1.jpg", "https://cdn/2.jpg"]
    assert p0.posted_at == datetime(2026, 11, 6, 9, tzinfo=timezone.utc)
    assert p1.is_pinned and p1.coauthors == ["mercadillosencasa"]


def test_parse_story_link_sticker():
    raw = {"id": "3500_123", "username": "My_Market_Mad", "taken_at": 1762500000,
           "image_versions": {"items": [{"url": "https://cdn/s.jpg"}]},
           "story_link_stickers": [{"story_link": {"url": "https://www.eventbrite.es/e/2004504034527"}}]}
    st = parse_story(raw)
    assert st.owner == "my_market_mad" and st.story_id == "3500"
    assert st.link_urls == ["https://www.eventbrite.es/e/2004504034527"]


def _event(**kw):
    e = Event(id=1, account_id=1, title="Mercadillo Clara del Rey", summary="Dishes, furniture, lamps.",
              booking_method="eventbrite", booking_scope="Fri 13 10:00–13:00", user_decision="pending", **kw)
    e.date_list = [datetime(2026, 11, d).date() for d in (13, 14, 15)]
    e.session_list = [
        {"date": "2026-11-13", "start": "10:00", "end": "13:00", "booking_required": True},
        {"date": "2026-11-13", "start": "13:00", "end": "20:00", "booking_required": False},
        {"date": "2026-11-14", "start": "11:00", "end": "15:00", "booking_required": False},
    ]
    return e


def test_caption_shows_split_sessions_and_booking():
    e = _event(address="C/ Clara del Rey 14, 6.º A, 28002 Madrid",
               booking_opens_at=datetime(2026, 11, 10, 9, tzinfo=timezone.utc))
    text = event_caption(e, "thecircularmarket.es", TZ, header="🆕 New market")
    assert "Fri 13 Nov · 10:00–13:00 🎟, 13:00–20:00" in text
    assert "Booking opens Tue 10 Nov 10:00" in text  # 09:00 UTC = 10:00 Madrid
    assert len(text) <= 1024


def test_caption_address_pending_and_schedule_without_sessions():
    e = _event(area="Chamberí (Almagro)")
    assert "exact address not announced yet" in event_caption(e, "almamarket_home", TZ)
    e.session_list = []
    assert schedule_lines(e) == ["Fri 13 Nov · Sat 14 Nov · Sun 15 Nov"]

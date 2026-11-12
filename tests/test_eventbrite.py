"""M3: Eventbrite resolver, ticket-status client and booking watcher (no network)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from scout import hooks
from scout.bot import templates as T
from scout.config import Settings
from scout.db import Account, BookingWatch, Database, Event
from scout.eventbrite.client import EventbriteClient
from scout.eventbrite.resolver import Resolver, event_id_from_url, organizer_id_from_url
from scout.eventbrite.watcher import BookingWatcher

TZ = ZoneInfo("Europe/Madrid")


def mad(month, day, hour=10, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def ld_page(name, start, end, availability=None, valid_from=None, price="0", url=None):
    offer = {"@type": "Offer", "price": price, "priceCurrency": "EUR"}
    if availability:
        offer["availability"] = f"https://schema.org/{availability}"
    if valid_from:
        offer["validFrom"] = valid_from
    ev = {"@context": "https://schema.org", "@type": "SocialEvent", "name": name, "startDate": start,
          "endDate": end, "offers": [offer]}
    if url:
        ev["url"] = url
    return f'<html><head><script type="application/ld+json">{json.dumps(ev)}</script></head><body></body></html>'


class Router:
    """httpx.MockTransport handler: maps (host+path) to responses and records every request."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []

    def add(self, url, resp):
        self.routes[url] = resp

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        self.calls.append(key)
        resp = self.routes.get(key)
        if callable(resp):
            resp = resp(request)
        if resp is None:
            return httpx.Response(404, text="not found")
        if isinstance(resp, httpx.Response):
            return resp
        if isinstance(resp, (dict, list)):
            return httpx.Response(200, json=resp)
        return httpx.Response(200, text=resp, headers={"content-type": "text/html"})


def make_client(router, token=""):
    http = httpx.AsyncClient(transport=httpx.MockTransport(router), follow_redirects=True)
    return EventbriteClient(token, http=http)


API = "https://www.eventbriteapi.com/v3"


# ------------------------------------------------------------------ resolver
def test_url_parsing():
    assert event_id_from_url("https://www.eventbrite.es/e/entradas-puertas-abiertas-naluz-2003776483402?aff=x") == "2003776483402"
    assert event_id_from_url("https://www.eventbrite.es/e/2004504034527") == "2004504034527"
    assert event_id_from_url("https://example.com/e/2004504034527") is None
    assert organizer_id_from_url("https://www.eventbrite.com/o/46888144463") == "46888144463"
    assert organizer_id_from_url("https://www.eventbrite.es/o/clara-del-rey-46888144463") == "46888144463"


async def test_vanity_subdomain_redirects_to_event():
    r = Router()
    target = "https://www.eventbrite.es/e/entradas-puertas-abiertas-naluz-2003776483402?aff=ebdsoporgprofile"
    r.add("https://puertasabiertasnaluz.eventbrite.es/", httpx.Response(302, headers={"location": target}))
    r.add("https://www.eventbrite.es/e/entradas-puertas-abiertas-naluz-2003776483402",
          ld_page("Puertas Abiertas Naluz", "2026-10-29T12:00:00+01:00", "2026-10-31T20:00:00+01:00", "PreOrder"))
    res = await Resolver(make_client(r)).resolve(
        [date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31)], None, ["https://puertasabiertasnaluz.eventbrite.es"])
    assert res.eventbrite_id == "2003776483402"
    assert res.url == "https://www.eventbrite.es/e/entradas-puertas-abiertas-naluz-2003776483402"  # no ?aff


async def test_organizer_page_picks_event_matching_dates():
    r = Router()
    r.add(f"{API}/organizers/46888144463/events/", {"events": [
        {"id": "111111111", "url": "https://www.eventbrite.com/e/old-111111111",
         "start": {"local": "2026-11-06T10:00:00", "utc": "2026-11-06T09:00:00Z"},
         "end": {"local": "2026-11-08T15:00:00", "utc": "2026-11-08T14:00:00Z"}, "name": {"text": "Claudio Coello"}},
        {"id": "222222222", "url": "https://www.eventbrite.com/e/clara-del-rey-222222222",
         "start": {"local": "2026-11-13T10:00:00", "utc": "2026-11-13T09:00:00Z"},
         "end": {"local": "2026-11-13T13:00:00", "utc": "2026-11-13T12:00:00Z"}, "name": {"text": "Clara del Rey"}},
    ]})
    res = await Resolver(make_client(r, token="tok")).resolve(
        [date(2026, 11, 13), date(2026, 11, 14), date(2026, 11, 15)], None, ["https://www.eventbrite.com/o/46888144463"])
    assert res.eventbrite_id == "222222222"


async def test_linktree_followed_to_eventbrite():
    r = Router()
    r.add("https://linktr.ee/lamoralejahome", """<html><a href="https://www.instagram.com/homemarket_lmh">IG</a>
        <a href="https://www.eventbrite.es/e/entradas-home-market-nave-2004999000111?aff=linktree">Entradas viernes</a>
        <script id="__NEXT_DATA__">{"links":[{"url":"https:\\/\\/wa.me\\/34600000000"}]}</script></html>""")
    r.add("https://www.eventbriteapi.com/v3/events/2004999000111/", httpx.Response(403))
    r.add("https://www.eventbrite.es/e/entradas-home-market-nave-2004999000111",
          ld_page("Home Market Nave", "2026-11-13T10:00:00+01:00", "2026-11-13T14:00:00+01:00", "PreOrder",
                  "2026-11-08T20:00:00+01:00"))
    res = await Resolver(make_client(r, token="tok")).resolve(
        [date(2026, 11, 13), date(2026, 11, 14), date(2026, 11, 15)], None, ["https://linktr.ee/lamoralejahome"])
    assert res.eventbrite_id == "2004999000111"


async def test_bio_still_pointing_at_previous_market_is_rejected():
    r = Router()
    r.add(f"{API}/events/2004504034527/", {"id": "2004504034527", "url": "https://www.eventbrite.es/e/2004504034527",
                                          "start": {"utc": "2026-10-23T09:00:00Z"}, "end": {"utc": "2026-10-25T14:00:00Z"}})
    res = await Resolver(make_client(r, token="tok")).resolve(
        [date(2026, 11, 13)], None, ["https://www.eventbrite.es/e/2004504034527"])
    assert res is None


async def test_direct_event_url_without_token_uses_page_dates():
    r = Router()
    r.add("https://www.eventbrite.es/e/2004504034527",
          ld_page("Market Martínez Villergas", "2026-11-13T10:00:00+01:00", "2026-11-13T20:00:00+01:00", "PreOrder"))
    res = await Resolver(make_client(r)).resolve([date(2026, 11, 13), date(2026, 11, 14)], None,
                                                 ["https://www.eventbrite.es/e/2004504034527", "http://www.x.com"])
    assert res.eventbrite_id == "2004504034527"
    assert not any("eventbriteapi" in c for c in r.calls)


# -------------------------------------------------------------------- client
def api_event(sales_status="not_yet_on_sale", has_available=False, sold_out=False):
    return {"id": "9", "url": "https://www.eventbrite.es/e/x-9", "is_free": True,
            "start": {"utc": "2026-11-13T09:00:00Z"}, "end": {"utc": "2026-11-13T12:00:00Z"},
            "ticket_availability": {"has_available_tickets": has_available, "is_sold_out": sold_out},
            "event_sales_status": {"sales_status": sales_status,
                                   "start_sales_date": {"timezone": "Europe/Madrid", "local": "2026-11-10T10:00:00",
                                                        "utc": "2026-11-10T09:00:00Z"}}}


def classes(*statuses, start="2026-11-10T09:00:00Z"):
    return {"ticket_classes": [{"on_sale_status": s, "free": True, "sales_start": start,
                                "sales_end": "2026-11-13T08:00:00Z"} for s in statuses]}


async def test_api_not_yet_on_sale():
    r = Router()
    r.add(f"{API}/events/9/", api_event())
    r.add(f"{API}/events/9/ticket_classes/", classes("NOT_YET_ON_SALE"))
    st = await make_client(r, "tok").status("9")
    assert st.state == "not_open" and st.is_free is True
    assert st.sales_start == mad(11, 10, 10)


async def test_api_available_and_sold_out_trust_ticket_classes():
    r = Router()
    r.add(f"{API}/events/9/", api_event("on_sale", has_available=True))
    r.add(f"{API}/events/9/ticket_classes/", classes("AVAILABLE"))
    assert (await make_client(r, "tok").status("9")).state == "open"
    # has_available_tickets still says True, but the only class is sold out → trust on_sale_status
    r.add(f"{API}/events/9/ticket_classes/", classes("SOLD_OUT"))
    assert (await make_client(r, "tok").status("9")).state == "sold_out"


async def test_jsonld_fallback_states():
    url = "https://www.eventbrite.es/e/x-9"
    for avail, want in (("PreOrder", "not_open"), ("InStock", "open"), ("SoldOut", "sold_out")):
        r = Router()
        r.add(url, ld_page("X", "2026-11-13T10:00:00+01:00", "2026-11-13T13:00:00+01:00", avail,
                           "2026-11-10T10:00:00+01:00"))
        st = await make_client(r).status("9", url)
        assert st.state == want and st.is_free is True and st.source == "page"
        assert st.sales_start == mad(11, 10, 10)


async def test_api_403_falls_back_to_page():
    url = "https://www.eventbrite.es/e/x-9"
    r = Router()
    r.add(f"{API}/events/9/", httpx.Response(403))
    r.add(url, ld_page("X", "2026-11-13T10:00:00+01:00", "2026-11-13T13:00:00+01:00", "InStock"))
    st = await make_client(r, "tok").status("9", url)
    assert st.state == "open" and st.source == "page"


async def test_transport_error_gives_unknown():
    def boom(request):
        raise httpx.ConnectError("down")
    st = await make_client(boom, "tok").status("9")
    assert st.state == "unknown"


# ------------------------------------------------------------------- watcher
class FakeJob:
    def __init__(self, kind, callback, name, data, when=None, interval=None, first=None, last=None):
        self.kind, self.callback, self.name, self.data = kind, callback, name, data
        self.when, self.interval, self.first, self.last = when, interval, first, last
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs: list[FakeJob] = []

    def run_once(self, callback, when, name=None, data=None, **kw):
        self.jobs.append(FakeJob("once", callback, name, data, when=when))

    def run_repeating(self, callback, interval, first=None, last=None, name=None, data=None, **kw):
        self.jobs.append(FakeJob("repeating", callback, name, data, interval=interval, first=first, last=last))

    def run_daily(self, callback, time, name=None, **kw):
        self.jobs.append(FakeJob("daily", callback, name, None))

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]

    def live(self, event_id):
        return sorted(j.name.split(":")[2] for j in self.jobs if not j.removed and j.name.startswith(f"eb:{event_id}:"))

    def get(self, name):
        return self.get_jobs_by_name(name)[0]


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})

    async def edit_message_reply_markup(self, *a, **kw):
        pass


class FakeApp:
    def __init__(self):
        self.job_queue = FakeJobQueue()
        self.bot = FakeBot()
        self.bot_data = {}
        self.handlers = []

    def add_handler(self, h):
        self.handlers.append(h)


class Ctx:
    def __init__(self, job):
        self.job = job


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeStatusClient:
    """Stands in for EventbriteClient in watcher tests."""

    token = "tok"

    def __init__(self):
        from scout.eventbrite.client import TicketStatus
        self.TS = TicketStatus
        self.state = "not_open"
        self.sales_start = None
        self.calls = 0

    async def status(self, eid, url=None):
        self.calls += 1
        return self.TS(True, self.sales_start, self.state, url or f"https://www.eventbrite.es/e/{eid}")


class FakeResolver:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    async def resolve(self, dates, booking_url, bio):
        self.calls += 1
        return self.result


@pytest.fixture
def wenv(tmp_path: Path):
    hooks.clear()
    before = list(T.EXTRA_ANSWERED_BUTTONS)
    settings = Settings(
        bot_token="x", webhook_url="", port=8000, allowed_chat_id=1, gemini_api_key="x", gemini_model="m",
        ig_provider="apify", apify_token="x", eventbrite_token="tok", gcal_sa_json="", gcal_calendar_id="",
        tz=TZ, data_dir=tmp_path,
    )
    db = Database(":memory:")
    app = FakeApp()
    clock = Clock(mad(11, 9, 12))  # Mon 9 Nov 12:00
    client = FakeStatusClient()
    w = BookingWatcher(settings, db, app, client=client, now=clock)
    from scout.eventbrite.resolver import Resolved
    w.resolver = FakeResolver(Resolved("2222", "https://www.eventbrite.com/e/clara-del-rey-2222"))
    yield w, db, app, clock, client
    hooks.clear()
    T.EXTRA_ANSWERED_BUTTONS[:] = before


def add_event(db, decision="yes", opens=None, method="eventbrite", bio=("https://www.eventbrite.com/o/46888144463",),
              dates=("2026-11-13", "2026-11-14", "2026-11-15"), link_in_bio=True):
    with db.session() as s:
        acc = Account(handle="thecircularmarket.es", last_bio_links=json.dumps(list(bio)))
        s.add(acc)
        s.flush()
        ev = Event(account_id=acc.id, title="Mercadillo Clara del Rey", dates=json.dumps(list(dates)),
                   booking_method=method, booking_opens_at=opens, booking_scope="Fri 13 10:00–13:00",
                   link_in_bio=link_in_bio, user_decision=decision, status="announced")
        s.add(ev)
        s.flush()
        return ev.id


def texts(app):
    return [m["text"].split("\n")[0] for m in app.bot.sent]


async def test_known_opening_schedules_pings_and_dedupes(wenv):
    w, db, app, clock, client = wenv
    eid = add_event(db, opens=mad(11, 10, 10))
    client.sales_start = mad(11, 10, 10)
    await w.refresh(eid, reason="decision", force=True)
    jq = app.job_queue
    assert jq.live(eid) == ["after", "fast", "t-1", "t-15", "t0"]
    assert jq.get(f"eb:{eid}:t-15").when == mad(11, 10, 9, 45)
    fast = jq.get(f"eb:{eid}:fast")
    assert fast.interval == 20 and fast.last == mad(11, 10, 10, 10)

    clock.t = mad(11, 10, 9, 45)
    job = jq.get(f"eb:{eid}:t-15")
    await job.callback(Ctx(job))
    await job.callback(Ctx(job))  # fired twice (e.g. restart race) → one message
    assert texts(app) == ["⏳ Booking opens in 15 min — be ready"]
    msg = app.bot.sent[0]
    assert "Mercadillo Clara del Rey" in msg["text"] and "Fri 13 10:00–13:00" in msg["text"]
    buttons = [b for row in msg["reply_markup"].inline_keyboard for b in row]
    assert buttons[0].text == "🎟 Open Eventbrite" and buttons[0].url == "https://www.eventbrite.com/e/clara-del-rey-2222"
    assert buttons[1].url == "https://www.eventbrite.es/checkout-external?eid=2222"
    assert json.loads(w.get_watch(eid).pings_sent) == ["t-15"]

    # Restart: a fresh watcher rebuilds jobs from the row and skips what was already sent.
    app2 = FakeApp()
    w2 = BookingWatcher(w.settings, db, app2, client=client, now=clock)
    await w2.startup()
    assert app2.job_queue.live(eid) == ["after", "boot", "fast", "t-1", "t0"]


async def test_early_opening_alerts_then_sold_out(wenv):
    w, db, app, clock, client = wenv
    eid = add_event(db, opens=mad(11, 10, 10))
    client.sales_start = mad(11, 10, 10)
    await w.refresh(eid, reason="decision", force=True)
    jq = app.job_queue

    clock.t = mad(11, 10, 9, 57)  # opened three minutes early
    client.state = "open"
    fast = jq.get(f"eb:{eid}:fast")
    await fast.callback(Ctx(fast))
    await fast.callback(Ctx(jq.get(f"eb:{eid}:fast")))
    assert texts(app) == ["🚨 Tickets are live"]
    assert w.get_watch(eid).state == "live"

    clock.t = mad(11, 10, 10)
    t0 = jq.get(f"eb:{eid}:t0")
    await t0.callback(Ctx(t0))  # already told it's live → no "open NOW"
    assert len(app.bot.sent) == 1

    clock.t = mad(11, 10, 10, 1)
    client.state = "sold_out"
    fast = jq.get(f"eb:{eid}:fast")
    await fast.callback(Ctx(fast))
    await fast.callback(Ctx(jq.get(f"eb:{eid}:fast")))
    assert texts(app) == ["🚨 Tickets are live", "⛔ Sold out"]

    # After the window nothing keeps polling a sold-out market.
    clock.t = mad(11, 10, 10, 11)
    after = jq.get(f"eb:{eid}:after")
    await after.callback(Ctx(after))
    assert jq.live(eid) == []


async def test_eventbrite_time_wins_over_instagram(wenv):
    w, db, app, clock, client = wenv
    eid = add_event(db, opens=mad(11, 10, 10))
    client.sales_start = mad(11, 10, 9, 30)
    watch = await w.refresh(eid, reason="decision", force=True)
    assert watch.opens_at == mad(11, 10, 9, 30)
    assert app.job_queue.get(f"eb:{eid}:t0").when == mad(11, 10, 9, 30)


async def test_unknown_opening_polls_until_live(wenv):
    w, db, app, clock, client = wenv
    eid = add_event(db, opens=None)
    client.state, client.sales_start = "unknown", None
    await w.refresh(eid, reason="decision", force=True)
    jq = app.job_queue
    assert jq.live(eid) == ["slow"]
    slow = jq.get(f"eb:{eid}:slow")
    assert slow.interval == 600

    await slow.callback(Ctx(slow))
    assert app.bot.sent == [] and jq.live(eid) == ["slow"]

    client.state = "open"
    clock.t += timedelta(minutes=10)
    await slow.callback(Ctx(slow))
    assert texts(app) == ["🚨 Tickets are live"]
    assert jq.live(eid) == []  # live → stop polling


async def test_link_not_found_yet_keeps_resolving(wenv):
    w, db, app, clock, client = wenv
    w.resolver.result = None
    eid = add_event(db, opens=mad(11, 10, 10))
    await w.refresh(eid, reason="decision", force=True)
    jq = app.job_queue
    assert "slow" in jq.live(eid) and "t-15" in jq.live(eid)
    assert w.get_watch(eid).state == "resolving"

    clock.t = mad(11, 10, 9, 45)
    job = jq.get(f"eb:{eid}:t-15")
    await job.callback(Ctx(job))
    msg = app.bot.sent[0]
    assert "link not found yet" in msg["text"]
    assert msg["reply_markup"].inline_keyboard[0][0].url == "https://www.eventbrite.com/o/46888144463"


async def test_pending_event_gets_watch_but_no_pings(wenv):
    w, db, app, clock, client = wenv
    w.register()
    eid = add_event(db, decision="pending", opens=mad(11, 10, 10))
    await hooks.emit("event_created", eid, kind="new_event")
    import asyncio
    await asyncio.gather(*w._tasks)
    watch = w.get_watch(eid)
    assert watch is not None and watch.eventbrite_id == "2222"
    assert app.job_queue.live(eid) == [] and app.bot.sent == []

    # Tapping ✅ starts the reminders (the bot stores the decision, then emits).
    with db.session() as s:
        s.get(Event, eid).user_decision = "yes"
    await hooks.emit("decision_changed", eid, old="pending", new="yes")
    await asyncio.gather(*w._tasks)
    assert app.job_queue.live(eid) == ["after", "fast", "t-1", "t-15", "t0"]

    with db.session() as s:
        s.get(Event, eid).user_decision = "no"
    await hooks.emit("decision_changed", eid, old="yes", new="no")
    await asyncio.gather(*w._tasks)
    assert app.job_queue.live(eid) == []


async def test_free_entry_market_is_not_watched(wenv):
    w, db, app, clock, client = wenv
    eid = add_event(db, method="free_entry", link_in_bio=False)
    await w.refresh(eid, reason="created", force=True)
    assert w.get_watch(eid) is None


async def test_cancellations_post_checks_immediately_but_stays_quiet(wenv):
    """Released tickets only alert through /hunt; the watcher just refreshes the state."""
    w, db, app, clock, client = wenv
    w.register()
    eid = add_event(db, opens=mad(11, 6, 10))
    client.state = "sold_out"
    with db.session() as s:
        s.add(BookingWatch(event_id=eid, eventbrite_id="2222", state="sold_out", opens_at=mad(11, 6, 10),
                           pings_sent=json.dumps(["live0", "soldout1"])))
    client.state = "open"
    await hooks.emit("event_updated", eid, changes=["tickets available again (cancellations)"])
    import asyncio
    await asyncio.gather(*w._tasks)
    assert texts(app) == []
    assert "cxl" in app.job_queue.live(eid)
    assert w.get_watch(eid).state == "live"


async def test_toggle_buttons_and_bookings_list(wenv):
    w, db, app, clock, client = wenv
    w.register()
    assert w.card_buttons in T.EXTRA_ANSWERED_BUTTONS
    eid = add_event(db, opens=mad(11, 10, 10))
    client.sales_start = mad(11, 10, 10)
    await w.refresh(eid, reason="decision", force=True)
    with db.session() as s:
        ev = s.get(Event, eid)
    row = w.card_buttons(ev)
    assert row[0].text == "🔔 Booking reminders: on" and row[0].callback_data == f"eb:toggle:{eid}"
    assert row[1].text == "🎟 Eventbrite"
    kb = T.answered_keyboard(ev)
    assert any(b.callback_data == f"eb:toggle:{eid}" for r in kb.inline_keyboard for b in r)

    assert await w.toggle(eid) is False
    assert app.job_queue.live(eid) == []
    assert w.card_buttons(ev)[0].text == "🔔 Booking reminders: off"

    text = w.bookings_text()
    assert "Mercadillo Clara del Rey" in text and "Tue 10 Nov 10:00" in text and "🔕 off" in text
    assert "not open yet" in text


async def test_rejected_candidates_logged_once(caplog):
    import logging
    from datetime import date as _d, datetime as _dt, timezone as _tz
    from scout.eventbrite.resolver import EBEvent, Resolver

    r = Resolver(client=None)

    async def expand(link, depth):
        return [EBEvent(id="1991930752502", url=link, name="Other market",
                        start=_dt(2026, 10, 1, 9, tzinfo=_tz.utc), end=_dt(2026, 10, 1, 14, tzinfo=_tz.utc))]

    r._expand = expand
    dates = [_d(2026, 11, 20), _d(2026, 11, 21)]
    with caplog.at_level(logging.INFO, logger="scout.eventbrite.resolver"):
        for _ in range(3):
            assert await r._resolve(dates, ["https://www.eventbrite.es/e/1991930752502"]) is None
    assert sum("don't match dates" in m for m in caplog.messages) == 1

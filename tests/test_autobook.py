"""Scout-side auto-book client: booker calls via httpx.MockTransport, no network."""

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from telegram.ext import Application, CallbackQueryHandler

from scout import hooks
from scout.autobook import AutoBook, BookerClient, eligible, setup_autobook
from scout.bot import templates as T
from scout.config import Settings
from scout.db import Account, BookingWatch, Database, Event, utcnow

CHAT = 42


def make_settings(tmp_path: Path) -> Settings:
    return Settings(bot_token="1:x", webhook_url="", port=8000, allowed_chat_id=CHAT, gemini_api_key="g",
                    gemini_model="m", ig_provider="apify", apify_token="a", eventbrite_token="", gcal_sa_json="",
                    gcal_calendar_id="", tz=ZoneInfo("Europe/Madrid"), data_dir=tmp_path)


class Booker:
    """Fake scout-booker: records requests, answers like the real API."""

    def __init__(self, healthy=True):
        self.healthy = healthy
        self.requests: list[tuple[str, str, dict]] = []
        self.jobs: dict[int, dict] = {}
        self.book_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        assert request.headers.get("X-Booker-Token") == "tok"
        path = request.url.path
        if path == "/health":
            if not self.healthy:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"ok": True, "logged_in": True, "browser": "chromium"})
        if request.method == "POST" and path == "/book":
            if self.book_status != 200:
                return httpx.Response(self.book_status, json={"error": "nope", "job": {"state": "failed:captcha"}})
            job = {**body, "state": "scheduled"}
            self.jobs[body["event_id"]] = job
            return httpx.Response(200, json={"job": job, "changed": True})
        if request.method == "DELETE" and path.startswith("/book/"):
            eid = int(path.rsplit("/", 1)[1])
            job = self.jobs.pop(eid, None)
            if not job:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"job": {**job, "state": "cancelled"}})
        if path == "/jobs":
            return httpx.Response(200, json={"jobs": list(self.jobs.values())})
        return httpx.Response(404, json={})


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = Booker()
    client = BookerClient("http://booker", "tok", transport=httpx.MockTransport(fake.handler))
    ab = AutoBook(make_settings(tmp_path), db, client)
    yield db, fake, ab
    hooks.clear()


def add_event(db, decision="yes", eventbrite_id="123456789", is_free=None, opens_in=timedelta(days=2),
              autobook=False, watch=True) -> int:  # noqa: ANN001
    with db.session() as s:
        acc = s.query(Account).first() or Account(handle="market")
        s.add(acc)
        s.flush()
        ev = Event(account_id=acc.id, title="Mercadillo <Vintage>", user_decision=decision,
                   booking_method="eventbrite", booking_url="https://www.eventbrite.es/e/m-123456789")
        s.add(ev)
        s.flush()
        if watch:
            s.add(BookingWatch(event_id=ev.id, eventbrite_id=eventbrite_id, is_free=is_free, autobook=autobook,
                               opens_at=utcnow() + opens_in if opens_in is not None else None, state="waiting"))
        return ev.id


def get(db, event_id):  # noqa: ANN001
    with db.session() as s:
        return s.get(Event, event_id), s.query(BookingWatch).filter_by(event_id=event_id).one_or_none()


class FakeQuery:
    def __init__(self, data):  # noqa: ANN001
        self.data = data
        self.answers = []
        self.markups = []

    async def answer(self, text=None, show_alert=False):  # noqa: ANN001
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, markup):  # noqa: ANN001
        self.markups.append(markup)


class FakeChat:
    def __init__(self, id_):  # noqa: ANN001
        self.id = id_


class FakeUpdate:
    def __init__(self, data, chat=CHAT):  # noqa: ANN001
        self.callback_query = FakeQuery(data)
        self.effective_chat = FakeChat(chat)


def callbacks(markup) -> list[str]:  # noqa: ANN001
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ---------------------------------------------------------------- visibility
async def test_button_visibility_rules(env):
    db, _, ab = env
    await ab.check_health()
    cases = {
        "ok": add_event(db),
        "free": add_event(db, is_free=True),
        "paid": add_event(db, is_free=False),
        "maybe": add_event(db, decision="maybe"),
        "no_eid": add_event(db, eventbrite_id=None),
        "opened": add_event(db, opens_in=-timedelta(minutes=1)),
        "no_time": add_event(db, opens_in=None),
        "no_watch": add_event(db, watch=False),
        "armed": add_event(db, autobook=True),
    }
    labels = {k: [b.text for b in ab.buttons(get(db, i)[0])] for k, i in cases.items()}
    assert labels["ok"] == ["🤖 Auto-book at opening"]
    assert labels["free"] == ["🤖 Auto-book at opening"]
    assert labels["armed"] == ["🤖 Auto-book: armed — cancel"]
    for k in ("paid", "maybe", "no_eid", "opened", "no_time", "no_watch"):
        assert labels[k] == [], k
    assert ab.buttons(get(db, cases["armed"])[0])[0].callback_data == f"ab:off:{cases['armed']}"


async def test_disabled_when_health_fails(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = Booker(healthy=False)
    ab = AutoBook(make_settings(tmp_path), db, BookerClient("http://booker", "tok", httpx.MockTransport(fake.handler)))
    assert await ab.check_health() is False
    ev_id = add_event(db)
    assert ab.buttons(get(db, ev_id)[0]) == []
    toast, alert = await ab.arm(ev_id)
    assert alert and "unavailable" in toast
    assert not [r for r in fake.requests if r[1] == "/book"]
    # tick doesn't hammer the booker while it's down
    n = len(fake.requests)
    await ab.tick()
    assert len(fake.requests) == n
    fake.healthy = True
    ab._last_health -= 3601
    await ab.tick()
    assert ab.enabled


def test_eligible_pure():
    assert not eligible(None, None)


# ---------------------------------------------------------------- arm/cancel
async def test_arm_via_button_and_cancel(env):
    db, fake, ab = env
    await ab.check_health()
    ev_id = add_event(db)
    T.EXTRA_ANSWERED_BUTTONS.append(ab.buttons)
    try:
        await _arm_then_cancel(db, fake, ab, ev_id)
    finally:
        T.EXTRA_ANSWERED_BUTTONS.remove(ab.buttons)


async def _arm_then_cancel(db, fake, ab, ev_id):  # noqa: ANN001
    upd = FakeUpdate(f"ab:on:{ev_id}")
    await ab.on_callback(upd, None)
    method, path, body = [r for r in fake.requests if r[1] == "/book"][0]
    assert method == "POST" and body["eventbrite_id"] == "123456789" and body["event_id"] == ev_id
    assert body["title"] == "Mercadillo <Vintage>" and body["event_url"].startswith("https://")
    _, w = get(db, ev_id)
    assert w.autobook is True and w.autobook_state == "scheduled"
    assert upd.callback_query.answers[0][1] is False and "Armed" in upd.callback_query.answers[0][0]
    assert f"ab:off:{ev_id}" in callbacks(upd.callback_query.markups[0])

    upd = FakeUpdate(f"ab:off:{ev_id}")
    await ab.on_callback(upd, None)
    assert ("DELETE", f"/book/{ev_id}", {}) in fake.requests
    _, w = get(db, ev_id)
    assert w.autobook is False and w.autobook_state == "cancelled"
    assert f"ab:on:{ev_id}" in callbacks(upd.callback_query.markups[0])


async def test_other_chat_is_ignored(env):
    db, fake, ab = env
    await ab.check_health()
    ev_id = add_event(db)
    upd = FakeUpdate(f"ab:on:{ev_id}", chat=999)
    await ab.on_callback(upd, None)
    assert not [r for r in fake.requests if r[1] == "/book"]
    assert upd.callback_query.answers == [(None, False)]


async def test_arm_refused_when_already_attempted(env):
    db, fake, ab = env
    await ab.check_health()
    fake.book_status = 409
    ev_id = add_event(db)
    toast, alert = await ab.arm(ev_id)
    assert alert and "Already attempted" in toast
    _, w = get(db, ev_id)
    assert w.autobook is False and w.autobook_state == "failed:captcha"


async def test_decision_away_from_yes_cancels(env):
    db, fake, ab = env
    await ab.check_health()
    ev_id = add_event(db)
    await ab.arm(ev_id)
    hooks.on("decision_changed", ab.on_decision)
    await hooks.emit("decision_changed", ev_id, old="yes", new="yes")
    assert not [r for r in fake.requests if r[0] == "DELETE"]
    await hooks.emit("decision_changed", ev_id, old="yes", new="no")
    assert [r for r in fake.requests if r[0] == "DELETE"]
    assert get(db, ev_id)[1].autobook is False


async def test_sync_rearms_when_opening_time_moves(env):
    db, fake, ab = env
    await ab.check_health()
    ev_id = add_event(db)
    await ab.arm(ev_id)
    with db.session() as s:
        w = s.query(BookingWatch).filter_by(event_id=ev_id).one()
        w.opens_at = w.opens_at + timedelta(hours=1)
    posts = len([r for r in fake.requests if r[1] == "/book"])
    await ab.sync()
    assert len([r for r in fake.requests if r[1] == "/book"]) == posts + 1
    await ab.sync()  # now in step: no further re-arm
    assert len([r for r in fake.requests if r[1] == "/book"]) == posts + 1


async def test_setup_registers_and_off_switch(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    db = Database(tmp_path / "t.db")
    monkeypatch.setenv("BOOKER_URL", "off")
    app = Application.builder().token("1:x").build()
    ab = setup_autobook(app, settings, db)
    assert ab.client is None and ab.buttons not in T.EXTRA_ANSWERED_BUTTONS

    monkeypatch.setenv("BOOKER_URL", "http://scout-booker:8100")
    app = Application.builder().token("1:x").build()
    ab = setup_autobook(app, settings, db)
    try:
        assert ab.buttons in T.EXTRA_ANSWERED_BUTTONS
        assert any(isinstance(h, CallbackQueryHandler) for h in app.handlers[0])
        assert app.job_queue.get_jobs_by_name("autobook-sync")
    finally:
        T.EXTRA_ANSWERED_BUTTONS.remove(ab.buttons)
        hooks.clear()

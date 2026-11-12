"""Sneak peeks: item photos become albums with ⭐ buttons; starring adds a picture note."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scout import hooks
from scout.bot import templates as T
from scout.db import Account, Database, Event, Peek
from scout.peeks import Peeks
from scout.pipeline import Candidate
from tests.test_pipeline_real_cases import at, world  # noqa: F401 - fixture


class Bot:
    def __init__(self):
        self.calls = []
        self.n = 500

    def _msg(self):
        self.n += 1
        return SimpleNamespace(message_id=self.n)

    async def send_media_group(self, chat_id, media, **kw):
        for m in media:
            assert m.media.input_file_content, "album photo sent empty"
        self.calls.append(("album", [m.caption for m in media], kw))
        return [self._msg() for _ in media]

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        assert isinstance(photo, bytes) and photo, "photo sent empty"  # the Nov 11 bug
        self.calls.append(("photo", caption, kw))
        return self._msg()

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("text", text, kw))
        return self._msg()

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kw):
        self.calls.append(("edit", message_id, reply_markup))


class App:
    def __init__(self):
        self.bot = Bot()
        self.bot_data = {}
        self.job_queue = None
        self.handlers = {0: []}

    def add_handler(self, h, group=0):
        self.handlers.setdefault(group, []).append(h)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def penv(tmp_path):
    hooks.clear()
    db = Database(":memory:")
    settings = SimpleNamespace(allowed_chat_id=1, data_dir=tmp_path)
    clock = Clock(at(10, 21))
    app = App()
    peeks = Peeks(app, settings, db, now=clock)
    peeks.register()
    with db.session() as s:
        acc = Account(handle="pujar_con_arte")
        s.add(acc)
        s.flush()
        ev = Event(account_id=acc.id, title="House sale on Paseo de la Habana", dates=json.dumps(["2026-11-14"]),
                   user_decision="yes", status="announced", telegram_message_id=77)
        s.add(ev)
        s.flush()
        ids = (acc.id, ev.id)
    yield peeks, db, app, clock, tmp_path, ids
    hooks.clear()


def add_peeks(db, tmp, acc_id, ev_id, n, created):
    with db.session() as s:
        for i in range(n):
            img = tmp / f"p{ev_id}_{i}.jpg"
            img.write_bytes(b"jpg")
            s.add(Peek(account_id=acc_id, event_id=ev_id, ig_id=f"story:{i}", image_path=str(img),
                       items=json.dumps(["rattan armchair", "brass lamp"]) if i % 2 == 0 else "[]",
                       created_at=created))


async def test_waits_for_quiet_then_sends_album_of_ten_and_holds_rest(penv):
    peeks, db, app, clock, tmp, (acc, ev) = penv
    add_peeks(db, tmp, acc, ev, 13, clock.t)
    assert await peeks.tick() == 0  # account still posting
    clock.t += timedelta(minutes=4)
    assert await peeks.tick() == 10
    album = [c for c in app.bot.calls if c[0] == "album"][0]
    assert album[1][0] == "1 · rattan armchair, brass lamp" and album[1][1] == "2"
    assert album[2]["reply_parameters"].message_id == 77 and album[2]["disable_notification"] is True
    text = [c for c in app.bot.calls if c[0] == "text"][0]
    kb = text[2]["reply_markup"].inline_keyboard
    assert [b.text for b in kb[0]] == ["⭐ 1", "⭐ 2", "⭐ 3", "⭐ 4", "⭐ 5"]
    assert kb[-1][0].text == "👀 Show 3 more (3 waiting)"
    with db.session() as s:
        assert sorted(p.state for p in s.query(Peek)) == ["held"] * 3 + ["sent"] * 10
    assert await peeks.tick() == 0  # nothing new
    assert await peeks.send_more(ev) == 3


async def test_star_adds_picture_note_and_unstar_removes_it(penv):
    peeks, db, app, clock, tmp, (acc, ev) = penv
    add_peeks(db, tmp, acc, ev, 2, clock.t - timedelta(minutes=10))
    await peeks.tick()
    with db.session() as s:
        first = s.query(Peek).filter_by(number=1).one()
    seen = []
    hooks.on("note_added", lambda eid, **kw: _record(seen, eid))
    e, starred = await peeks.star(first.id)
    assert starred
    with db.session() as s:
        notes = s.get(Event, ev).note_list
    assert notes[0]["text"] == "⭐ rattan armchair (photo 1)" and notes[0]["peek"] == first.id
    assert Path(notes[0]["image"]).is_file() and "notes" in notes[0]["image"]
    assert seen == [ev]
    edit = [c for c in app.bot.calls if c[0] == "edit"][-1]
    assert edit[2].inline_keyboard[0][0].text == "✅ 1"
    e, starred = await peeks.star(first.id)
    assert not starred
    with db.session() as s:
        assert s.get(Event, ev).note_list == []
    assert not Path(notes[0]["image"]).exists()


async def _record(seen, eid):
    seen.append(eid)


async def test_reply_to_photo_sets_note_text(penv):
    peeks, db, app, clock, tmp, (acc, ev) = penv
    add_peeks(db, tmp, acc, ev, 1, clock.t - timedelta(minutes=10))
    await peeks.tick()
    photo = [c for c in app.bot.calls if c[0] == "photo"]
    assert photo  # a single peek goes as one photo
    with db.session() as s:
        p = s.query(Peek).one()
    msg = MagicMock()
    msg.text = "Ask the price of this chair"
    msg.reply_to_message = SimpleNamespace(message_id=p.telegram_message_id)
    msg.reply_html = AsyncMock()
    assert peeks.peek_for_message(p.telegram_message_id)
    await peeks.on_reply(SimpleNamespace(effective_message=msg), None)
    with db.session() as s:
        assert s.get(Event, ev).note_list[0]["text"] == "Ask the price of this chair"
        assert s.query(Peek).one().starred


async def test_no_answer_skips_and_orphans_join_new_event(penv):
    peeks, db, app, clock, tmp, (acc, ev) = penv
    add_peeks(db, tmp, acc, ev, 2, clock.t - timedelta(minutes=10))
    await hooks.emit("decision_changed", ev, old="yes", new="no")
    assert await peeks.tick() == 0
    add_peeks(db, tmp, acc, None, 2, clock.t - timedelta(days=2))
    with db.session() as s:
        s.get(Event, ev).user_decision = "yes"
        new = Event(account_id=acc, title="Next sale", dates="[]", user_decision="pending", status="teaser")
        s.add(new)
        s.flush()
        new_id = new.id
    await hooks.emit("event_created", new_id, kind="teaser")
    with db.session() as s:
        assert s.query(Peek).filter_by(event_id=new_id).count() == 2


# --------------------------------------------------------------- pipeline side
def test_pipeline_links_item_photos_to_next_market(world, tmp_path):  # noqa: F811
    scout, db, _, _ = world
    acc, _ = db.add_account("pujar_con_arte")
    with db.session() as s:
        s.add(Event(account_id=acc.id, title="Later", dates=json.dumps(["2026-11-28"]), status="announced"))
        s.add(Event(account_id=acc.id, title="Sooner", dates=json.dumps(["2026-11-14"]), status="announced"))
    img = tmp_path / "x.jpg"
    img.write_bytes(b"j")
    c = Candidate(account=acc, kind="story", ig_id="story:1", url="u", posted_at=at(10), caption="",
                  image_urls=[])
    assert scout.store_peeks(c, ["oak table"], [img], [], at(10)) == 1
    assert scout.store_peeks(c, [], [img], [], at(10)) == 0  # no pieces seen: no peek
    with db.session() as s:
        p = s.query(Peek).one()
        assert s.get(Event, p.event_id).title == "Sooner" and p.item_list == ["oak table"]


async def test_upload_timeout_keeps_photos_queued(penv):
    from telegram.error import TimedOut
    peeks, db, app, clock, tmp, (acc, ev) = penv
    add_peeks(db, tmp, acc, ev, 1, clock.t - timedelta(minutes=10))

    async def slow(*a, **kw):
        assert kw["write_timeout"] == 60
        raise TimedOut()

    app.bot.send_photo = slow
    assert await peeks.tick() == 0
    with db.session() as s:
        assert s.query(Peek).one().state == "new"  # sent on the next round

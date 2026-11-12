"""New-market cards are never left waiting for /start: image timeouts fall back to text, failed
cards are redelivered every 5 minutes."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram.error import NetworkError, TimedOut

from scout.bot.handlers import TelegramNotifier
from scout.db import Account, Database, Event, utcnow


class Bot:
    def __init__(self):
        self.photos, self.texts, self.fail_text = [], [], 0
        self.n = 100

    async def send_photo(self, chat_id, photo, **kw):
        assert kw["write_timeout"] == 60
        raise TimedOut()

    async def send_message(self, chat_id, text, **kw):
        if self.fail_text:
            self.fail_text -= 1
            raise NetworkError("Telegram unreachable")
        self.n += 1
        self.texts.append(text)
        return SimpleNamespace(message_id=self.n)


def setup(tmp_path, created_ago=10):
    db = Database(":memory:")
    img = tmp_path / "flyer.jpg"
    img.write_bytes(b"jpg")
    with db.session() as s:
        a = Account(handle="mercadillosmad")
        s.add(a)
        s.flush()
        e = Event(account_id=a.id, title="Market 33", dates=json.dumps(["2026-11-20"]), status="announced",
                  user_decision="pending", media_path=str(img), created_at=utcnow() - timedelta(minutes=created_ago))
        s.add(e)
        s.flush()
        eid = e.id
    app = SimpleNamespace(bot=Bot(), bot_data={})
    return TelegramNotifier(app, db, 1, ZoneInfo("Europe/Madrid")), db, app, eid


async def test_image_timeout_sends_the_card_as_text(tmp_path):
    n, db, app, eid = setup(tmp_path)
    with db.session() as s:
        ev = s.get(Event, eid)
    await n.new_event(ev, "new_event")
    assert len(app.bot.texts) == 1 and "Market 33" in app.bot.texts[0]
    with db.session() as s:
        assert s.get(Event, eid).telegram_message_id == 101


async def test_failed_cards_are_redelivered(tmp_path):
    n, db, app, eid = setup(tmp_path)
    app.bot.fail_text = 1
    with db.session() as s:
        ev = s.get(Event, eid)
    await n.new_event(ev, "new_event")  # Telegram unreachable: card not sent
    assert n.unsent() and await n.redeliver() == 1
    assert "🆕 New market" in app.bot.texts[0]
    assert await n.redeliver() == 0  # nothing left


async def test_redelivery_waits_for_fresh_cards_and_pause(tmp_path):
    n, db, app, eid = setup(tmp_path, created_ago=1)
    assert await n.redeliver() == 0  # may still be in flight
    db.set_setting("paused", "1")
    with db.session() as s:
        s.get(Event, eid).created_at = utcnow() - timedelta(minutes=10)
    assert await n.redeliver() == 0  # paused: /resume sends it

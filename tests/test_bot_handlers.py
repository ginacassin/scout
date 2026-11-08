"""Offline tests of the Telegram handlers: no network, bot calls are AsyncMocks."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from telegram import Chat, Message, Update, User
from telegram.error import BadRequest
from telegram.ext import Application, ExtBot

from scout import hooks, obs
from scout.bot.handlers import EVENTS_PAGE_SIZE, BotHandlers, TelegramNotifier
from scout.config import Settings
from scout.db import Account, Database, Event

TZ = ZoneInfo("Europe/Madrid")
CHAT = 42


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        bot_token="1:x", webhook_url="", port=8000, allowed_chat_id=CHAT, gemini_api_key="x", gemini_model="m",
        ig_provider="apify", apify_token="x", eventbrite_token="", gcal_sa_json="", gcal_calendar_id="", tz=TZ,
        data_dir=tmp_path,
    )
    db = Database(":memory:")
    db.add_account("almamarket_home")
    db.add_account("my_market_mad")
    notifier = MagicMock(spec=TelegramNotifier)
    notifier.paused = False
    notifier.send_event_card = AsyncMock(return_value=99)
    scout = SimpleNamespace(source=SimpleNamespace(spend_usd=AsyncMock(return_value=None)))
    hooks.clear()
    yield BotHandlers(scout, db, notifier, settings), db, notifier
    hooks.clear()


def add_event(db: Database, title: str, days: list[date] | None = None, decision: str = "pending",
              status: str = "announced", handle: str = "almamarket_home") -> int:
    with db.session() as s:
        acc = s.query(Account).filter_by(handle=handle).one()
        ev = Event(account_id=acc.id, title=title, user_decision=decision, status=status, area="Chamberí")
        ev.date_list = days or []
        s.add(ev)
        s.flush()
        return ev.id


def soon(n: int = 3) -> date:
    return date.today() + timedelta(days=n)


def callback_update(data: str, chat_id: int = CHAT, photo: bool = True):
    """MagicMock Update with a CallbackQuery whose message is a real Message (photo or text)."""
    msg = Message(message_id=7, date=None, chat=Chat(chat_id, "private"),
                  photo=[MagicMock()] if photo else None, text=None if photo else "card")
    q = MagicMock()
    q.data = data
    q.message = msg
    q.answer = AsyncMock()
    q.edit_message_caption = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    update = MagicMock()
    update.callback_query = q
    update.effective_chat = SimpleNamespace(id=chat_id)
    return update, q


def command_update(chat_id: int = CHAT):
    update = MagicMock()
    update.effective_chat = SimpleNamespace(id=chat_id)
    update.effective_message.reply_html = AsyncMock()
    update.effective_message.reply_text = AsyncMock()
    return update


def ctx(args=None):
    return SimpleNamespace(args=args or [], application=MagicMock())


def callback_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ---------------------------------------------------------------- decisions
def test_tap_yes_updates_db_answers_and_edits_caption(env):
    h, db, notifier = env
    ev_id = add_event(db, "Mercadillo Clara del Rey", [soon()])
    seen = []

    async def hook(event_id, **kw):
        seen.append((event_id, kw))

    hooks.on("decision_changed", hook)
    update, q = callback_update(f"d:{ev_id}:yes")
    run(h.on_callback(update, ctx()))

    with db.session() as s:
        assert s.get(Event, ev_id).user_decision == "yes"
    q.answer.assert_awaited_once_with("Marked as going")
    q.edit_message_caption.assert_awaited_once()
    caption = q.edit_message_caption.await_args.kwargs["caption"]
    assert "Mercadillo Clara del Rey" in caption and "✅ Going" in caption
    assert seen == [(ev_id, {"old": "pending", "new": "yes"})]
    notifier.send_event_card.assert_not_awaited()


def test_teaser_maybe_on_text_card(env):
    h, db, _ = env
    ev_id = add_event(db, "Something's coming", [], status="teaser")
    update, q = callback_update(f"d:{ev_id}:maybe", photo=False)
    run(h.on_callback(update, ctx()))
    with db.session() as s:
        assert s.get(Event, ev_id).user_decision == "maybe"
    q.answer.assert_awaited_once()
    q.edit_message_text.assert_awaited_once()
    assert "Watching" in q.edit_message_text.await_args.args[0]


def test_uneditable_card_falls_back_to_fresh_card(env):
    h, db, notifier = env
    ev_id = add_event(db, "Market", [soon()])
    update, q = callback_update(f"d:{ev_id}:no")
    q.edit_message_caption.side_effect = BadRequest("There is no caption in the message to edit")
    run(h.on_callback(update, ctx()))
    q.answer.assert_awaited_once_with("Skipped")
    notifier.send_event_card.assert_awaited_once()
    assert notifier.send_event_card.await_args.args[0].user_decision == "no"


def test_not_modified_is_quiet(env):
    h, db, notifier = env
    ev_id = add_event(db, "Market", [soon()], decision="yes")
    update, q = callback_update(f"d:{ev_id}:yes")
    q.edit_message_caption.side_effect = BadRequest("Message is not modified: specified new message content ...")
    run(h.on_callback(update, ctx()))
    q.answer.assert_awaited_once()
    notifier.send_event_card.assert_not_awaited()


def test_unknown_event_and_unknown_data_answer_once(env):
    h, _, _ = env
    update, q = callback_update("d:999:yes")
    run(h.on_callback(update, ctx()))
    q.answer.assert_awaited_once_with("That market is no longer tracked.")
    update, q = callback_update("d:oops")
    run(h.on_callback(update, ctx()))
    q.answer.assert_awaited_once()


def test_unauthorized_callback_is_answered_but_ignored(env):
    h, db, _ = env
    ev_id = add_event(db, "Market", [soon()])
    update, q = callback_update(f"d:{ev_id}:yes", chat_id=666)
    run(h.on_callback(update, ctx()))
    q.answer.assert_awaited_once()
    q.edit_message_caption.assert_not_awaited()
    with db.session() as s:
        assert s.get(Event, ev_id).user_decision == "pending"


# ------------------------------------------------------------------ commands
def test_list_shows_accounts(env):
    h, _, _ = env
    update = command_update()
    run(h.cmd_list(update, ctx()))
    text = update.effective_message.reply_html.await_args.args[0]
    assert "Following 2 accounts" in text and "@almamarket_home" in text and "not checked yet" in text


def test_events_filters_paging_and_open_card(env):
    h, db, notifier = env
    ids = [add_event(db, f"Market {i}", [soon(i + 1)]) for i in range(10)]
    yes_id = add_event(db, "Going one", [soon(2)], decision="yes")
    out_id = add_event(db, "Toledo fair", [soon(2)], decision="filtered")
    past_id = add_event(db, "Old one", [date.today() - timedelta(days=30)], status="past", decision="yes")

    update = command_update()
    run(h.cmd_events(update, ctx()))
    text = update.effective_message.reply_html.await_args.args[0]
    kb = update.effective_message.reply_html.await_args.kwargs["reply_markup"]
    assert "Upcoming</b> (11)" in text and "page 1/2" in text
    assert "Toledo fair" not in text and "Old one" not in text
    data = callback_data(kb)
    assert "ev:up:1" in data and "ev:up:-1" not in data
    assert sum(d.startswith("e:") for d in data) == EVENTS_PAGE_SIZE

    # next page edits the same message
    upd, q = callback_update("ev:up:1", photo=False)
    run(h.on_callback(upd, ctx()))
    q.answer.assert_awaited_once()
    text2 = q.edit_message_text.await_args.args[0]
    assert "page 2/2" in text2
    assert "ev:up:0" in callback_data(q.edit_message_text.await_args.kwargs["reply_markup"])

    # filters
    for code, present, absent in [("yes", "Going one", "Market 0"), ("out", "Toledo fair", "Going one"),
                                  ("past", "Old one", "Going one"), ("new", "Market 0", "Going one")]:
        upd, q = callback_update(f"ev:{code}:0", photo=False)
        run(h.on_callback(upd, ctx()))
        t = q.edit_message_text.await_args.args[0]
        assert present in t and absent not in t, code

    # open a card
    upd, q = callback_update(f"e:{yes_id}", photo=False)
    run(h.on_callback(upd, ctx()))
    q.answer.assert_awaited_once()
    sent = notifier.send_event_card.await_args.args[0]
    assert sent.id == yes_id and sent.user_decision == "yes"
    assert ids and out_id and past_id


def test_logs_command_reads_ring(env):
    h, _, _ = env
    obs.RING.clear()
    handler = obs.RingHandler()
    handler.addFilter(obs.TraceFilter())
    lg = logging.getLogger("scout.test")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        lg.info("just info")
        lg.warning("something <odd>")
    finally:
        lg.removeHandler(handler)
    update = command_update()
    run(h.cmd_logs(update, ctx()))
    text = update.effective_message.reply_html.await_args.args[0]
    assert text.startswith("<pre>") and "something &lt;odd&gt;" in text and "just info" not in text
    update = command_update()
    run(h.cmd_logs(update, ctx(["all"])))
    assert "just info" in update.effective_message.reply_html.await_args.args[0]


# ------------------------------------------------- routing through Application
def test_unauthorized_chat_ignored_by_application(env, monkeypatch):
    h, _, _ = env
    sent = AsyncMock()
    async def fake_get_me(self, *args, **kwargs):  # noqa: ANN001
        self._bot_user = User(1, "Scout", True, username="scout_bot")
        return self._bot_user

    monkeypatch.setattr(ExtBot, "get_me", fake_get_me)
    monkeypatch.setattr(ExtBot, "send_message", sent)
    app = Application.builder().token("1:x").build()
    h.register(app)

    def cmd(chat_id: int, update_id: int) -> Update:
        return Update.de_json({
            "update_id": update_id,
            "message": {"message_id": 1, "date": 0, "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": chat_id, "is_bot": False, "first_name": "U"}, "text": "/list",
                        "entities": [{"type": "bot_command", "offset": 0, "length": 5}]},
        }, app.bot)

    async def go():
        await app.initialize()
        try:
            before = obs.STATS.counters["updates"]
            await app.process_update(cmd(666, 1))
            assert sent.await_count == 0
            await app.process_update(cmd(CHAT, 2))
            assert sent.await_count == 1 and "Following 2 accounts" in sent.await_args.kwargs["text"]
            assert obs.STATS.counters["updates"] == before + 2
        finally:
            await app.shutdown()

    run(go())


def test_error_handler_rate_limited():
    reporter = obs.ErrorReporter(CHAT, interval=600)
    bot = SimpleNamespace(send_message=AsyncMock())

    def context(err):
        return SimpleNamespace(error=err, bot=bot, job=None)

    async def go():
        await reporter(None, context(ValueError("a")))
        await reporter(None, context(ValueError("b")))
        await reporter(None, context(KeyError("c")))

    run(go())
    assert bot.send_message.await_count == 2
    assert "Something went wrong (trace " in bot.send_message.await_args_list[0].args[1]

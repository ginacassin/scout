"""Notes on events: /note, reply-to-card, the 📝 button, /notes · /delnote, calendar description,
market-day reminders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram import Chat, Message

from scout import bookings
from scout.bot import templates as T
from scout.bot.booking_cmds import maps_url, reminder_due
from scout.db import Event
from tests.test_bookings_flow import callback, command, world  # noqa: F401 — fixture
from tests.test_calendar import NOW, TZ, add_event, dates


def text_update(text, reply_to=None):
    msg = MagicMock()
    msg.text = text
    msg.reply_to_message = reply_to
    msg.reply_html = AsyncMock()
    msg.reply_text = AsyncMock()
    upd = MagicMock()
    upd.effective_message = msg
    return upd, msg


def notes(db, ev_id):
    with db.session() as s:
        return [n["text"] for n in s.get(Event, ev_id).note_list]


async def test_note_command_updates_card_and_calendar(world):
    w = world
    ev_id = add_event(w.db, dates=dates(13), address="Calle Almagro 3")
    assert await w.sync.sync(ev_id)
    update, ctx = command([str(ev_id), "the", "green", "lamp", "(living", "room)"])
    await w.cmds.cmd_note(update, ctx)
    await w.sync.drain()
    assert notes(w.db, ev_id) == ["the green lamp (living room)"]
    assert "Noted for" in update.effective_message.reply_html.await_args.args[0]
    desc = w.google.by_key()["2026-11-13"]["description"]
    assert "📝 Notes:\n• the green lamp (living room)" in desc
    caption = w.app.bot.named("edit_message_caption")[-1][2]["caption"]
    assert "📝 <b>Notes:</b>\n• the green lamp (living room)" in caption


async def test_reply_to_card_adds_note(world):
    w = world
    ev_id = add_event(w.db, dates=dates(13))  # card message id 777
    chat = Chat(42, "private")
    card = Message(message_id=777, date=NOW, chat=chat)
    other = Message(message_id=778, date=NOW, chat=chat)
    reply = Message(message_id=900, date=NOW, chat=chat, text="blue armchair", reply_to_message=card)
    stray = Message(message_id=901, date=NOW, chat=chat, text="hello", reply_to_message=other)
    plain = Message(message_id=902, date=NOW, chat=chat, text="hello")
    assert w.cmds.wants_text(reply)
    assert not w.cmds.wants_text(stray) and not w.cmds.wants_text(plain)  # left to the core handler

    upd, msg = text_update("blue armchair", reply_to=card)
    await w.cmds.on_text(upd, None)
    assert notes(w.db, ev_id) == ["blue armchair"]


async def test_add_note_button_flow(world):
    w = world
    ev_id = add_event(w.db, dates=dates(13))
    upd, q = callback(f"nt:{ev_id}")
    await w.cmds.on_callback(upd, None)
    prompt = w.app.bot.named("send_message")[-1]
    assert "What should I remember" in prompt[1][1]
    assert prompt[2]["reply_parameters"].message_id == 777
    assert w.cmds.pending["kind"] == "note"

    upd, msg = text_update("ask about the rattan mirror")
    assert w.cmds.wants_text(msg)
    await w.cmds.on_text(upd, None)
    assert notes(w.db, ev_id) == ["ask about the rattan mirror"]
    assert w.cmds.pending is None
    upd, msg = text_update("random text")
    assert not w.cmds.wants_text(msg)


async def test_pending_note_expires(world):
    w = world
    ev_id = add_event(w.db, dates=dates(13))
    w.cmds._ask("note", ev_id)
    w.cmds.pending["until"] = NOW - timedelta(seconds=1)
    _, msg = text_update("late")
    assert not w.cmds.wants_text(msg)


async def test_notes_list_and_delete(world):
    w = world
    ev_id = add_event(w.db, dates=dates(13))
    await bookings.add_note(w.db, ev_id, "one")
    await bookings.add_note(w.db, ev_id, "two")
    update, ctx = command([str(ev_id)])
    await w.cmds.cmd_notes(update, ctx)
    text = update.effective_message.reply_html.await_args.args[0]
    assert "1. one" in text and "2. two" in text
    update, ctx = command([str(ev_id), "1"])
    await w.cmds.cmd_delnote(update, ctx)
    assert notes(w.db, ev_id) == ["two"]
    update, ctx = command([str(ev_id), "5"])
    await w.cmds.cmd_delnote(update, ctx)
    assert "No such note" in update.effective_message.reply_text.await_args.args[0]


async def test_reminder_scheduling_and_message(world):
    w = world
    tomorrow = add_event(w.db, dates=dates(9), address="Calle Almagro 3, Madrid", title="Alma")
    no_notes = add_event(w.db, dates=dates(9), title="Plain")
    later = add_event(w.db, dates=dates(20), title="Later")
    skipped = add_event(w.db, dates=dates(9), title="Skipped", decision="no")
    for ev_id in (tomorrow, later, skipped):
        await bookings.add_note(w.db, ev_id, "green lamp")
    jobs = {j.name: j for j in w.app.job_queue.jobs()}
    assert len(jobs) == 1  # only "Alma": tomorrow, has notes, not skipped
    job = next(iter(jobs.values()))
    assert job.data == tomorrow
    assert job.when == datetime(2026, 11, 9, 8, 30, tzinfo=TZ)

    # booking moves it to 1 h before the slot and replaces the old job
    await bookings.record_booking(w.db, tomorrow, datetime(2026, 11, 9, 11, 0, tzinfo=TZ), via="manual")
    live = w.app.job_queue.jobs()
    assert [j.when for j in live] == [datetime(2026, 11, 9, 10, 0, tzinfo=TZ)]

    assert await w.cmds.send_reminder(tomorrow)
    sent = w.app.bot.named("send_message")[-1]
    text = sent[1][1]
    assert "Today: Alma" in text and "Your slot: 11:00" in text and "• green lamp" in text
    url = maps_url("Calle Almagro 3, Madrid")
    assert url == "https://www.google.com/maps/search/?api=1&query=Calle+Almagro+3%2C+Madrid"
    assert sent[2]["reply_markup"].inline_keyboard[0][0].url == url
    assert sent[2]["reply_parameters"].message_id == 777
    assert not await w.cmds.send_reminder(tomorrow)  # once per day
    assert w.cmds.schedule_reminders() == []
    assert no_notes  # no reminder without notes or a booking


def test_reminder_due_rules():
    ev = Event(title="x", dates='["2026-11-13", "2026-11-14"]', notes='[{"text": "a"}]', user_decision="yes")
    assert reminder_due(ev, TZ, NOW.date()) == datetime(2026, 11, 13, 8, 30, tzinfo=TZ)
    assert reminder_due(ev, TZ, NOW.date().replace(day=14)) == datetime(2026, 11, 14, 8, 30, tzinfo=TZ)
    ev.notes = "[]"
    assert reminder_due(ev, TZ, NOW.date()) is None
    ev.booked_start = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
    assert reminder_due(ev, TZ, NOW.date()) == datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)


def test_caption_keeps_notes_when_summary_is_long():
    ev = Event(id=1, title="Alma", dates='["2026-11-13"]', sessions="[]", summary="x" * 1500,
               notes='[{"text": "green lamp"}]', user_decision="yes", booking_method="unknown")
    cap = T.event_caption(ev, "alma", TZ)
    assert len(cap) <= T.CAPTION_LIMIT
    assert "• green lamp" in cap and cap.endswith("✅ Going")

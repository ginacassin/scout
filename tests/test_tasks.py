"""Tasks: Dokploy runs `python -m scout.cron <task>`, which asks the running bot over 127.0.0.1."""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scout import cron
from scout.db import Database
from scout.tasks import TaskRunner, parse_daily_cron

TZ = ZoneInfo("Europe/Madrid")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


class JQ:
    def __init__(self):
        self.daily, self.repeating = [], []

    def run_daily(self, cb, t, name=None, data=None):
        self.daily.append((name, t, data))

    def run_repeating(self, cb, interval, first=None, name=None, data=None):
        self.repeating.append(name)


@pytest.fixture
def runner(monkeypatch):
    port = free_port()
    monkeypatch.setenv("TASKS_PORT", str(port))
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.delenv("SCHEDULER", raising=False)
    app = SimpleNamespace(bot=Bot(), job_queue=JQ(), bot_data={})
    settings = SimpleNamespace(bot_token="123:abc", tz=TZ, allowed_chat_id=1)
    clock = SimpleNamespace(t=datetime(2026, 11, 11, 12, tzinfo=timezone.utc))
    r = TaskRunner(app, settings, Database(":memory:"), now=lambda: clock.t)
    return r, app, clock


async def test_cron_command_runs_task_in_the_bot(runner, capsys):
    r, app, clock = runner
    gate = asyncio.Event()
    calls = []

    async def check_posts():
        calls.append(1)
        await gate.wait()
        return "16 accounts read, 3 new item(s)"

    async def broken():
        raise RuntimeError("Apify down")

    r.register("check-posts", check_posts, "Read posts", "2 8,11 * * *")
    r.register("broken", broken, "Fails", "0 4 * * *")
    await r.start()
    assert app.job_queue.daily == [] and app.job_queue.repeating == ["task-stale-check"]  # dokploy mode

    first = asyncio.create_task(cron.call("check-posts"))
    await asyncio.sleep(0.05)
    busy = await cron.call("check-posts")
    assert busy == {"ok": False, "summary": "check-posts is already running"}
    gate.set()
    assert (await first) == {"ok": True, "summary": "16 accounts read, 3 new item(s)"}
    assert calls == [1] and r.last_run("check-posts") == clock.t

    assert (await cron.call("broken")) == {"ok": False, "summary": "RuntimeError: Apify down"}
    listing = await cron.call("list")
    assert [t["name"] for t in listing["tasks"]] == ["check-posts", "broken"]
    assert (await cron.call("nope"))["summary"].startswith("unknown task 'nope'")


async def test_wrong_token_is_refused(runner, monkeypatch):
    r, app, clock = runner
    r.register("upkeep", lambda: asyncio.sleep(0, "x"), "", "0 4 * * *")
    await r.start()
    monkeypatch.setenv("BOT_TOKEN", "someone-else")
    assert (await cron.call("upkeep")) == {"ok": False, "summary": "bad token"}


def test_cli_exit_code_when_bot_not_running(monkeypatch, capsys):
    monkeypatch.setenv("TASKS_PORT", str(free_port()))
    assert cron.main(["check-posts"]) == 2
    assert "not reachable" in capsys.readouterr().out


async def test_internal_mode_schedules_suggested_crons(runner, monkeypatch):
    r, app, clock = runner
    r.mode = "internal"
    r.register("check-stories", lambda: asyncio.sleep(0, "x"), "", "0 13,21 * * *")
    await r.start()
    assert [(n, t) for n, t, _ in app.job_queue.daily] == [
        ("task-check-stories-1300", time(13, 0, tzinfo=TZ)), ("task-check-stories-2100", time(21, 0, tzinfo=TZ))]
    assert parse_daily_cron("2 8,11,14 * * *") == [time(8, 2), time(11, 2), time(14, 2)]


async def test_warns_once_when_a_check_stops_running(runner):
    r, app, clock = runner

    async def ok():
        return "ok"

    r.register("check-posts", ok, "", "2 8 * * *")
    await r.stale_check()  # first look: remembers when tasks started
    clock.t += timedelta(hours=10)
    assert await r.stale_check() == []
    clock.t += timedelta(hours=2)
    assert await r.stale_check() == ["check-posts"]
    assert await r.stale_check() == []  # once
    assert "Dokploy" in app.bot.sent[0] and "python -m scout.cron check-posts" in app.bot.sent[0]
    await r.run("check-posts")
    clock.t += timedelta(hours=12)
    assert await r.stale_check() == ["check-posts"]  # warns again after recovering and stopping again

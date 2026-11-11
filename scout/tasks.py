"""Scheduled tasks, run by Dokploy Schedules (or by the bot itself with SCHEDULER=internal).

Every recurring job with a fixed timetable is a named task. A Dokploy schedule on the scout-bot
service runs, inside the container:

    python -m scout.cron check-posts

which asks the running bot (over 127.0.0.1:TASKS_PORT, only reachable inside the container) to
do it now, waits, prints the outcome and exits 0 / non-zero, so Dokploy shows each run's result and
you can also launch any task by hand there. The work runs inside the bot, so everything reacting to
it (alerts, calendar, booking watcher, peeks) behaves exactly as before.

SCHEDULER=dokploy (default): the bot schedules none of these itself; set them up in Dokploy (README,
`python -m scout.cron list` prints the suggested crons). If the Instagram checks stop running, the
bot tells you once. SCHEDULER=internal: the bot runs them on the suggested crons (local runs).

Timers tied to a market (booking countdown, slot reminders) and short loops (ticket hunt, sneak
peeks, heartbeat, retries) stay inside the bot: they depend on each market's times.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable

from telegram.ext import Application, ContextTypes

from . import obs
from .db import Database

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
LAST_KEY = "task_last:{}"  # ISO time of the last successful run
STALE_WARNED = "task_stale_warned:{}"
# Warn when these haven't succeeded for this long (longest normal gap + margin).
STALE_AFTER = {"check-posts": timedelta(hours=11), "check-stories": timedelta(hours=18)}


def task_token(bot_token: str) -> str:
    return hashlib.sha256(f"scout-tasks:{bot_token}".encode()).hexdigest()[:32]


def tasks_port() -> int:
    return int(os.environ.get("TASKS_PORT", DEFAULT_PORT))


@dataclass
class Task:
    name: str
    fn: Callable[[], Awaitable[str | None]]
    description: str
    cron: str  # suggested schedule, Europe/Madrid


def parse_daily_cron(expr: str) -> list[time]:
    """'M H1,H2 * * *' → times. Only what the suggested crons use (internal scheduling)."""
    minute, hours, *rest = expr.split()
    if rest != ["*", "*", "*"]:
        raise ValueError(f"only daily crons are supported internally: {expr!r}")
    return [time(int(h), int(minute)) for h in hours.split(",")]


class TaskRunner:
    def __init__(self, app: Application, settings, db: Database, now=None):  # noqa: ANN001
        self.app = app
        self.settings = settings
        self.db = db
        self.tasks: dict[str, Task] = {}
        self.mode = os.environ.get("SCHEDULER", "dokploy").strip().lower()
        self.token = task_token(settings.bot_token)
        self.port = tasks_port()
        self._locks: dict[str, asyncio.Lock] = {}
        self._server: asyncio.base_events.Server | None = None
        self.now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------- registry
    def register(self, name: str, fn: Callable[[], Awaitable[str | None]], description: str, cron: str) -> None:
        self.tasks[name] = Task(name, fn, description, cron)
        self._locks[name] = asyncio.Lock()

    async def run(self, name: str, trigger: str = "manual") -> tuple[bool, str]:
        task = self.tasks.get(name)
        if task is None:
            return False, f"unknown task {name!r}; known: {', '.join(sorted(self.tasks))}"
        lock = self._locks[name]
        if lock.locked():
            return False, f"{name} is already running"
        async with lock:
            try:
                async with obs.trace(f"task {name}", trigger=trigger):
                    summary = await task.fn()
            except Exception as exc:  # noqa: BLE001 - reported to the caller (Dokploy shows it)
                log.exception("Task %s failed", name)
                return False, f"{type(exc).__name__}: {exc}"
        self.db.set_setting(LAST_KEY.format(name), self.now().isoformat())
        self.db.set_setting(STALE_WARNED.format(name), "")
        return True, summary or "done"

    def last_run(self, name: str) -> datetime | None:
        raw = self.db.get_setting(LAST_KEY.format(name))
        try:
            return datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    # ------------------------------------------------------------ the socket
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            req = json.loads(line or b"{}")
            if req.get("token") != self.token:
                resp = {"ok": False, "summary": "bad token"}
            elif req.get("task") == "list":
                resp = {"ok": True, "tasks": [
                    {"name": t.name, "cron": t.cron, "description": t.description,
                     "last_run": (self.last_run(t.name) or "never").__str__()} for t in self.tasks.values()]}
            else:
                ok, summary = await self.run(str(req.get("task")), trigger="cron")
                resp = {"ok": ok, "summary": summary}
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except Exception:  # noqa: BLE001
            log.exception("Task request failed")
        finally:
            writer.close()

    async def start(self, application: Application | None = None) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        log.info("Tasks listening on 127.0.0.1:%d (scheduler=%s, %d tasks)", self.port, self.mode, len(self.tasks))
        jq = self.app.job_queue
        if jq is None:
            return
        if self.mode == "internal":
            for task in self.tasks.values():
                for t in parse_daily_cron(task.cron):
                    jq.run_daily(self._job, t.replace(tzinfo=self.settings.tz), name=f"task-{task.name}-{t:%H%M}",
                                 data=task.name)
        else:
            jq.run_repeating(self.stale_check, interval=1800, first=600, name="task-stale-check")

    async def _job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, summary = await self.run(ctx.job.data, trigger="internal")
        log.info("Task %s: %s", ctx.job.data, summary if ok else f"FAILED {summary}")

    async def stale_check(self, ctx: ContextTypes.DEFAULT_TYPE | None = None) -> list[str]:
        """Dokploy mode: say once if a check stopped running (schedule deleted or disabled)."""
        warned = []
        started = self.db.get_setting("tasks_started_at")
        if not started:
            self.db.set_setting("tasks_started_at", self.now().isoformat())
            return warned
        since = datetime.fromisoformat(started)
        for name, limit in STALE_AFTER.items():
            last = self.last_run(name) or since
            if name not in self.tasks or self.now() - last < limit:
                continue
            if self.db.get_setting(STALE_WARNED.format(name)) == "1":
                continue
            self.db.set_setting(STALE_WARNED.format(name), "1")
            warned.append(name)
            when = last.astimezone(self.settings.tz).strftime("%a %-d %b %H:%M")
            try:
                await self.app.bot.send_message(
                    self.settings.allowed_chat_id,
                    f"⚠️ <b>{name}</b> hasn't run since {when}. Check its schedule in Dokploy → scout-bot → "
                    f"Schedules (command <code>python -m scout.cron {name}</code>).", parse_mode="HTML")
            except Exception:  # noqa: BLE001
                log.exception("Stale task warning failed")
        return warned


def setup_tasks(app: Application, settings, db: Database) -> TaskRunner:  # noqa: ANN001
    runner = TaskRunner(app, settings, db)
    app.bot_data["tasks"] = runner
    return runner

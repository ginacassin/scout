"""Job persistence and scheduling. No browser here, so it is unit-tested.

Job states: scheduled -> running -> booked | failed:<reason>;  scheduled/running -> cancelled.
`attempts` is written to disk *before* a job runs, so a job never runs twice, even across restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    """ISO 8601 -> aware datetime; naive values are taken as UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class JobLocked(Exception):
    """The job already ran (or is running) and can't be changed."""

    def __init__(self, job: "Job"):
        super().__init__(job.state)
        self.job = job


@dataclass
class Job:
    event_id: int
    eventbrite_id: str
    event_url: str
    opens_at: str  # ISO, UTC
    title: str
    quantity: int = 1
    state: str = "scheduled"
    attempts: int = 0
    detail: str = ""
    constraints: dict | None = None   # from Scout's planner, see slots.py
    slot_start: str = ""              # chosen timed slot, ISO with Europe/Madrid offset
    slot_end: str = ""
    slot_label: str = ""
    ticket: str = ""                  # file name under /profile/tickets (QR png or ticket pdf)
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    @property
    def opens(self) -> datetime:
        return parse_iso(self.opens_at)

    @property
    def finished(self) -> bool:
        return self.state == "booked" or self.state.startswith("failed")

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def wake_at(opens: datetime, preopen_s: int) -> datetime:
    return opens - timedelta(seconds=preopen_s)


class JobStore:
    """jobs.json on the profile volume, keyed by Scout's event id. Written atomically."""

    def __init__(self, path: Path, clock: Callable[[], datetime] = utcnow):
        self.path = Path(path)
        self.clock = clock
        self.jobs: dict[int, Job] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.jobs = {}
            return
        try:
            raw = json.loads(self.path.read_text() or "[]")
        except json.JSONDecodeError:
            log.exception("jobs.json is corrupt; keeping a copy and starting empty")
            self.path.rename(self.path.with_suffix(".corrupt"))
            raw = []
        self.jobs = {int(d["event_id"]): Job.from_dict(d) for d in raw}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(j) for j in self.jobs.values()], ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)

    def get(self, event_id: int) -> Job | None:
        return self.jobs.get(int(event_id))

    def all(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.opens_at)

    def _touch(self, job: Job, state: str | None = None, detail: str | None = None) -> None:
        if state is not None:
            job.state = state
        if detail is not None:
            job.detail = detail
        job.updated_at = self.clock().isoformat()

    def arm(self, event_id: int, eventbrite_id: str, event_url: str, opens_at: datetime, title: str,
            quantity: int, constraints: dict | None = None) -> tuple[Job, bool]:
        """Create or update a job. Returns (job, changed); `changed` is False for an identical re-post
        (new constraints count as a change).

        Raises JobLocked if the job has already been attempted."""
        opens_iso = opens_at.astimezone(timezone.utc).isoformat()
        job = self.jobs.get(int(event_id))
        if job and job.attempts > 0:
            raise JobLocked(job)
        if job and job.state == "scheduled":
            changed = (job.opens_at, job.eventbrite_id, job.event_url, job.quantity, job.constraints) != (
                opens_iso, eventbrite_id, event_url, quantity, constraints)
            job.eventbrite_id, job.event_url, job.opens_at, job.title, job.quantity, job.constraints = (
                eventbrite_id, event_url, opens_iso, title, quantity, constraints)
            self._touch(job)
            self.save()
            return job, changed
        job = Job(event_id=int(event_id), eventbrite_id=eventbrite_id, event_url=event_url, opens_at=opens_iso,
                  title=title, quantity=quantity, constraints=constraints)
        self.jobs[job.event_id] = job
        self.save()
        return job, True

    def cancel(self, event_id: int) -> Job | None:
        job = self.jobs.get(int(event_id))
        if job is None:
            return None
        if job.finished:
            raise JobLocked(job)
        if job.state == "running" and self.clock() >= job.opens:
            raise JobLocked(job)  # already clicking; too late to stop safely
        self._touch(job, "cancelled")
        self.save()
        return job

    def mark_running(self, event_id: int) -> bool:
        """Claim the single attempt. False if the job isn't runnable."""
        job = self.jobs.get(int(event_id))
        if job is None or job.state != "scheduled" or job.attempts > 0:
            return False
        job.attempts = 1
        self._touch(job, "running")
        self.save()
        return True

    def finish(self, event_id: int, state: str, detail: str = "") -> Job | None:
        job = self.jobs.get(int(event_id))
        if job is None or job.state == "cancelled":
            return job
        self._touch(job, state, detail)
        self.save()
        return job

    def set_result(self, event_id: int, slot_start: str = "", slot_end: str = "", slot_label: str = "",
                   ticket: str = "") -> Job | None:
        """Remember the chosen slot and the captured ticket file (kept even if the state changes later)."""
        job = self.jobs.get(int(event_id))
        if job is None:
            return None
        if slot_start:
            job.slot_start, job.slot_end, job.slot_label = slot_start, slot_end, slot_label
        if ticket:
            job.ticket = ticket
        self._touch(job)
        self.save()
        return job

    def recover(self, window_s: int) -> list[Job]:
        """After a restart: a job left 'running' was interrupted; a scheduled job whose window has
        passed was missed. Both become failed (and are returned so the caller can warn). The rest
        stay scheduled."""
        now = self.clock()
        failed = []
        for job in self.jobs.values():
            if job.state == "running":
                self._touch(job, "failed:interrupted", "the booker restarted during the attempt")
                failed.append(job)
            elif job.state == "scheduled" and now > job.opens + timedelta(seconds=window_s):
                self._touch(job, "failed:missed", "the booker was offline when sales opened")
                failed.append(job)
        if failed:
            self.save()
        return failed

    def runnable(self) -> list[Job]:
        return [j for j in self.all() if j.state == "scheduled" and j.attempts == 0]


Runner = Callable[[Job], Awaitable[tuple[str, str]]]  # returns (state, detail)


class Scheduler:
    """One asyncio task per scheduled job: sleep until opens_at - preopen, then run once."""

    def __init__(self, store: JobStore, runner: Runner, preopen_s: int = 90,
                 clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.store = store
        self.runner = runner
        self.preopen_s = preopen_s
        self.clock = clock
        self.sleep = sleep
        self.tasks: dict[int, asyncio.Task] = {}

    def start(self) -> None:
        for job in self.store.runnable():
            self.schedule(job)

    def schedule(self, job: Job) -> asyncio.Task:
        self.unschedule(job.event_id)
        task = asyncio.get_running_loop().create_task(self._run(job.event_id), name=f"job-{job.event_id}")
        self.tasks[job.event_id] = task
        return task

    def unschedule(self, event_id: int) -> None:
        task = self.tasks.pop(int(event_id), None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _run(self, event_id: int) -> None:
        try:
            job = self.store.get(event_id)
            if job is None:
                return
            target = wake_at(job.opens, self.preopen_s)
            # Sleep in short chunks against the wall clock so NTP corrections on the Pi don't make us late.
            while (remaining := (target - self.clock()).total_seconds()) > 0:
                await self.sleep(min(remaining, 30.0))
            job = self.store.get(event_id)
            if job is None or not self.store.mark_running(event_id):
                return
            try:
                state, detail = await self.runner(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Job %s crashed", event_id)
                state, detail = "failed:error", str(exc)[:200]
            self.store.finish(event_id, state, detail)
        except asyncio.CancelledError:
            log.info("Job %s cancelled", event_id)
            raise
        finally:
            if self.tasks.get(event_id) is asyncio.current_task():
                self.tasks.pop(event_id, None)

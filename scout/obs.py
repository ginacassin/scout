"""Observability: logging setup, trace ids, an in-memory log ring and counters.

Every log line carries a short trace id (one per Telegram update or scheduled job run), so a
single tap or poll can be followed through handlers, the pipeline, Gemini and Apify.

Where logs go:
- stdout (what Dokploy shows under the service's Logs tab)
- DATA_DIR/logs/scout.log, rotated at 2 MB, 5 files kept
- the last 300 records in memory, readable in the chat with /logs
"""

from __future__ import annotations

import functools
import logging
import secrets
import sys
import time
from collections import Counter, OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.error import NetworkError, TelegramError
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(trace_id)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
RING_SIZE = 300
ERROR_NOTICE_INTERVAL = 600.0  # seconds between "Something went wrong" messages per error type

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


# --------------------------------------------------------------------- trace ids
def new_trace_id() -> str:
    return secrets.token_hex(3)


def current_trace() -> str:
    return _trace_id.get()


def set_trace(tid: str | None = None) -> str:
    """Start a new trace in the current task (and tasks it creates)."""
    tid = tid or new_trace_id()
    _trace_id.set(tid)
    return tid


def kv(**fields: Any) -> str:
    """Render key=value pairs for a log line; values with spaces are quoted, None is skipped."""
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "yes" if v else "no"
        s = str(v)
        if not s or any(c in s for c in " \t\n="):
            s = repr(s)
        parts.append(f"{k}={s}")
    return " ".join(parts)


# ----------------------------------------------------------------- stats & ring
@dataclass
class RunInfo:
    finished_at: datetime
    duration_ms: int
    outcome: str


@dataclass
class Stats:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    counters: Counter = field(default_factory=Counter)
    runs: dict[str, RunInfo] = field(default_factory=dict)
    last_error_at: datetime | None = None

    def incr(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def uptime(self) -> str:
        secs = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        return (f"{d}d " if d else "") + f"{h}h {m:02d}m"


STATS = Stats()
COUNTER_NAMES = ("updates", "callbacks", "errors", "gemini_calls", "gemini_tokens", "apify_runs", "alerts_sent")


@dataclass
class RingRecord:
    created: float
    levelno: int
    line: str


RING: deque[RingRecord] = deque(maxlen=RING_SIZE)


class TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = _trace_id.get()
        return True


class RingHandler(logging.Handler):
    """Keeps the last records in memory and counts errors."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            t = datetime.fromtimestamp(record.created).strftime("%m-%d %H:%M:%S")
            line = f"{t} {record.levelname} {record.name} [{getattr(record, 'trace_id', '-')}] {record.getMessage()}"
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                line += f" | {type(exc).__name__}: {exc}"
            RING.append(RingRecord(record.created, record.levelno, line))
            if record.levelno >= logging.ERROR:
                STATS.incr("errors")
                STATS.last_error_at = datetime.now(timezone.utc)
        except Exception:  # noqa: BLE001
            self.handleError(record)


def recent(n: int = 20, min_level: int = logging.WARNING) -> list[str]:
    return [r.line for r in RING if r.levelno >= min_level][-n:]


def setup_logging(data_dir: Path | None, level: int = logging.INFO) -> None:
    """Configure the root logger: stdout, rotating file (if data_dir) and the memory ring."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)
    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    handlers: list[logging.Handler] = []

    out = logging.StreamHandler(sys.stdout)
    handlers.append(out)
    if data_dir is not None:
        log_dir = Path(data_dir) / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(log_dir / "scout.log", maxBytes=2 * 1024 * 1024, backupCount=4,
                                                encoding="utf-8"))
        except OSError as exc:  # keep running with stdout only
            print(f"Can't write logs to {log_dir}: {exc}", file=sys.stderr)
    handlers.append(RingHandler())
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(TraceFilter())
        root.addHandler(h)

    for noisy in ("httpx", "httpcore", "apscheduler", "google_genai", "apify_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------------ trace
class trace:  # noqa: N801 - used like a function: `async with trace("poll")` / `@trace("poll")`
    """Run a block under a fresh trace id; log start/end with duration_ms and outcome.

    Exceptions are logged with traceback and re-raised, unless `swallow=True`.
    """

    def __init__(self, name: str, *, swallow: bool = False, **fields: Any):
        self.name = name
        self.swallow = swallow
        self.fields = fields
        self._token = None
        self._t0 = 0.0

    async def __aenter__(self) -> "trace":
        parent = _trace_id.get()
        self._token = _trace_id.set(new_trace_id())
        self._t0 = time.perf_counter()
        log.info("%s start %s", self.name, kv(**self.fields, parent=None if parent == "-" else parent))
        return self

    async def __aexit__(self, et, exc, tb) -> bool:  # noqa: ANN001
        ms = int((time.perf_counter() - self._t0) * 1000)
        if et is None:
            outcome = "ok"
        elif issubclass(et, Exception):
            outcome = "error"
            log.error("%s failed %s", self.name, kv(duration_ms=ms), exc_info=(et, exc, tb))
        else:
            outcome = "cancelled"
        STATS.runs[self.name] = RunInfo(datetime.now(timezone.utc), ms, outcome)
        log.info("%s end %s", self.name, kv(duration_ms=ms, outcome=outcome))
        if self._token is not None:
            try:
                _trace_id.reset(self._token)
            except ValueError:  # exited in a different context; leave it
                pass
        return bool(self.swallow and outcome == "error")

    def __call__(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with trace(self.name, swallow=self.swallow, **self.fields):
                return await fn(*args, **kwargs)

        return wrapper


# -------------------------------------------------------------- telegram updates
_update_traces: "OrderedDict[int, str]" = OrderedDict()


def trace_for_update(update: object) -> str | None:
    if isinstance(update, Update):
        return _update_traces.get(update.update_id)
    return None


def describe_update(update: Update) -> dict[str, Any]:
    """kind (command/callback/text/other) plus the command name or callback data."""
    if update.callback_query is not None:
        return {"kind": "callback", "data": update.callback_query.data}
    msg = update.effective_message
    text = (msg.text or "") if msg else ""
    if text.startswith("/"):
        return {"kind": "command", "command": text.split()[0][1:].split("@")[0]}
    if text:
        return {"kind": "text", "chars": len(text)}
    return {"kind": "other"}


def make_update_logger(allowed_chat_id: int) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
    """Callback for TypeHandler(Update, ...) at group=-1: new trace id, one log line, continue."""
    upd_log = logging.getLogger("scout.updates")

    async def on_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        tid = set_trace()
        _update_traces[update.update_id] = tid
        while len(_update_traces) > 500:
            _update_traces.popitem(last=False)
        info = describe_update(update)
        chat_id = update.effective_chat.id if update.effective_chat else None
        allowed = chat_id == allowed_chat_id
        STATS.incr("updates")
        if info["kind"] == "callback":
            STATS.incr("callbacks")
        line = kv(update_id=update.update_id, **info, chat=chat_id, allowed=allowed)
        if allowed:
            upd_log.info("update %s", line)
        else:
            upd_log.warning("update from unauthorized chat %s", line)

    return on_update


class ErrorReporter:
    """Application error handler: log with trace id, tell the user (rate-limited per error type)."""

    def __init__(self, chat_id: int, interval: float = ERROR_NOTICE_INTERVAL):
        self.chat_id = chat_id
        self.interval = interval
        self._last_sent: dict[str, float] = {}

    async def __call__(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        err = ctx.error
        tid = trace_for_update(update) or current_trace()
        if tid == "-":
            tid = set_trace()
        else:
            _trace_id.set(tid)
        name = type(err).__name__ if err else "Unknown"
        where = kv(update_id=getattr(update, "update_id", None), job=getattr(ctx.job, "name", None))
        if isinstance(err, NetworkError) and update is None:
            # Polling hiccups: PTB retries on its own; nothing the user can act on.
            log.warning("Telegram network error %s %s: %s", name, where, err)
            return
        exc_info = (type(err), err, err.__traceback__) if err else None
        log.error("Unhandled %s %s", name, where, exc_info=exc_info)

        now = time.monotonic()
        last = self._last_sent.get(name)
        if last is not None and now - last < self.interval:
            log.info("error notice suppressed %s", kv(error=name))
            return
        self._last_sent[name] = now
        try:
            await ctx.bot.send_message(self.chat_id, f"⚠️ Something went wrong (trace {tid}).")
        except TelegramError as exc:
            log.warning("Could not send error notice: %s", exc)

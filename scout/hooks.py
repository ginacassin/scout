"""Tiny in-process event bus so features (calendar, booking watcher, auto-booker) plug in
without editing the bot or the pipeline.

Signals (all handlers are `async def fn(event_id: int, **kw)`):
- "decision_changed": kw = old: str, new: str            (user tapped a button)
- "event_created":    kw = kind: str                       (new event stored, alerted or not)
- "event_updated":    kw = changes: list[str]              (merge changed something)

Handlers run sequentially; one failing handler is logged and never breaks the others or the caller.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[None]]
_handlers: dict[str, list[Handler]] = defaultdict(list)

SIGNALS = {"decision_changed", "event_created", "event_updated"}


def on(signal: str, fn: Handler) -> None:
    if signal not in SIGNALS:
        raise ValueError(f"Unknown signal {signal!r}")
    _handlers[signal].append(fn)


async def emit(signal: str, event_id: int, **kw) -> None:  # noqa: ANN003
    for fn in list(_handlers[signal]):
        try:
            await fn(event_id, **kw)
        except Exception:  # noqa: BLE001
            log.exception("Hook %s for %s(event=%s) failed", getattr(fn, "__qualname__", fn), signal, event_id)


def clear() -> None:
    """For tests."""
    _handlers.clear()

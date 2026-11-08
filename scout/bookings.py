"""Single entry point for "I have a booking slot" — used by the auto-booker sync and by /booked.

Calendar sync, the slot planner and the Eventbrite watcher react to the `booking_confirmed`
and `ticket_added` hooks; nobody else writes Event.booked_* directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import hooks
from .db import BookingWatch, Database, Event

log = logging.getLogger(__name__)

DEFAULT_SLOT = timedelta(minutes=30)


async def record_booking(db: Database, event_id: int, start: datetime, end: datetime | None = None,
                         via: str = "manual", ticket_path: Path | str | None = None) -> Event | None:
    """Store a confirmed slot (aware datetimes) and notify listeners. Idempotent for the same slot."""
    end = end or start + DEFAULT_SLOT
    with db.session() as s:
        ev = s.get(Event, event_id)
        if ev is None:
            return None
        same = ev.booked_start == start and ev.booked_end == end
        ev.booked_start, ev.booked_end, ev.booked_via = start, end, via
        if ticket_path:
            ev.ticket_path = str(ticket_path)
        if ev.user_decision != "yes":
            ev.user_decision = "yes"
        ev.log_change(f"booked {start.isoformat()} via {via}")
        w = s.query(BookingWatch).filter_by(event_id=event_id).one_or_none()
        if w:
            w.state = "done"
            w.autobook_state = w.autobook_state if via == "auto" else (w.autobook_state or "manual")
    if not same:
        log.info("Booking recorded event=%s start=%s via=%s", event_id, start, via)
        await hooks.emit("booking_confirmed", event_id, via=via)
    elif ticket_path:
        await hooks.emit("ticket_added", event_id, path=str(ticket_path))
    return ev


async def attach_ticket(db: Database, event_id: int, path: Path | str) -> None:
    with db.session() as s:
        ev = s.get(Event, event_id)
        if ev is None:
            return
        ev.ticket_path = str(path)
    await hooks.emit("ticket_added", event_id, path=str(path))

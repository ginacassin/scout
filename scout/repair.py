"""Rebuild markets' details from every saved Instagram item (task `repair-sessions`).

    python -m scout.cron repair-sessions        # every upcoming market
    python -m scout.cron repair-sessions 23 41  # just these

Before opening hours were merged day by day, a later story mentioning a market without times could
wipe out the hours an earlier post gave. This replays every saved item linked to each upcoming market,
oldest first, with today's rules (hours kept unless newer ones are given, the most exact address, the
booking link), using what Gemini already read — no new Gemini calls. Changed markets get their card
redrawn in place and their calendar entry re-synced silently.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select

from .db import Database, Event, SourceItem
from .dedupe import upgrade_address
from .matching import event_status, parse_dates
from .parser.schema import ParseResult
from .pipeline import merge_sessions

log = logging.getLogger(__name__)


def _matching(result: ParseResult, ev: Event):  # noqa: ANN202
    """The parsed market in this item that is this event: the only one, or the one sharing a date."""
    if len(result.events) == 1:
        return result.events[0]
    days = set(ev.date_list)
    for p in result.events:
        if days & set(parse_dates(p.dates)):
            return p
    return None


def rebuild(ev: Event, items: list[SourceItem]) -> list[str]:
    """Apply the items (oldest first) to the event; returns what changed."""
    sessions = ev.session_list
    address, booking = ev.address, ev.booking_url
    for item in items:
        try:
            result = ParseResult.model_validate_json(item.parsed_json)
        except ValueError as exc:
            log.debug("repair-sessions: unreadable parse of %s: %s", item.ig_id, exc)
            continue
        p = _matching(result, ev)
        if p is None:
            continue
        if p.sessions:
            sessions = merge_sessions(sessions, [s.model_dump() for s in p.sessions])
        if p.address:
            address, _ = upgrade_address(address, p.address)
        if p.booking_url:
            booking = p.booking_url
    changes = []
    if sessions != ev.session_list:
        ev.session_list = sessions
        changes.append("opening hours")
    if address != ev.address:
        ev.address = address
        changes.append(f"address: {address}")
    if booking and booking != ev.booking_url:
        ev.booking_url = booking
        changes.append("booking link")
    if changes and ev.status != "past":
        ev.status = event_status(ev.date_list, ev.address)
    return changes


async def repair_sessions(app, db: Database, args: list[str]) -> str:  # noqa: ANN001
    from .organizers import redraw

    only = {int(a.lstrip("#")) for a in args if a.lstrip("#").isdigit()}
    with db.session() as s:
        items = list(s.scalars(select(SourceItem).where(SourceItem.parsed_json.is_not(None))
                               .order_by(SourceItem.posted_at)))
        ids = [e.id for e in s.scalars(select(Event).where(Event.status != "past"))
               if not only or e.id in only]
    lines = []
    for event_id in ids:
        with db.session() as s:
            ev = s.get(Event, event_id)
            mine = [i for i in items if event_id in json.loads(i.event_ids or "[]")]
            changes = rebuild(ev, mine)
            title = ev.title
            for ch in changes:
                ev.log_change(f"repaired {ch}")
        if changes:
            log.info("repair-sessions event=%s %s", event_id, ", ".join(changes))
            lines.append(f"#{event_id} {title}: " + ", ".join(changes))
            await redraw(app, db, event_id)
    return f"{len(lines)} of {len(ids)} markets repaired" + ("\n" + "\n".join(lines) if lines else "")

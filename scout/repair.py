"""Rebuild markets' details from every saved Instagram item (task `repair-sessions`).

    python -m scout.cron repair-sessions        # every upcoming market
    python -m scout.cron repair-sessions 23 41  # just these
    python -m scout.cron repair-sessions --from=/data/recovery/old.db   # start from an older copy
    python -m scout.cron repair-sessions --dry-run                      # only say what would change

Before opening hours were merged day by day, a later story mentioning a market without times could
wipe out the hours an earlier post gave. This replays every saved item linked to each upcoming market,
oldest first, with today's rules (hours kept unless newer ones are given, the most exact address, the
booking link), using what Gemini already read — no new Gemini calls. Changed markets get their card
redrawn in place and their calendar entry re-synced silently.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import select

from .db import Database, Event, SourceItem
from .dedupe import upgrade_address
from .matching import SAME, compare, event_status, facts_from_event, facts_from_parsed
from .parser.schema import ParseResult
from .pipeline import merge_sessions

log = logging.getLogger(__name__)


def _matching(result: ParseResult, ev: Event, linked: int, now: datetime):  # noqa: ANN202
    """The parsed market in this item that is this event. A post about one market linked to one event is
    that event; in a round-up (several markets, several events) only a clear match by the market
    matcher counts — sharing a date is not enough."""
    if len(result.events) == 1 and linked == 1:
        return result.events[0]
    old = facts_from_event(ev)
    scored = sorted(((compare(facts_from_parsed(p, now=now), old, now), i) for i, p in enumerate(result.events)),
                    key=lambda x: -x[0].score)
    same = [(c, i) for c, i in scored if c.verdict == SAME]
    if len(same) == 1 or (same and same[0][0].score > same[1][0].score):
        return result.events[same[0][1]]
    return None


def rebuild(ev: Event, items: list[SourceItem], start: dict | None = None,
            now: datetime | None = None) -> list[str]:
    """Apply the items (oldest first) to the event; returns what changed. `start`: the sessions/address/
    booking link to begin from (an older copy of the database), instead of the current ones."""
    now = now or datetime.now(timezone.utc)
    before = (ev.session_list, ev.address, ev.booking_url)
    if start is not None:
        ev.session_list, ev.address, ev.booking_url = start["sessions"], start["address"], start["booking_url"]
    sessions = ev.session_list
    address, booking = ev.address, ev.booking_url
    for item in items:
        try:
            result = ParseResult.model_validate_json(item.parsed_json)
        except ValueError as exc:
            log.debug("repair-sessions: unreadable parse of %s: %s", item.ig_id, exc)
            continue
        p = _matching(result, ev, len(json.loads(item.event_ids or "[]")), now)
        if p is None:
            continue
        if p.sessions:
            sessions = merge_sessions(sessions, [s.model_dump() for s in p.sessions])
        if p.address:
            address, _ = upgrade_address(address, p.address)
        if p.booking_url:
            booking = p.booking_url
    ev.session_list, ev.address, ev.booking_url = sessions, address, booking
    changes = []
    if sessions != before[0]:
        changes.append("opening hours")
    if address != before[1]:
        changes.append(f"address: {address}")
    if booking != before[2]:
        changes.append("booking link")
    if changes and ev.status != "past":
        ev.status = event_status(ev.date_list, ev.address)
    return changes


def _hours(sessions: list[dict]) -> str:
    return " ".join(f"{x.get('date', '')[5:]} {x.get('start') or '?'}-{x.get('end') or '?'}" for x in sessions)


_EMPTY = {"sessions": [], "address": None, "booking_url": None}  # created after the copy: items only


def _snapshot(path: str) -> dict[int, dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {i: {"sessions": json.loads(sess or "[]"), "address": addr, "booking_url": url}
                for i, sess, addr, url in con.execute("select id, sessions, address, booking_url from events")}
    finally:
        con.close()


async def repair_sessions(app, db: Database, args: list[str]) -> str:  # noqa: ANN001
    from .organizers import redraw

    only = {int(a.lstrip("#")) for a in args if a.lstrip("#").isdigit()}
    snapshot = next((a.split("=", 1)[1] for a in args if a.startswith("--from=")), None)
    starts = _snapshot(snapshot) if snapshot else {}
    dry = "--dry-run" in args
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
            start = starts.get(event_id, _EMPTY) if snapshot else None
            changes = rebuild(ev, mine, start) if mine or start is None else []
            title = ev.title
            detail = f" → {_hours(ev.session_list)} @ {ev.address}" if dry and changes else ""
            if dry:
                s.rollback()
            else:
                for ch in changes:
                    ev.log_change(f"repaired {ch}")
        if changes and dry:
            lines.append(f"#{event_id} {title}: " + ", ".join(changes) + detail)
        elif changes:
            log.info("repair-sessions event=%s %s", event_id, ", ".join(changes))
            lines.append(f"#{event_id} {title}: " + ", ".join(changes))
            await redraw(app, db, event_id)
    return f"{len(lines)} of {len(ids)} markets " + ("would change" if dry else "repaired") + ("\n" + "\n".join(lines) if lines else "")

"""Find the organiser of markets created before reposts were understood (task `refresh-organizers`).

    python -m scout.cron refresh-organizers mercadillosmad      # that account's markets on your calendar
    python -m scout.cron refresh-organizers mercadillosmad all  # …including 🤔 and unanswered ones

Markets whose organiser was already known just get their card and calendar entry redrawn. For the
rest, each market's saved Instagram items (caption + images, kept 30 days) are read again with the current
instructions; the first organiser named that isn't the posting account is stored. The card is edited
in place and the calendar entry re-synced silently — no new messages.
"""

from __future__ import annotations

import json
import logging
from datetime import timezone
from pathlib import Path

from sqlalchemy import select

from .db import Account, Database, Event, SourceItem, utcnow

log = logging.getLogger(__name__)

MAX_ITEMS = 4  # per market


async def refresh_organizers(app, scout, db: Database, args: list[str]) -> str:  # noqa: ANN001
    handles = {a.lower().lstrip("@") for a in args if a.lower() != "all"}
    decisions = ("yes", "maybe", "pending") if "all" in [a.lower() for a in args] else ("yes",)
    with db.session() as s:
        accounts = {a.id: a for a in s.scalars(select(Account))}
        events = [e for e in s.scalars(select(Event).where(Event.user_decision.in_(decisions),
                                                           Event.status != "past"))
                  if not handles or accounts[e.account_id].handle.lower() in handles]
        items = list(s.scalars(select(SourceItem).where(SourceItem.parsed_json.is_not(None))
                               .order_by(SourceItem.posted_at)))
    if not events:
        return "no markets to check" + (f" for @{', @'.join(sorted(handles))}" if handles else "")
    lines, found = [], 0
    tz = scout.settings.tz
    for ev in events:
        own = accounts[ev.account_id].handle.lower()
        if ev.organizer and ev.organizer.lower() != own:
            # Known from the first parse, but cards and calendar were drawn before organisers were shown.
            lines.append(f"#{ev.id} {ev.title}: by @{ev.organizer} · via @{own} (already known, refreshed)")
            found += 1
            await redraw(app, db, ev.id)
            continue
        mine = [i for i in items if ev.id in json.loads(i.event_ids or "[]")][-MAX_ITEMS:]
        org = None
        for item in mine:
            acc = accounts.get(item.account_id)
            # Another followed account posted about this market as its own (merged duplicates).
            if acc and acc.handle.lower() != own and _own_market(item, acc.handle):
                org = acc.handle.lower()
                break
            images = [Path(p) for p in json.loads(item.media_paths or "[]") if Path(p).is_file()]
            if not item.caption and not images:
                continue
            posted = (item.posted_at or utcnow()).replace(tzinfo=item.posted_at.tzinfo or timezone.utc) \
                if item.posted_at else utcnow()
            try:
                result = await scout.parser.parse(
                    handle=acc.handle if acc else own, item_kind=item.kind,
                    posted_at_local=posted.astimezone(tz).strftime("%A %Y-%m-%d %H:%M"),
                    today_local=utcnow().astimezone(tz).strftime("%A %Y-%m-%d"),
                    caption=item.caption or "", images=images)
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh-organizers: parse of %s failed: %s", item.ig_id, exc)
                continue
            named = [(p.organizer or "").strip().lstrip("@").lower() for p in result.events]
            named = [n for n in named if n and n != own]
            if named:
                org = named[0]
                break
        if not org:
            lines.append(f"#{ev.id} {ev.title}: no other organiser (own market, or not visible in the saved items)")
            continue
        with db.session() as s:
            row = s.get(Event, ev.id)
            row.organizer = org
        found += 1
        lines.append(f"#{ev.id} {ev.title}: by @{org} · via @{own}")
        log.info("refresh-organizers event=%s organizer=%s", ev.id, org)
        await redraw(app, db, ev.id)
    return f"{found} of {len(events)} markets updated\n" + "\n".join(lines)


def _own_market(item: SourceItem, handle: str) -> bool:
    """The item's own parse says its account runs the market (organizer empty or itself)."""
    try:
        data = json.loads(item.parsed_json or "{}")
    except ValueError:
        return False
    orgs = [(e.get("organizer") or "").lstrip("@").lower() for e in data.get("events") or []]
    return bool(orgs) and all(o in ("", handle.lower()) for o in orgs)


async def redraw(app, db: Database, event_id: int) -> None:  # noqa: ANN001
    """Card edited in place (answered or not) and the calendar entry re-synced silently."""
    from .bot import templates as T

    with db.session() as s:
        ev = s.get(Event, event_id)
        acc = s.get(Account, ev.account_id) if ev else None
    if ev is None:
        return
    tools = app.bot_data.get("admin_tools")
    if tools is not None and ev.telegram_message_id and ev.status != "past":
        if ev.user_decision == "pending":
            await tools.refresh_pending_card(event_id, None)
        else:
            await tools._edit(ev.telegram_message_id, T.event_caption(ev, acc.handle, tools.tz),
                              T.answered_keyboard(ev))
    cal = app.bot_data.get("calendar")
    if cal is not None and (ev.user_decision == "yes" or ev.gcal_event_id):
        await cal.sync(event_id, reason="silent")

"""Nightly retention (run from the 04:00 upkeep job).

- Events whose last day ended more than RETENTION_DAYS (default 14) ago are deleted, with their
  BookingWatch rows (and any other row pointing at them), their local image and ticket files.
  Dateless teasers go after 60 days. Google Calendar entries are left alone: they are history.
- Source items older than 30 days keep only what dedupe needs (ig_id, kind, account, dates,
  classification) so a post is never processed twice; caption, parsed JSON and image files go.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select

from .db import Base, Database, Event, SourceItem

log = logging.getLogger(__name__)

TEASER_DAYS = 60
SOURCE_ITEM_DAYS = 30


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("RETENTION_DAYS", "14")))
    except ValueError:
        return 14


def _remove_file(path: str | None, data_dir: Path) -> bool:
    """Delete a file, but only inside DATA_DIR (never follow a stray path elsewhere)."""
    if not path:
        return False
    p = Path(path)
    try:
        p.resolve().relative_to(data_dir.resolve())
    except (ValueError, OSError):
        return False
    if p.is_file():
        p.unlink(missing_ok=True)
        return True
    return False


def _referencing_columns() -> list[Any]:
    """Every column (in any table, including ones added by other features) with an FK to events.id."""
    cols = []
    for table in Base.metadata.sorted_tables:
        if table.name == Event.__tablename__:
            continue
        for col in table.columns:
            if any(fk.column.table.name == Event.__tablename__ for fk in col.foreign_keys):
                cols.append(col)
    return cols


def run_cleanup(db: Database, settings, now: datetime | None = None, days: int | None = None) -> dict[str, int]:  # noqa: ANN001
    now = now or datetime.now(timezone.utc)
    days = days if days is not None else retention_days()
    tz = settings.tz
    data_dir = Path(settings.data_dir)
    today = now.astimezone(tz).date()
    cutoff_day = today - timedelta(days=days)
    teaser_cutoff = now - timedelta(days=TEASER_DAYS)
    item_cutoff = now - timedelta(days=SOURCE_ITEM_DAYS)
    counts = {"events": 0, "watch_rows": 0, "files": 0, "source_items_scrubbed": 0}

    with db.session() as s:
        doomed: list[Event] = []
        for ev in s.scalars(select(Event)):
            end = ev.end_date
            if end is not None:
                if end < cutoff_day and (ev.booked_start is None or ev.booked_start.astimezone(tz).date() < cutoff_day):
                    doomed.append(ev)
            elif ev.created_at and ev.created_at < teaser_cutoff and ev.booked_start is None:
                doomed.append(ev)
        ids = [e.id for e in doomed]
        if ids:
            for col in _referencing_columns():
                res = s.execute(delete(col.table).where(col.in_(ids)))
                if col.table.name == "booking_watches":
                    counts["watch_rows"] += res.rowcount or 0
            for ev in doomed:
                for f in (ev.media_path, ev.ticket_path, *ev.note_images):
                    counts["files"] += _remove_file(f, data_dir)
                s.delete(ev)
            counts["events"] = len(ids)

        # Sneak-peek photos whose account never announced a market for them.
        from .db import Peek
        s.execute(delete(Peek).where(Peek.event_id.is_(None), Peek.created_at < now - timedelta(days=30)))

        for item in s.scalars(select(SourceItem).where(
            SourceItem.created_at < item_cutoff,
            or_(SourceItem.caption != "", SourceItem.parsed_json.is_not(None), SourceItem.media_paths != "[]"),
        )):
            try:
                media = json.loads(item.media_paths or "[]")
            except ValueError:
                media = []
            for f in media:
                counts["files"] += _remove_file(f, data_dir)
            item.caption = ""
            item.parsed_json = None
            item.media_paths = "[]"
            counts["source_items_scrubbed"] += 1

    if ids:
        _forget_settings(db, set(ids))
    log.info("Cleanup (retention %d days): %d events deleted, %d booking watches, %d files, "
             "%d source items scrubbed", days, counts["events"], counts["watch_rows"], counts["files"],
             counts["source_items_scrubbed"])
    return counts


def _forget_settings(db: Database, ids: set[int]) -> None:
    """Drop deleted events from per-event settings (calendar retry list, ticket pins)."""
    from .db import Setting

    raw = db.get_setting("gcal_pending")
    if raw:
        try:
            pending = [i for i in json.loads(raw) if i not in ids]
            db.set_setting("gcal_pending", json.dumps(pending))
        except ValueError:
            pass
    with db.session() as s:
        s.execute(delete(Setting).where(Setting.key.in_([f"ticket_pin:{i}" for i in ids])))

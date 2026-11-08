"""Retention: events gone 14 days after they end, seen Instagram ids kept forever."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from scout.cleanup import retention_days, run_cleanup
from scout.db import Account, BookingWatch, Database, Event, SourceItem
from tests.test_calendar import TZ, make_settings

NOW = datetime(2026, 11, 30, 3, 0, tzinfo=timezone.utc)


def setup(tmp_path):
    db = Database(":memory:")
    acc, _ = db.add_account("alma")
    media = tmp_path / "media"
    media.mkdir()
    (tmp_path / "tickets").mkdir()
    return db, acc.id, media


def ev(db, acc_id, days, **kw):
    with db.session() as s:
        e = Event(account_id=acc_id, title=kw.pop("title", "x"), **kw)
        e.date_list = days
        s.add(e)
        s.flush()
        return e.id


def test_deletes_events_ended_more_than_14_days_ago(tmp_path, monkeypatch):
    monkeypatch.delenv("RETENTION_DAYS", raising=False)
    db, acc, media = setup(tmp_path)
    img = media / "old.jpg"
    img.write_bytes(b"x")
    ticket = tmp_path / "tickets" / "1.png"
    ticket.write_bytes(b"x")
    outside = tmp_path.parent / "not_ours.jpg"
    outside.write_bytes(b"x")

    old = ev(db, acc, [date(2026, 11, 13), date(2026, 11, 15)], media_path=str(img), ticket_path=str(ticket),
             gcal_event_id=json.dumps({"days": {"x": "g1"}, "booking": None}))
    edge = ev(db, acc, [date(2026, 11, 16)])  # ended exactly 14 days ago: kept
    recent = ev(db, acc, [date(2026, 11, 13), date(2026, 11, 20)])  # multi-day, ended 10 days ago
    teaser_old = ev(db, acc, [], created_at=NOW - timedelta(days=61), media_path=str(outside))
    teaser_new = ev(db, acc, [], created_at=NOW - timedelta(days=10))
    with db.session() as s:
        s.add(BookingWatch(event_id=old, state="done"))
        s.add(BookingWatch(event_id=recent))
    db.set_setting("gcal_pending", json.dumps([old, recent]))

    counts = run_cleanup(db, make_settings(data_dir=tmp_path), now=NOW)

    with db.session() as s:
        left = {e.id for e in s.scalars(select(Event))}
        watches = [w.event_id for w in s.scalars(select(BookingWatch))]
    assert left == {edge, recent, teaser_new}
    assert watches == [recent]
    assert not img.exists() and not ticket.exists()
    assert outside.exists()  # never deletes outside DATA_DIR
    assert counts["events"] == 2 and counts["watch_rows"] == 1 and counts["files"] == 2
    assert json.loads(db.get_setting("gcal_pending")) == [recent]
    assert teaser_old  # deleted above


def test_retention_is_configurable(tmp_path, monkeypatch):
    db, acc, _ = setup(tmp_path)
    e = ev(db, acc, [date(2026, 11, 25)])
    monkeypatch.setenv("RETENTION_DAYS", "3")
    assert retention_days() == 3
    run_cleanup(db, make_settings(data_dir=tmp_path), now=NOW)
    with db.session() as s:
        assert s.get(Event, e) is None


def test_source_items_scrubbed_but_seen_ids_kept(tmp_path):
    db, acc, media = setup(tmp_path)
    img = media / "post.jpg"
    img.write_bytes(b"x")
    with db.session() as s:
        s.add(SourceItem(account_id=acc, ig_id="old_post", kind="post", caption="Mercadillo sábado",
                         media_paths=json.dumps([str(img)]), parsed_json='{"kind": "announcement"}',
                         classification="announcement", created_at=NOW - timedelta(days=31)))
        s.add(SourceItem(account_id=acc, ig_id="new_post", kind="post", caption="fresh",
                         parsed_json="{}", created_at=NOW - timedelta(days=5)))

    counts = run_cleanup(db, make_settings(data_dir=tmp_path), now=NOW)
    assert counts["source_items_scrubbed"] == 1
    with db.session() as s:
        items = {i.ig_id: i for i in s.scalars(select(SourceItem))}
    assert set(items) == {"old_post", "new_post"}  # dedupe key survives
    old = items["old_post"]
    assert (old.caption, old.parsed_json, old.media_paths, old.classification) == ("", None, "[]", "announcement")
    assert items["new_post"].caption == "fresh"
    assert not img.exists()
    # idempotent
    assert run_cleanup(db, make_settings(data_dir=tmp_path), now=NOW)["source_items_scrubbed"] == 0


def test_pipeline_still_sees_scrubbed_ids_as_seen(tmp_path):
    """Scout.pipeline checks `SourceItem.ig_id` existence; scrubbing must keep that true."""
    db, acc, _ = setup(tmp_path)
    with db.session() as s:
        s.add(SourceItem(account_id=acc, ig_id="p1", kind="post", caption="c", created_at=NOW - timedelta(days=90)))
    run_cleanup(db, make_settings(data_dir=tmp_path), now=NOW)
    with db.session() as s:
        assert s.scalar(select(SourceItem.id).where(SourceItem.ig_id == "p1")) is not None
        assert s.scalar(select(Account).where(Account.id == acc)) is not None

"""SQLite storage. One file on the `scout-data` volume holds everything."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from contextlib import contextmanager

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite keeps no timezone: store naive UTC, always hand back aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):  # noqa: ANN001
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_polled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_bio_links: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of URLs
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @property
    def bio_links(self) -> list[str]:
        return json.loads(self.last_bio_links or "[]")


class SourceItem(Base):
    """Every post, story or bio change Scout has seen. `ig_id` is the dedupe key."""

    __tablename__ = "source_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    ig_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # post | story | bio
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    caption: Mapped[str] = mapped_column(Text, default="")
    media_paths: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of local files
    parsed_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_ids: Mapped[str] = mapped_column(Text, default="[]")
    skipped_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Event(Base):
    """One real-world market, built from one or more source items."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    dates: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of ISO dates
    sessions: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of session dicts
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    area: Mapped[str | None] = mapped_column(String(200), nullable=True)
    region: Mapped[str] = mapped_column(String(16), default="unknown")  # madrid | outside | unknown
    organizer: Mapped[str | None] = mapped_column(String(100), nullable=True)  # handle, when not the poster
    organizer_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # their original post
    booking_method: Mapped[str] = mapped_column(String(32), default="unknown")
    booking_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    booking_opens_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    booking_scope: Mapped[str | None] = mapped_column(String(200), nullable=True)
    booking_status: Mapped[str] = mapped_column(String(16), default="unknown")
    link_in_bio: Mapped[bool] = mapped_column(Boolean, default=False)
    price: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="teaser")  # teaser | announced | confirmed | past
    user_decision: Mapped[str] = mapped_column(String(16), default="pending")  # pending | yes | maybe | no
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    media_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gcal_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    change_log: Mapped[str] = mapped_column(Text, default="[]")
    # Your own notes ("get the green lamp in the living room"), JSON list of {"at": iso, "text": str}
    notes: Mapped[str] = mapped_column(Text, default="[]")
    # A confirmed booking slot (from the auto-booker or entered with /booked)
    booked_start: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    booked_end: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    booked_via: Mapped[str | None] = mapped_column(String(16), nullable=True)  # auto | manual
    ticket_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # QR image or ticket PDF on /data
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    # -- JSON helpers -------------------------------------------------------
    @property
    def date_list(self) -> list[date]:
        return [date.fromisoformat(d) for d in json.loads(self.dates or "[]")]

    @date_list.setter
    def date_list(self, values: list[date]) -> None:
        self.dates = json.dumps(sorted({d.isoformat() for d in values}))

    @property
    def session_list(self) -> list[dict[str, Any]]:
        return json.loads(self.sessions or "[]")

    @session_list.setter
    def session_list(self, values: list[dict[str, Any]]) -> None:
        self.sessions = json.dumps(values, ensure_ascii=False)

    @property
    def note_list(self) -> list[dict[str, Any]]:
        return json.loads(self.notes or "[]")

    def add_note(self, text: str, image: str | None = None, **extra: Any) -> None:
        notes = self.note_list
        note: dict[str, Any] = {"at": utcnow().isoformat(), "text": text}
        if image:
            note["image"] = image  # local file under DATA_DIR/notes; attached to the calendar entry
        note.update({k: v for k, v in extra.items() if v is not None})
        notes.append(note)
        self.notes = json.dumps(notes, ensure_ascii=False)

    @property
    def note_images(self) -> list[str]:
        return [n["image"] for n in self.note_list if n.get("image")]

    @property
    def end_date(self) -> date | None:
        ds = self.date_list
        return max(ds) if ds else None

    def log_change(self, what: str) -> None:
        log = json.loads(self.change_log or "[]")
        log.append({"at": utcnow().isoformat(), "what": what})
        self.change_log = json.dumps(log, ensure_ascii=False)


class BookingWatch(Base):
    """Eventbrite tracking for one event (M3) and the auto-book request (M4)."""

    __tablename__ = "booking_watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True, index=True)
    eventbrite_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opens_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="resolving")  # resolving | waiting | live | sold_out | done | error
    is_free: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    pings_sent: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of ping keys already sent
    reminders_on: Mapped[bool] = mapped_column(Boolean, default=True)
    autobook: Mapped[bool] = mapped_column(Boolean, default=False)
    autobook_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ticket hunt (scout/tickethunt.py): only when you ask, until 09:00 on the market's first day.
    hunt_on: Mapped[bool] = mapped_column(Boolean, default=False)
    hunt_before: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM" cut-off, optional
    hunt_avail: Mapped[str] = mapped_column(Text, default="[]")  # JSON: matching slot keys free at last check
    hunt_alerts: Mapped[str] = mapped_column(Text, default="[]")  # JSON: ISO times of alerts sent
    hunt_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    hunt_status: Mapped[str | None] = mapped_column(String(200), nullable=True)  # last check, for /hunt


class Peek(Base):
    """A photo of pieces for sale at a market (usually a story), shown to you as a sneak peek."""

    __tablename__ = "peeks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True, index=True)
    ig_id: Mapped[str] = mapped_column(String(80), index=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    image_path: Mapped[str] = mapped_column(String(500))
    items: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of short English names
    state: Mapped[str] = mapped_column(String(10), default="new")  # new | held | sent
    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # its number in the album
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the photo
    keyboard_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the ⭐ buttons
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: utcnow())

    @property
    def item_list(self) -> list[str]:
        return json.loads(self.items or "[]")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, path: Path | str):
        url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{path}"
        self.engine: Engine = create_engine(url, future=True)

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        self._session = sessionmaker(self.engine, expire_on_commit=False)
        self._drop_self_organizers()

    def _drop_self_organizers(self) -> None:
        """Older events stored the posting account as its own organiser; it only matters when different."""
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE events SET organizer = NULL WHERE organizer IS NOT NULL AND lower(organizer) = "
                "(SELECT lower(handle) FROM accounts WHERE accounts.id = events.account_id)"))

    def _add_missing_columns(self) -> None:
        """create_all() never alters existing tables: add new nullable/defaulted columns by hand."""
        from sqlalchemy import inspect, text

        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if not insp.has_table(table.name):
                    continue
                have = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in have:
                        continue
                    ddl = col.type.compile(dialect=self.engine.dialect)
                    default = ""
                    if col.default is not None and getattr(col.default, "is_scalar", False):
                        v = col.default.arg
                        default = f" DEFAULT {int(v) if isinstance(v, bool) else repr(v)}"
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}{default}'))

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # -- settings ------------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.session() as s:
            row = s.get(Setting, key)
            return row.value if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.session() as s:
            row = s.get(Setting, key)
            if row:
                row.value = value
            else:
                s.add(Setting(key=key, value=value))

    # -- accounts --------------------------------------------------------------
    def active_accounts(self) -> list[Account]:
        with self.session() as s:
            return list(s.scalars(select(Account).where(Account.active.is_(True)).order_by(Account.handle)))

    def add_account(self, handle: str) -> tuple[Account, bool]:
        """Returns (account, created). Re-activates a previously removed account."""
        with self.session() as s:
            acc = s.scalar(select(Account).where(Account.handle == handle))
            if acc:
                created = not acc.active
                acc.active = True
                return acc, created
            acc = Account(handle=handle)
            s.add(acc)
            s.flush()
            return acc, True

    def remove_account(self, handle: str) -> bool:
        with self.session() as s:
            acc = s.scalar(select(Account).where(Account.handle == handle, Account.active.is_(True)))
            if not acc:
                return False
            acc.active = False
            return True

    def seed_accounts(self, handles: list[str]) -> None:
        if self.get_setting("seeded") == "1":
            return
        for h in handles:
            self.add_account(h)
        self.set_setting("seeded", "1")

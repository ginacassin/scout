"""Google Calendar sync (M2): markets you said ✅ to appear in a dedicated "Scout" calendar.

A service account (GCAL_SA_JSON) writes to a calendar the user shared with it (GCAL_CALENDAR_ID).
Tokens are minted with google-auth's signer and exchanged over httpx; the Calendar REST API v3 is
called directly with httpx (no googleapiclient).

What is stored in Event.gcal_event_id (a JSON string):
    {"days": {"<key>": "<gcal id>", ...}, "booking": "<gcal id>" | null,
     "booked": "<gcal id>" | null, "ticket": {Drive attachment} | null}
where <key> is "2026-11-13" for a timed day or "2026-11-13..2026-11-15" for an all-day span.

Once a slot is booked (scout.bookings.record_booking) the per-day entries and the "🎟 Book:"
reminder are deleted and ONE timed "booked" entry remains; clearing the booking restores them.
The ticket QR and every note photo are uploaded to Google Drive and attached to the entries: to your
own Drive once linked with /google (scout/google_link.py), else to a Shared Drive folder shared with
the service account (GDRIVE_FOLDER_ID). The ticket is pinned in the Telegram chat only when it can't
be attached. Uploaded files are remembered in state["files"] {local path: attachment}.
Every calendar event also carries extendedProperties.private {scoutId, scoutKey}, so a lost
column can be rebuilt from Google (events.list ?privateExtendedProperty=scoutId=N) and retries
never duplicate.

Failures never break the bot: the event id goes into settings["gcal_pending"] and a job retries
every 10 minutes. That job also creates entries for ✅ events that only later got dates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import time as _time
import uuid
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy import select

from scout import hooks
from scout.db import Account, Database, Event

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/calendar.events"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DEFAULT_SCOPES = (SCOPE, DRIVE_SCOPE)
API = "https://www.googleapis.com/calendar/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
PENDING_KEY = "gcal_pending"
WARNED_KEY = "gcal_warned"
RETRY_SECONDS = 600
BOOKING = "booking"
BOOKED = "booked"
QUOTA_WARNED_KEY = "gdrive_quota_warned"
TICKET_IN_CHAT = "Ticket QR: in your Scout chat (pinned)"
MY_TICKETS = "https://www.eventbrite.es/mytickets/"


def configured_scopes() -> list[str]:
    """GOOGLE_SCOPES (space or comma separated; short names like "drive" allowed) or the defaults."""
    raw = os.environ.get("GOOGLE_SCOPES", "").replace(",", " ").split()
    if not raw:
        return list(DEFAULT_SCOPES)
    return [x if x.startswith("https://") else f"https://www.googleapis.com/auth/{x}" for x in raw]

BOOKING_NAME = {"eventbrite": "Eventbrite", "other_link": "booking link", "dm": "DM", "free_entry": "Free entry"}


class CalendarError(Exception):
    def __init__(self, status: int | None, message: str, reason: str | None = None):
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.reason = reason  # Google's error reason, e.g. "storageQuotaExceeded"

    @property
    def access_problem(self) -> bool:
        """Calendar not shared / wrong id / key revoked: the user has to fix something in Google."""
        return self.status in (401, 403, 404)


# --------------------------------------------------------------------------- auth
class TokenProvider(Protocol):
    async def token(self) -> str: ...
    def invalidate(self) -> None: ...


class ServiceAccountTokens:
    """Mints OAuth access tokens for a service account. The key itself is never logged."""

    def __init__(self, info: dict[str, Any], client: httpx.AsyncClient, scopes: list[str] | None = None):
        from google.auth import jwt
        from google.oauth2 import service_account

        self._jwt = jwt
        self.scopes = list(scopes or configured_scopes())
        self._creds = service_account.Credentials.from_service_account_info(info, scopes=self.scopes)
        self._token_uri = info.get("token_uri") or DEFAULT_TOKEN_URI
        self._client = client
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()
        self.email: str = self._creds.service_account_email

    def invalidate(self) -> None:
        self._token = None

    async def token(self) -> str:
        async with self._lock:
            if self._token and _time.time() < self._expires - 60:
                return self._token
            now = int(_time.time())
            assertion = self._jwt.encode(self._creds.signer, {
                "iss": self.email, "scope": " ".join(self.scopes), "aud": self._token_uri, "iat": now, "exp": now + 3600,
            })
            resp = await self._client.post(self._token_uri, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion.decode(),
            })
            if resp.status_code != 200:
                # Google's body is a short error code ("invalid_grant"); it never echoes the key.
                raise CalendarError(401 if resp.status_code in (400, 401) else resp.status_code,
                                    f"token endpoint: {resp.text[:200]}")
            body = resp.json()
            self._token = body["access_token"]
            self._expires = _time.time() + int(body.get("expires_in", 3600))
            return self._token


# --------------------------------------------------------------------------- mapping (pure)
def _consecutive_runs(days: list[date]) -> list[list[date]]:
    runs: list[list[date]] = []
    for d in sorted(set(days)):
        if runs and d - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])
    return runs


def _fmt_day(d: date) -> str:
    return d.strftime("%a %-d %b")


def _sessions_by_day(ev: Event) -> dict[date, list[dict[str, Any]]]:
    out: dict[date, list[dict[str, Any]]] = {}
    for s in ev.session_list:
        try:
            d = date.fromisoformat(s["date"])
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(d, []).append(s)
    for v in out.values():
        v.sort(key=lambda s: s.get("start") or "")
    return out


def schedule_text(ev: Event) -> list[str]:
    by_day = _sessions_by_day(ev)
    lines = []
    for d in ev.date_list:
        spans = []
        for s in by_day.get(d, []):
            if s.get("start") or s.get("end"):
                span = f"{s.get('start') or '?'}–{s.get('end') or '?'}"
                spans.append(span + (" 🎟" if s.get("booking_required") else ""))
            elif s.get("booking_required"):
                spans.append("🎟")
        lines.append(_fmt_day(d) + (" · " + ", ".join(spans) if spans else ""))
    return lines


def booking_text(ev: Event, tz) -> str | None:  # noqa: ANN001
    method = ev.booking_method or "unknown"
    if method in ("unknown", "free_entry") and not (ev.booking_url or ev.booking_opens_at or ev.booking_scope):
        return "Free entry" if method == "free_entry" else None
    head = BOOKING_NAME.get(method, "Booking")
    head = head[0].upper() + head[1:]
    parts = [head + (f" — {ev.booking_scope}" if ev.booking_scope else "")]
    if ev.booking_opens_at:
        parts.append("opens " + ev.booking_opens_at.astimezone(tz).strftime("%a %-d %b %H:%M"))
    if ev.booking_url:
        parts.append(f"link {ev.booking_url}")
    return "Booking: " + "; ".join(parts)


def describe(ev: Event, handle: str | None, tz) -> str:  # noqa: ANN001
    blocks = []
    ig = ev.source_url or (f"https://www.instagram.com/{handle}/" if handle else None)
    if ig:
        blocks.append(f"📸 Instagram{f' @{handle}' if handle else ''}: {ig}")
    if ev.summary:
        blocks.append(ev.summary.strip())
    b = booking_text(ev, tz)
    if b:
        blocks.append("🎟 " + b)
    sched = schedule_text(ev)
    if sched:
        blocks.append("Opening hours:\n" + "\n".join(sched))
    notes = notes_text(ev)
    if notes:
        blocks.append(notes)
    blocks.append("Added by Scout")
    return "\n\n".join(blocks)


def notes_text(ev: Event) -> str | None:
    notes = [n.get("text", "") + (" 📷 (photo attached)" if n.get("image") else "")
             for n in ev.note_list if n.get("text") or n.get("image")]
    return ("📝 Notes:\n" + "\n".join(f"• {n}" for n in notes)) if notes else None


def describe_booked(ev: Event, handle: str | None, tz, eventbrite_url: str | None = None,  # noqa: ANN001
                    ticket: dict[str, Any] | None = None) -> str:
    blocks = []
    ig = ev.source_url or (f"https://www.instagram.com/{handle}/" if handle else None)
    if ig:
        blocks.append(f"📸 Instagram{f' @{handle}' if handle else ''}: {ig}")
    if ev.address:
        blocks.append(f"📍 {ev.address}")
    if ev.booking_method == "eventbrite" or eventbrite_url:
        lines = []
        if eventbrite_url:
            lines.append(f"🎟 Eventbrite: {eventbrite_url}")
        lines.append(f"🎫 Your Eventbrite orders: {MY_TICKETS}")
        blocks.append("\n".join(lines))
    elif ev.booking_url:
        blocks.append(f"🎟 Booking: {ev.booking_url}")
    if ev.ticket_path:
        if ticket and ticket.get("fileUrl"):
            blocks.append(f"🎫 Ticket QR: attached to this event\n{ticket['fileUrl']}")
        else:
            blocks.append(TICKET_IN_CHAT)
    notes = notes_text(ev)
    if notes:
        blocks.append(notes)
    if ev.summary:
        blocks.append(ev.summary.strip())
    blocks.append("Booked automatically by Scout" if ev.booked_via == "auto" else "Booked by you")
    return "\n\n".join(blocks)


def _clean(att: dict[str, Any]) -> dict[str, Any]:
    return {k: att[k] for k in ("fileUrl", "title", "mimeType", "fileId") if att.get(k)}


def booked_event(ev: Event, handle: str | None, tz, eventbrite_url: str | None = None,  # noqa: ANN001
                 ticket: dict[str, Any] | None = None,
                 attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The single timed calendar entry that replaces all dates once a slot is booked."""
    tzname = getattr(tz, "key", str(tz))
    start = ev.booked_start.astimezone(tz)
    end = (ev.booked_end or ev.booked_start + timedelta(minutes=30)).astimezone(tz)
    body: dict[str, Any] = {
        "summary": f"🎟 {ev.title} — booked {start:%H:%M}",
        "location": ev.address or ev.area or "",
        "description": describe_booked(ev, handle, tz, eventbrite_url, ticket),
        "start": {"dateTime": start.replace(tzinfo=None).isoformat(timespec="seconds"), "timeZone": tzname},
        "end": {"dateTime": end.replace(tzinfo=None).isoformat(timespec="seconds"), "timeZone": tzname},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]},
        "extendedProperties": {"private": {"scoutId": str(ev.id), "scoutKey": BOOKED}},
    }
    if attachments is not None:  # always sent when attaching is possible, so removed files go too
        body["attachments"] = [_clean(a) for a in attachments]
    elif ticket and ticket.get("fileUrl"):
        body["attachments"] = [_clean(ticket)]
    return body


def _hm(value: str | None) -> tuple[int, int] | None:
    try:
        h, m = (value or "").strip().split(":")[:2]
        return int(h), int(m)
    except ValueError:
        return None


def desired_events(ev: Event, handle: str | None, tz, now: datetime,  # noqa: ANN001
                   attachments: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """key -> Calendar API event body. Keys are stable so patches hit the right calendar entry."""
    days = ev.date_list
    if not days:
        return {}
    tzname = getattr(tz, "key", str(tz))
    by_day = _sessions_by_day(ev)
    timed = any(_hm(s.get("start")) for d in days for s in by_day.get(d, []))
    common = {
        "summary": f"🪑 {ev.title}",
        "location": ev.address or ev.area or "",
        "description": describe(ev, handle, tz),
    }
    if attachments is not None:
        common["attachments"] = [_clean(a) for a in attachments]
    out: dict[str, dict[str, Any]] = {}

    def add(key: str, start: dict[str, str], end: dict[str, str]) -> None:
        out[key] = {**common, "start": start, "end": end,
                    "extendedProperties": {"private": {"scoutId": str(ev.id), "scoutKey": key}}}

    allday_days = []
    for d in days:
        starts = [_hm(s.get("start")) for s in by_day.get(d, []) if _hm(s.get("start"))]
        ends = [_hm(s.get("end")) for s in by_day.get(d, []) if _hm(s.get("end"))]
        if not (timed and starts):
            allday_days.append(d)
            continue
        sh, sm = min(starts)
        start = datetime(d.year, d.month, d.day, sh, sm)
        if ends:
            eh, em = max(ends)
            end = datetime(d.year, d.month, d.day, eh, em)
            if end <= start:
                end += timedelta(days=1)  # closes after midnight
        else:
            end = start + timedelta(hours=2)
        add(d.isoformat(),
            {"dateTime": start.isoformat(timespec="seconds"), "timeZone": tzname},
            {"dateTime": end.isoformat(timespec="seconds"), "timeZone": tzname})
    for run in _consecutive_runs(allday_days):
        key = run[0].isoformat() if len(run) == 1 else f"{run[0].isoformat()}..{run[-1].isoformat()}"
        add(key, {"date": run[0].isoformat()}, {"date": (run[-1] + timedelta(days=1)).isoformat()})

    opens = ev.booking_opens_at
    if opens and opens > now:
        local = opens.astimezone(tz).replace(tzinfo=None)
        desc = [f"Booking opens for 🪑 {ev.title}"]
        if ev.booking_scope:
            desc.append(ev.booking_scope)
        if ev.booking_url:
            desc.append(ev.booking_url)
        out[BOOKING] = {
            "summary": f"🎟 Book: {ev.title}",
            "description": "\n".join(desc),
            "location": ev.booking_url or "",
            "start": {"dateTime": local.isoformat(timespec="seconds"), "timeZone": tzname},
            "end": {"dateTime": (local + timedelta(minutes=15)).isoformat(timespec="seconds"), "timeZone": tzname},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 5}]},
            "extendedProperties": {"private": {"scoutId": str(ev.id), "scoutKey": BOOKING}},
        }
    return out


def load_state(raw: str | None) -> dict[str, Any]:
    """Old states keep their exact shape ({"days", "booking"}); "booked"/"ticket" appear only when used."""
    if not raw:
        return {"days": {}, "booking": None}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"days": {"legacy": raw}, "booking": None}  # pre-M2 plain id
    if isinstance(data, list):  # tolerate a bare list of ids
        return {"days": {f"legacy{i}": x for i, x in enumerate(data)}, "booking": None}
    out = {"days": dict(data.get("days") or {}), "booking": data.get("booking")}
    for extra in (BOOKED, "ticket", "files"):
        if data.get(extra):
            out[extra] = data[extra]
    return out


def dump_state(state: dict[str, Any]) -> str | None:
    if not state["days"] and not state["booking"] and not state.get(BOOKED) and not state.get("ticket") \
            and not state.get("files"):
        return None
    out = {"days": state["days"], "booking": state["booking"]}
    for extra in (BOOKED, "ticket", "files"):
        if state.get(extra):
            out[extra] = state[extra]
    return json.dumps(out)


# --------------------------------------------------------------------------- sync
class CalendarSync:
    def __init__(self, settings, db: Database, app=None, *,  # noqa: ANN001
                 client: httpx.AsyncClient | None = None, tokens: TokenProvider | None = None,
                 sa_email: str | None = None, now=None):  # noqa: ANN001
        self.settings = settings
        self.db = db
        self.app = app
        self.tz = settings.tz
        self.calendar_id = settings.gcal_calendar_id
        self.client = client or httpx.AsyncClient(timeout=20)
        self._now = now or (lambda: datetime.now(timezone.utc))
        if tokens is None:
            info = json.loads(settings.gcal_sa_json)
            tokens = ServiceAccountTokens(info, self.client)
            sa_email = sa_email or tokens.email
        self.tokens = tokens
        self.sa_email = sa_email or "the service account"
        self.user_drive = None  # scout.google_link.UserDrive, set by setup when configured
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---- HTTP ---------------------------------------------------------------
    @property
    def _events_url(self) -> str:
        return f"{API}/calendars/{quote(self.calendar_id, safe='')}/events"

    async def _call(self, method: str, url: str, headers_extra: dict[str, str] | None = None,
                    **kw) -> dict[str, Any]:  # noqa: ANN003
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {await self.tokens.token()}", **(headers_extra or {})}
            try:
                resp = await self.client.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                raise CalendarError(None, f"network: {type(exc).__name__}") from exc
            if resp.status_code == 401 and attempt == 1:
                self.tokens.invalidate()  # expired or revoked; mint a fresh one and try once more
                continue
            if resp.status_code >= 400:
                reason = None
                try:
                    err = resp.json().get("error", {})
                    msg = err.get("message", "")
                    reason = next((e.get("reason") for e in err.get("errors") or [] if e.get("reason")), None)
                except (ValueError, AttributeError):
                    msg = resp.text[:200]
                raise CalendarError(resp.status_code, msg, reason)
            return resp.json() if resp.content else {}
        raise AssertionError("unreachable")

    async def _find(self, event_id: int) -> list[dict[str, Any]]:
        data = await self._call("GET", self._events_url, params={
            "privateExtendedProperty": f"scoutId={event_id}", "maxResults": 50, "showDeleted": "false",
        })
        return [i for i in data.get("items", []) if i.get("status") != "cancelled"]

    @staticmethod
    def _params(body: dict[str, Any]) -> dict[str, str] | None:
        return {"supportsAttachments": "true"} if "attachments" in body else None

    async def _insert(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", self._events_url, json=body, params=self._params(body))

    async def _patch(self, gid: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("PATCH", f"{self._events_url}/{quote(gid, safe='')}", json=body,
                                params=self._params(body))

    async def _delete(self, gid: str) -> None:
        try:
            await self._call("DELETE", f"{self._events_url}/{quote(gid, safe='')}")
        except CalendarError as exc:
            if exc.status not in (404, 410):  # already gone is fine
                raise

    # ---- pending list ---------------------------------------------------------
    def pending(self) -> list[int]:
        return json.loads(self.db.get_setting(PENDING_KEY, "[]") or "[]")

    def _set_pending(self, event_id: int, on: bool) -> None:
        ids = self.pending()
        if on and event_id not in ids:
            ids.append(event_id)
        elif not on and event_id in ids:
            ids.remove(event_id)
        else:
            return
        self.db.set_setting(PENDING_KEY, json.dumps(ids))

    # ---- core -----------------------------------------------------------------
    def _load(self, event_id: int) -> tuple[Event | None, str | None]:
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            if not ev:
                return None, None
            acc = s.get(Account, ev.account_id)
            return ev, acc.handle if acc else None

    def _eventbrite_url(self, event_id: int, ev: Event) -> str | None:
        from scout.db import BookingWatch

        with self.db.session() as s:
            w = s.scalar(select(BookingWatch).where(BookingWatch.event_id == event_id))
            url = w.event_url if w else None
        if not url and (ev.booking_url or "").startswith("https://") and "eventbrite" in ev.booking_url:
            url = ev.booking_url
        return url

    def _save_state(self, event_id: int, state: dict[str, Any]) -> None:
        with self.db.session() as s:
            row = s.get(Event, event_id)
            if row:
                row.gcal_event_id = dump_state(state)

    def _is_past(self, ev: Event) -> bool:
        days = ev.date_list
        return ev.status == "past" or bool(days and max(days) < self._now().astimezone(self.tz).date())

    async def sync(self, event_id: int, reason: str = "sync", changes: list[str] | None = None) -> bool:
        """Make the calendar match the event. Returns True on success (or nothing to do)."""
        lock = self._locks.setdefault(event_id, asyncio.Lock())
        async with lock:
            try:
                await self._sync(event_id, reason, changes or [])
            except CalendarError as exc:
                log.warning("Calendar sync for event %s failed (%s); will retry", event_id, exc)
                self._set_pending(event_id, True)
                if exc.access_problem:
                    await self._warn_access()
                return False
            except Exception:  # noqa: BLE001
                log.exception("Calendar sync for event %s crashed; will retry", event_id)
                self._set_pending(event_id, True)
                return False
            self._set_pending(event_id, False)
            if self.db.get_setting(WARNED_KEY) == "1":
                self.db.set_setting(WARNED_KEY, "0")  # access works again; warn again if it breaks later
            return True

    async def _sync(self, event_id: int, reason: str, changes: list[str]) -> None:
        ev, handle = self._load(event_id)
        if ev is None:
            return
        if self._is_past(ev):
            return  # leave history alone
        state = load_state(ev.gcal_event_id)
        booked = ev.booked_start is not None
        want = ev.user_decision == "yes" and (bool(ev.date_list) or booked)

        if not want:
            items = [(k, v) for k, v in state["days"].items()]
            if state["booking"]:
                items.append((BOOKING, state["booking"]))
            if state.get(BOOKED):
                items.append((BOOKED, state[BOOKED]))
            if not items and event_id in self.pending():
                # An earlier create may have reached Google before failing here: clean up orphans.
                items = [(i["id"], i["id"]) for i in await self._find(event_id)]
            for key, gid in items:
                await self._delete(gid)
                self._drop(state, key)
                self._save_state(event_id, state)  # progress survives a mid-way failure
            if items:
                log.info("Calendar: removed event %s", event_id)
            return

        atts = await self._attachments(ev, state)
        self._save_state(event_id, state)
        if booked:
            ticket = (state.get("files") or {}).get(ev.ticket_path or "") or state.get("ticket")
            desired = {BOOKED: booked_event(ev, handle, self.tz, self._eventbrite_url(event_id, ev),
                                            ticket, atts)}
        else:
            desired = desired_events(ev, handle, self.tz, self._now(), atts)
        had_any = bool(state["days"]) or bool(state.get(BOOKED))

        # Idempotency: with nothing recorded, adopt whatever Google already has for this scoutId.
        if not state["days"] and not state["booking"] and not state.get(BOOKED):
            for item in await self._find(event_id):
                key = item.get("extendedProperties", {}).get("private", {}).get("scoutKey") or item["id"]
                if key in (BOOKING, BOOKED):
                    state[key] = item["id"]
                else:
                    state["days"][key] = item["id"]

        existing: dict[str, str] = dict(state["days"])
        if state["booking"]:
            existing[BOOKING] = state["booking"]
        if state.get(BOOKED):
            existing[BOOKED] = state[BOOKED]
        # Once booked, the "🎟 Book:" reminder goes too; otherwise a past reminder stays as a record.
        booking_passed = (not booked and ev.booking_opens_at is not None
                          and ev.booking_opens_at <= self._now())

        first_link: str | None = None
        created = patched = 0
        for key, gid in existing.items():
            if key in desired:
                continue
            if key == BOOKING and booking_passed:
                continue  # the reminder already happened; keep it as a record
            await self._delete(gid)
            self._drop(state, key)
            self._save_state(event_id, state)
        for key, body in desired.items():
            gid = existing.get(key)
            res = None
            if gid:
                try:
                    res = await self._patch(gid, body)
                    patched += 1
                except CalendarError as exc:
                    if exc.status not in (404, 410):
                        raise
                    res = None  # deleted by hand in Google Calendar: recreate below
            if res is None:
                res = await self._insert(body)
                created += 1
            if key in (BOOKING, BOOKED):
                state[key] = res["id"]
            else:
                state["days"][key] = res["id"]
            if key != BOOKING:
                first_link = first_link or res.get("htmlLink")
            self._save_state(event_id, state)

        log.info("Calendar: event %s synced (%d created, %d updated)", event_id, created, patched)
        if reason == "booked" and booked:
            when = ev.booked_start.astimezone(self.tz).strftime("%a %-d %b %H:%M")
            await self._reply(ev, f"📅 Calendar: only your booked slot is left — {when}", first_link)
        elif reason == "unbooked" and not booked and state["days"]:
            await self._reply(ev, "📅 Calendar: booking cleared, all dates are back", first_link)
        elif not had_any and (state["days"] or state.get(BOOKED)):
            await self._reply(ev, "📅 Added to your Scout calendar", first_link)
        elif reason == "update" and (created or patched):
            what = "; ".join(changes) if changes else ev.title
            await self._reply(ev, f"📅 Calendar updated: {what}", first_link)

    @staticmethod
    def _drop(state: dict[str, Any], key: str) -> None:
        if key == BOOKING:
            state["booking"] = None
        elif key == BOOKED:
            state.pop(BOOKED, None)
        else:
            state["days"].pop(key, None)

    # ---- ticket QR on Google Drive ---------------------------------------------
    @property
    def drive_folder(self) -> str:
        return getattr(self.settings, "gdrive_folder_id", "") or os.environ.get("GDRIVE_FOLDER_ID", "")

    @property
    def user_drive_linked(self) -> bool:
        return self.user_drive is not None and self.user_drive.linked

    @property
    def can_attach(self) -> bool:
        """Files can go on calendar entries: your Drive is linked (/google) or a Shared Drive folder is set."""
        return self.user_drive_linked or bool(self.drive_folder)

    async def upload_file(self, event_id: int, path: str | Path, title: str) -> dict[str, Any] | None:
        path = Path(path)
        if not path.is_file():
            return None
        if self.user_drive_linked:
            try:
                att = await self.user_drive.upload(path, title)
                att["via"] = "user"
                return att
            except Exception as exc:  # noqa: BLE001
                log.warning("Upload to your Drive for event %s failed: %s", event_id, exc)
                return None
        return await self._sa_upload(event_id, path, title)

    async def upload_ticket(self, event_id: int, path: str | Path) -> dict[str, Any] | None:
        ev, _ = self._load(event_id)
        title = f"Scout ticket #{event_id} — {ev.title if ev else ''}".strip(" —")[:120] + Path(path).suffix.lower()
        return await self.upload_file(event_id, path, title)

    async def _attachments(self, ev: Event, state: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Upload the ticket and note photos not uploaded yet; forget removed ones. None = can't attach."""
        if not self.can_attach:
            return None
        files: dict[str, Any] = state.setdefault("files", {})
        if state.get("ticket") and ev.ticket_path and ev.ticket_path not in files:
            files[ev.ticket_path] = state.pop("ticket")  # pre-notes state shape
        state.pop("ticket", None)
        wanted = ([ev.ticket_path] if ev.ticket_path else []) + ev.note_images
        wanted = [p for p in dict.fromkeys(wanted) if p and (p in files or Path(p).is_file())]
        texts = {n["image"]: n.get("text", "") for n in ev.note_list if n.get("image")}
        for p in wanted:
            if p in files:
                continue
            if p == ev.ticket_path:
                att = await self.upload_ticket(ev.id, p)
                await self._ticket_result(ev.id, p, att is not None)
            else:
                label = (texts.get(p) or "photo").lstrip("⭐📷 ").strip()[:60]
                att = await self.upload_file(ev.id, p, f"Scout #{ev.id} — {label}{Path(p).suffix.lower()}")
            if att:
                files[p] = att
        for p in [p for p in files if p not in wanted]:
            gone = files.pop(p)
            if gone.get("via") == "user" and gone.get("fileId") and self.user_drive_linked:
                await self.user_drive.delete(gone["fileId"])
        if not files:
            state.pop("files", None)
        return [files[p] for p in wanted if p in files][:25]

    async def _ticket_result(self, event_id: int, path: str, ok: bool) -> None:
        """Attached → no pinned copy needed; failed → pin it in the chat so it's not lost."""
        cmds = self.app.bot_data.get("booking_cmds") if self.app is not None else None
        if cmds is None:
            return
        try:
            if ok:
                await cmds._unpin(event_id)
            else:
                await cmds.pin_ticket(event_id, path)
        except Exception:  # noqa: BLE001
            log.exception("Ticket pin/unpin for event %s failed", event_id)

    async def backfill_attachments(self) -> int:
        """After linking Drive: attach tickets and note photos to existing entries."""
        with self.db.session() as s:
            ids = [e.id for e in s.scalars(select(Event).where(Event.user_decision == "yes", Event.status != "past"))
                   if e.ticket_path or e.note_images]
        n = 0
        for event_id in ids:
            if await self.sync(event_id, reason="silent"):
                n += 1
        return n

    async def _sa_upload(self, event_id: int, path: Path, title: str) -> dict[str, Any] | None:
        """Upload into GDRIVE_FOLDER_ID (a Shared Drive folder, never shared publicly). None when not
        possible: no folder configured, quota exceeded (folder in a My Drive) or any other Drive error."""
        folder = self.drive_folder
        if not folder:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        boundary = f"scout-{uuid.uuid4().hex}"
        meta = json.dumps({"name": title, "parents": [folder], "mimeType": mime})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + path.read_bytes() + \
            f"\r\n--{boundary}--\r\n".encode()
        try:
            res = await self._call("POST", DRIVE_UPLOAD, content=body,
                                   params={"uploadType": "multipart", "supportsAllDrives": "true",
                                           "fields": "id,name,mimeType,webViewLink"},
                                   headers_extra={"Content-Type": f"multipart/related; boundary={boundary}"})
        except CalendarError as exc:
            if exc.reason == "storageQuotaExceeded" or "storage quota" in str(exc).lower():
                log.warning("Drive upload for event %s: service account has no storage quota — "
                            "GDRIVE_FOLDER_ID must be a folder in a Shared Drive", event_id)
                if self.db.get_setting(QUOTA_WARNED_KEY) != "1":
                    self.db.set_setting(QUOTA_WARNED_KEY, "1")
                    await self._send("ℹ️ I couldn't put the ticket in Google Drive: service accounts have no "
                                     "storage in a personal My Drive. Send /google to link your own Drive instead. "
                                     "The ticket is pinned here in the chat meanwhile.")
            else:
                log.warning("Drive upload for event %s failed (%s); ticket stays in Telegram only", event_id, exc)
            return None
        except Exception:  # noqa: BLE001
            log.exception("Drive upload for event %s crashed", event_id)
            return None
        fid = res.get("id")
        return {"fileId": fid, "title": res.get("name") or title, "mimeType": res.get("mimeType") or mime,
                "fileUrl": res.get("webViewLink") or f"https://drive.google.com/open?id={fid}"}

    async def attach_ticket(self, event_id: int, path: str | Path | None = None) -> bool:
        """Re-sync so the entry carries the ticket (uploaded inside the sync when attaching is possible)."""
        return await self.sync(event_id, reason="ticket")

    # ---- Telegram feedback -------------------------------------------------------
    async def _send(self, text: str, reply_to: int | None = None, url: str | None = None) -> None:
        if self.app is None:
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

        kw: dict[str, Any] = {}
        if url:
            kw["reply_markup"] = InlineKeyboardMarkup([[InlineKeyboardButton("Open in Calendar", url=url)]])
        if reply_to:
            kw["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        try:
            await self.app.bot.send_message(self.settings.allowed_chat_id, text, **kw)
        except Exception:  # noqa: BLE001
            log.exception("Could not send calendar message")

    async def _reply(self, ev: Event, text: str, url: str | None) -> None:
        await self._send(text, ev.telegram_message_id, url)

    async def _warn_access(self) -> None:
        if self.db.get_setting(WARNED_KEY) == "1":
            return
        self.db.set_setting(WARNED_KEY, "1")
        await self._send(f"⚠️ I can't write to the Scout calendar — check it's shared with {self.sa_email} "
                         "with 'Make changes to events'")

    # ---- hooks / jobs --------------------------------------------------------------
    def _spawn(self, coro) -> None:  # noqa: ANN001
        """Run in the background so the button tap is answered without waiting for Google."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for background syncs (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def on_decision(self, event_id: int, old: str, new: str, **_) -> None:  # noqa: ANN003
        if new == "yes" or old == "yes":
            self._spawn(self.sync(event_id, reason="decision"))

    async def on_update(self, event_id: int, changes: list[str] | None = None, _silent: bool = False,
                        **_) -> None:  # noqa: ANN003
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            relevant = ev is not None and (ev.user_decision == "yes" or bool(ev.gcal_event_id))
        if relevant:
            self._spawn(self.sync(event_id, reason="silent" if _silent else "update", changes=changes))

    async def on_booking(self, event_id: int, **_) -> None:  # noqa: ANN003
        self._spawn(self.sync(event_id, reason="booked"))

    async def on_ticket(self, event_id: int, path: str | None = None, **_) -> None:  # noqa: ANN003
        self._spawn(self.attach_ticket(event_id, path))

    async def on_note(self, event_id: int, **_) -> None:  # noqa: ANN003
        await self.on_update(event_id, changes=None, _silent=True)

    async def on_merged(self, event_id: int, merged_id: int | None = None, **_) -> None:  # noqa: ANN003
        if merged_id is not None:
            self._spawn(self.remove_merged(event_id, merged_id))

    async def remove_merged(self, kept_id: int, merged_id: int) -> None:
        """Delete every calendar entry of the folded-in event (looked up in Google if its row is gone)."""
        lock = self._locks.setdefault(merged_id, asyncio.Lock())
        async with lock:
            try:
                ev, _ = self._load(merged_id)
                state = load_state(ev.gcal_event_id) if ev else {"days": {}, "booking": None}
                kept, _h = self._load(kept_id)
                kept_ids = set()
                if kept is not None:
                    ks = load_state(kept.gcal_event_id)
                    kept_ids = set(ks["days"].values()) | {ks["booking"], ks.get(BOOKED)}
                gids = set(state["days"].values()) | {state["booking"], state.get(BOOKED)}
                gids |= {i["id"] for i in await self._find(merged_id)}
                for gid in gids - kept_ids - {None}:
                    await self._delete(gid)
                if ev is not None:
                    with self.db.session() as s:
                        row = s.get(Event, merged_id)
                        if row:
                            row.gcal_event_id = None
                self._set_pending(merged_id, False)
                log.info("Calendar: removed entries of merged event %s (kept %s)", merged_id, kept_id)
            except CalendarError as exc:
                log.warning("Calendar cleanup of merged event %s failed: %s", merged_id, exc)
        await self.on_update(kept_id, _silent=True)

    def register_hooks(self) -> None:
        hooks.on("decision_changed", self.on_decision)
        hooks.on("event_updated", self.on_update)
        hooks.on("booking_confirmed", self.on_booking)
        hooks.on("ticket_added", self.on_ticket)
        hooks.on("note_added", self.on_note)
        hooks.on("events_merged", self.on_merged)

    async def unbooked(self, event_id: int) -> bool:
        """Called after scout.bookings.clear_booking: bring all dates back."""
        return await self.sync(event_id, reason="unbooked")

    async def retry(self) -> int:
        """Retry pending syncs and create entries for ✅ events that gained dates. Returns successes."""
        ids = list(self.pending())
        with self.db.session() as s:
            for ev in s.scalars(select(Event).where(
                Event.user_decision == "yes", Event.gcal_event_id.is_(None), Event.status != "past",
            )):
                if ev.date_list and ev.id not in ids:
                    ids.append(ev.id)
        ok = 0
        for event_id in ids:
            if await self.sync(event_id, reason="retry"):
                ok += 1
            elif self.db.get_setting(WARNED_KEY) == "1":
                break  # calendar unreachable: don't hammer it, try again next round
        return ok

    async def retry_job(self, _ctx=None) -> None:  # noqa: ANN001
        try:
            n = await self.retry()
            if n:
                log.info("Calendar retry: %d synced", n)
        except Exception:  # noqa: BLE001
            log.exception("Calendar retry job failed")

    async def health(self) -> tuple[bool, str]:
        try:
            await self._call("GET", self._events_url, params={"maxResults": 1})
        except CalendarError as exc:
            if exc.access_problem:
                return False, f"Calendar not writable ({exc.status}) — share it with {self.sa_email}"
            return False, f"Calendar unreachable ({exc})"
        n = len(self.pending())
        return True, "Calendar OK" + (f", {n} pending" if n else "")


def setup_calendar(app, settings, db: Database) -> CalendarSync | None:  # noqa: ANN001
    """Register hooks and the retry job. Returns None (feature off) when not configured."""
    if not (settings.gcal_sa_json and settings.gcal_calendar_id):
        log.info("Google Calendar sync disabled (GCAL_SA_JSON / GCAL_CALENDAR_ID not set)")
        return None
    try:
        sync = CalendarSync(settings, db, app)  # scopes: GOOGLE_SCOPES or calendar.events + drive.file
    except Exception as exc:  # noqa: BLE001 — bad JSON must not stop the bot; never log the key itself
        log.error("Google Calendar sync disabled: GCAL_SA_JSON is not a valid service-account key (%s)",
                  type(exc).__name__)
        return None
    sync.register_hooks()
    app.job_queue.run_repeating(sync.retry_job, interval=RETRY_SECONDS, first=60, name="gcal-retry")
    app.bot_data["calendar"] = sync
    log.info("Google Calendar sync on (calendar %s, as %s)", settings.gcal_calendar_id, sync.sa_email)
    return sync

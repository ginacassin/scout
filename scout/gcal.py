"""Google Calendar sync (M2): markets you said ✅ to appear in a dedicated "Scout" calendar.

A service account (GCAL_SA_JSON) writes to a calendar the user shared with it (GCAL_CALENDAR_ID).
Tokens are minted with google-auth's signer and exchanged over httpx; the Calendar REST API v3 is
called directly with httpx (no googleapiclient).

What is stored in Event.gcal_event_id (a JSON string):
    {"days": {"<key>": "<gcal id>", ...}, "booking": "<gcal id>" | null}
where <key> is "2026-11-13" for a timed day or "2026-11-13..2026-11-15" for an all-day span.
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
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy import select

from scout import hooks
from scout.db import Account, Database, Event

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/calendar.events"
API = "https://www.googleapis.com/calendar/v3"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
PENDING_KEY = "gcal_pending"
WARNED_KEY = "gcal_warned"
RETRY_SECONDS = 600
BOOKING = "booking"

BOOKING_NAME = {"eventbrite": "Eventbrite", "other_link": "booking link", "dm": "DM", "free_entry": "Free entry"}


class CalendarError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status

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

    def __init__(self, info: dict[str, Any], client: httpx.AsyncClient):
        from google.auth import jwt
        from google.oauth2 import service_account

        self._jwt = jwt
        self._creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
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
                "iss": self.email, "scope": SCOPE, "aud": self._token_uri, "iat": now, "exp": now + 3600,
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
    blocks.append("Added by Scout")
    return "\n\n".join(blocks)


def _hm(value: str | None) -> tuple[int, int] | None:
    try:
        h, m = (value or "").strip().split(":")[:2]
        return int(h), int(m)
    except ValueError:
        return None


def desired_events(ev: Event, handle: str | None, tz, now: datetime) -> dict[str, dict[str, Any]]:  # noqa: ANN001
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
    if not raw:
        return {"days": {}, "booking": None}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"days": {"legacy": raw}, "booking": None}  # pre-M2 plain id
    if isinstance(data, list):  # tolerate a bare list of ids
        return {"days": {f"legacy{i}": x for i, x in enumerate(data)}, "booking": None}
    return {"days": dict(data.get("days") or {}), "booking": data.get("booking")}


def dump_state(state: dict[str, Any]) -> str | None:
    if not state["days"] and not state["booking"]:
        return None
    return json.dumps({"days": state["days"], "booking": state["booking"]})


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
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---- HTTP ---------------------------------------------------------------
    @property
    def _events_url(self) -> str:
        return f"{API}/calendars/{quote(self.calendar_id, safe='')}/events"

    async def _call(self, method: str, url: str, **kw) -> dict[str, Any]:  # noqa: ANN003
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {await self.tokens.token()}"}
            try:
                resp = await self.client.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                raise CalendarError(None, f"network: {type(exc).__name__}") from exc
            if resp.status_code == 401 and attempt == 1:
                self.tokens.invalidate()  # expired or revoked; mint a fresh one and try once more
                continue
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("error", {}).get("message", "")
                except ValueError:
                    msg = resp.text[:200]
                raise CalendarError(resp.status_code, msg)
            return resp.json() if resp.content else {}
        raise AssertionError("unreachable")

    async def _find(self, event_id: int) -> list[dict[str, Any]]:
        data = await self._call("GET", self._events_url, params={
            "privateExtendedProperty": f"scoutId={event_id}", "maxResults": 50, "showDeleted": "false",
        })
        return [i for i in data.get("items", []) if i.get("status") != "cancelled"]

    async def _insert(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", self._events_url, json=body)

    async def _patch(self, gid: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("PATCH", f"{self._events_url}/{quote(gid, safe='')}", json=body)

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
        want = ev.user_decision == "yes" and bool(ev.date_list)

        if not want:
            items = [(k, v) for k, v in state["days"].items()]
            if state["booking"]:
                items.append((BOOKING, state["booking"]))
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

        desired = desired_events(ev, handle, self.tz, self._now())
        had_any = bool(state["days"])

        # Idempotency: with nothing recorded, adopt whatever Google already has for this scoutId.
        if not state["days"] and not state["booking"]:
            for item in await self._find(event_id):
                key = item.get("extendedProperties", {}).get("private", {}).get("scoutKey") or item["id"]
                if key == BOOKING:
                    state["booking"] = item["id"]
                else:
                    state["days"][key] = item["id"]

        existing: dict[str, str] = dict(state["days"])
        if state["booking"]:
            existing[BOOKING] = state["booking"]
        booking_passed = ev.booking_opens_at is not None and ev.booking_opens_at <= self._now()

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
            if key == BOOKING:
                state["booking"] = res["id"]
            else:
                state["days"][key] = res["id"]
                first_link = first_link or res.get("htmlLink")
            self._save_state(event_id, state)

        log.info("Calendar: event %s synced (%d created, %d updated)", event_id, created, patched)
        if not had_any and state["days"]:
            await self._reply(ev, "📅 Added to your Scout calendar", first_link)
        elif reason == "update" and (created or patched):
            what = "; ".join(changes) if changes else ev.title
            await self._reply(ev, f"📅 Calendar updated: {what}", first_link)

    @staticmethod
    def _drop(state: dict[str, Any], key: str) -> None:
        if key == BOOKING:
            state["booking"] = None
        else:
            state["days"].pop(key, None)

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

    async def on_update(self, event_id: int, changes: list[str] | None = None, **_) -> None:  # noqa: ANN003
        with self.db.session() as s:
            ev = s.get(Event, event_id)
            relevant = ev is not None and (ev.user_decision == "yes" or bool(ev.gcal_event_id))
        if relevant:
            self._spawn(self.sync(event_id, reason="update", changes=changes))

    def register_hooks(self) -> None:
        hooks.on("decision_changed", self.on_decision)
        hooks.on("event_updated", self.on_update)

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
        sync = CalendarSync(settings, db, app)
    except Exception as exc:  # noqa: BLE001 — bad JSON must not stop the bot; never log the key itself
        log.error("Google Calendar sync disabled: GCAL_SA_JSON is not a valid service-account key (%s)",
                  type(exc).__name__)
        return None
    sync.register_hooks()
    app.job_queue.run_repeating(sync.retry_job, interval=RETRY_SECONDS, first=60, name="gcal-retry")
    app.bot_data["calendar"] = sync
    log.info("Google Calendar sync on (calendar %s, as %s)", settings.gcal_calendar_id, sync.sa_email)
    return sync

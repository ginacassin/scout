"""Poll → filter → parse → match/merge → notify."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from . import hooks
from .config import Settings
from .db import Account, Database, Event, Peek, SourceItem, utcnow
from .dedupe import GeminiTieBreaker, Matcher, latest_caption, merge_events, post_key, upgrade_address
from .matching import (DIFFERENT, MAYBE, MergeOutcome, address_specificity, compare, event_status,
                       facts_from_event, facts_from_parsed, parse_dates)
from .media import download_images
from .obs import kv
from .parser.schema import ItemKind, ParsedEvent, ParseResult, Region
from .sources.base import InstagramSource, ProfileSnapshot

log = logging.getLogger(__name__)

PINNED_MAX_AGE = timedelta(days=30)
BACKFILL_MAX_AGE = timedelta(days=10)
FAILURES_BEFORE_WARNING = 3


class Notifier(Protocol):
    async def new_event(self, event: Event, kind: str) -> None: ...
    async def event_update(self, event: Event, changes: list[str]) -> None: ...
    async def warn(self, text: str) -> None: ...


class Parser(Protocol):
    async def parse(self, **kwargs) -> ParseResult: ...  # noqa: ANN003


@dataclass
class Candidate:
    """One post, story or bio, normalised for processing."""

    account: Account
    kind: str  # post | story | bio
    ig_id: str
    url: str | None
    posted_at: datetime | None
    caption: str
    image_urls: list[str]
    location: str | None = None
    coauthors: list[str] | None = None
    link_urls: list[str] | None = None
    pinned: bool = False


def bio_ig_id(handle: str, bio: str, links: list[str]) -> str:
    digest = hashlib.sha1((bio + "|" + "|".join(sorted(links))).encode()).hexdigest()[:12]
    return f"bio:{handle}:{digest}"


STORIES_AT_KEY = "stories_polled_at"


class Scout:
    def __init__(self, settings: Settings, db: Database, source: InstagramSource, parser: Parser, notifier: Notifier,
                 tiebreaker=None):  # noqa: ANN001
        self.settings = settings
        self.db = db
        self.source = source
        self.parser = parser
        self.notifier = notifier
        # Duplicate detection: rules first, Gemini only for inconclusive pairs (same client/model as the parser).
        self.matcher = Matcher(db, tiebreaker if tiebreaker is not None else GeminiTieBreaker.from_parser(parser))

    # ------------------------------------------------------------------ polling
    async def poll_profiles(self, handles: list[str] | None = None) -> dict[str, int]:
        accounts = self.db.active_accounts()
        if handles is not None:
            accounts = [a for a in accounts if a.handle in handles]
        if not accounts:
            return {}
        snaps = await self.source.profiles([a.handle for a in accounts])
        missing = [a.handle for a in accounts if a.handle not in snaps]
        log.info("poll profiles %s", kv(accounts=len(accounts), returned=len(snaps),
                                        missing=",".join(missing) or None))
        counts: dict[str, int] = {}
        for acc in accounts:
            snap = snaps.get(acc.handle)
            if snap is None:
                await self._record_failure(acc)
                continue
            counts[acc.handle] = await self._process_profile(acc, snap)
        log.info("poll profiles done %s", kv(processed=sum(counts.values()),
                                             **{f"@{h}": n for h, n in counts.items() if n}))
        self.settings.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings.heartbeat_file.touch()
        return counts

    async def poll_stories(self, handles: list[str] | None = None) -> int:
        """Read current stories (they live 24 h) of all active accounts, or only of `handles`."""
        accounts = {a.handle: a for a in self.db.active_accounts()}
        if handles is not None:
            wanted = {h.lower() for h in handles}
            accounts = {h: a for h, a in accounts.items() if h.lower() in wanted}
        if not accounts:
            return 0
        stories = await self.source.stories(list(accounts))
        log.info("poll stories %s", kv(accounts=len(accounts), stories=len(stories)))
        if handles is None:
            self.db.set_setting(STORIES_AT_KEY, utcnow().isoformat())
        new = 0
        for st in stories:
            acc = accounts.get(st.owner)
            if not acc:
                continue
            new += await self.process(
                Candidate(
                    account=acc,
                    kind="story",
                    ig_id=st.ig_id,
                    url=st.url,
                    posted_at=st.posted_at,
                    caption="",
                    image_urls=[st.image_url] if st.image_url else [],
                    link_urls=st.link_urls,
                )
            )
        return new

    async def _record_failure(self, acc: Account) -> None:
        with self.db.session() as s:
            a = s.get(Account, acc.id)
            a.consecutive_failures += 1
            a.last_polled_at = utcnow()
            failures = a.consecutive_failures
        if failures == FAILURES_BEFORE_WARNING:
            await self.notifier.warn(f"⚠️ Can't read @{acc.handle}: {failures} checks in a row came back empty.")

    async def _process_profile(self, acc: Account, snap: ProfileSnapshot) -> int:
        bio_changed = snap.biography != (acc.last_bio or "") or snap.bio_links != acc.bio_links
        with self.db.session() as s:
            a = s.get(Account, acc.id)
            a.consecutive_failures = 0
            a.last_polled_at = a.last_ok_at = utcnow()
            a.full_name = snap.full_name
            a.last_bio = snap.biography
            a.last_bio_links = json.dumps(snap.bio_links)
            acc = a
        processed = 0
        if bio_changed and snap.biography.strip():
            processed += await self.process(
                Candidate(
                    account=acc,
                    kind="bio",
                    ig_id=bio_ig_id(acc.handle, snap.biography, snap.bio_links),
                    url=f"https://www.instagram.com/{acc.handle}/",
                    posted_at=utcnow(),
                    caption=snap.biography,
                    image_urls=[],
                    link_urls=snap.bio_links,
                )
            )
        # Oldest first, so a teaser is known before its announcement.
        for post in sorted(snap.posts, key=lambda p: p.posted_at or datetime.min.replace(tzinfo=timezone.utc)):
            processed += await self.process(
                Candidate(
                    account=acc,
                    kind="post",
                    ig_id=post.ig_id,
                    url=post.url,
                    posted_at=post.posted_at,
                    caption=post.caption,
                    image_urls=post.image_urls,
                    location=post.location,
                    coauthors=post.coauthors,
                    pinned=post.is_pinned,
                )
            )
        return processed

    # --------------------------------------------------------------- processing
    def prefilter(self, c: Candidate, now: datetime) -> str | None:
        """Reason to skip without calling Gemini, or None."""
        age = now - c.posted_at if c.posted_at else timedelta(0)
        if c.pinned and age > PINNED_MAX_AGE:
            return "old_pinned"
        if c.kind == "post" and age > BACKFILL_MAX_AGE:
            return "old"
        return None

    def _seen(self, ig_id: str) -> bool:
        with self.db.session() as s:
            return s.scalar(select(SourceItem.id).where(SourceItem.ig_id == ig_id)) is not None

    async def process(self, c: Candidate) -> int:
        if self._seen(c.ig_id):
            log.debug("item seen %s", kv(ig_id=c.ig_id))
            return 0
        now = utcnow()
        reason = self.prefilter(c, now)
        if reason:
            log.info("item %s", kv(ig_id=c.ig_id, kind=c.kind, account=c.account.handle, skipped=reason))
            with self.db.session() as s:
                s.add(SourceItem(account_id=c.account.id, ig_id=c.ig_id, kind=c.kind, posted_at=c.posted_at,
                                 url=c.url, caption=c.caption, skipped_reason=reason))
            return 0

        images: list[Path] = await download_images(c.image_urls, self.settings.media_dir, c.ig_id)
        tz = self.settings.tz
        posted_local = (c.posted_at or now).astimezone(tz)
        try:
            result = await self.parser.parse(
                handle=c.account.handle,
                item_kind=c.kind,
                posted_at_local=posted_local.strftime("%A %Y-%m-%d %H:%M"),
                today_local=now.astimezone(tz).strftime("%A %Y-%m-%d"),
                caption=c.caption,
                images=images,
                location=c.location,
                coauthors=c.coauthors,
                bio=c.account.last_bio if c.kind != "bio" else None,
                bio_links=c.account.bio_links,
                link_urls=c.link_urls,
            )
        except Exception:  # noqa: BLE001 - leave unseen so the next poll retries it
            log.exception("Parse failed for %s", c.ig_id)
            return 0

        with self.db.session() as s:
            item = SourceItem(
                account_id=c.account.id, ig_id=c.ig_id, kind=c.kind, posted_at=c.posted_at, url=c.url,
                caption=c.caption, media_paths=json.dumps([str(p) for p in images]),
                parsed_json=result.model_dump_json(), classification=result.kind.value,
            )
            s.add(item)
            s.flush()
            item_id = item.id

        if result.kind == ItemKind.unrelated or not result.events:
            log.info("item %s", kv(ig_id=c.ig_id, kind=c.kind, account=c.account.handle,
                                   classification=result.kind.value, events=0,
                                   items=len(result.items_for_sale) or None))
            # Photos of pieces often come without text and get called "unrelated": the named items decide.
            self.store_peeks(c, result.items_for_sale, images, [], now)
            return 1

        event_ids = []
        for parsed in result.events:
            ev_id = await self.handle_event(c, parsed, result.kind, images, now)
            if ev_id:
                event_ids.append(ev_id)
        with self.db.session() as s:
            s.get(SourceItem, item_id).event_ids = json.dumps(event_ids)
        self.store_peeks(c, result.items_for_sale, images, event_ids, now)
        log.info("item %s", kv(ig_id=c.ig_id, kind=c.kind, account=c.account.handle, classification=result.kind.value,
                               events=len(result.events), event_ids=",".join(map(str, event_ids)) or None))
        return 1

    # ------------------------------------------------------------ sneak peeks
    def upcoming_event_for(self, account_id: int, now: datetime) -> int | None:
        """The market an item photo most likely belongs to: the account's next dated market, else its
        newest dateless teaser (from the last 30 days)."""
        today = now.astimezone(self.settings.tz).date()
        with self.db.session() as s:
            evs = list(s.scalars(select(Event).where(Event.account_id == account_id, Event.status != "past",
                                                     Event.user_decision.not_in(("filtered",)))))
        dated = [e for e in evs if e.date_list and max(e.date_list) >= today]
        if dated:
            return min(dated, key=lambda e: (min(d for d in e.date_list if d >= today), e.id)).id
        recent = [e for e in evs if not e.date_list and e.created_at and now - e.created_at < timedelta(days=30)]
        return max(recent, key=lambda e: e.created_at).id if recent else None

    def store_peeks(self, c: Candidate, items: list[str], images: list[Path], event_ids: list[int],
                    now: datetime) -> int:
        """Keep photos of pieces for sale; scout/peeks.py sends them in albums. Without a known market
        they wait (event_id NULL) until the account announces one."""
        if not items or not images:
            return 0
        target = event_ids[0] if event_ids else self.upcoming_event_for(c.account.id, now)
        with self.db.session() as s:
            if target is not None:
                ev = s.get(Event, target)
                if ev is None or ev.user_decision in ("no", "filtered"):
                    return 0
            for img in images[:10]:
                s.add(Peek(account_id=c.account.id, event_id=target, ig_id=c.ig_id, url=c.url,
                           image_path=str(img), items=json.dumps(items[:5], ensure_ascii=False), created_at=now))
        log.info("peeks %s", kv(ig_id=c.ig_id, account=c.account.handle, event_id=target, photos=min(len(images), 10)))
        return min(len(images), 10)

    def _candidates(self, s, c: Candidate, parsed: ParsedEvent) -> tuple[list[Event], set[str], dict[int, str]]:  # noqa: ANN001
        """Open events of the same account, a collab partner or the named organizer; the brand handles
        of that group; and account_id -> handle."""
        handles = {c.account.handle, *(c.coauthors or [])}
        if parsed.organizer:
            handles.add(parsed.organizer.lstrip("@").lower())
        acc_ids = list(s.scalars(select(Account.id).where(Account.handle.in_(handles))))
        q = select(Event).where(Event.status != "past")
        events = list(s.scalars(q))
        cands = [e for e in events if e.account_id in acc_ids or (e.organizer and e.organizer.lower() in handles)]
        names = {a.id: a.handle for a in s.scalars(select(Account).where(Account.id.in_({e.account_id for e in cands})))}
        brands = handles | {e.organizer for e in cands if e.organizer} | set(names.values())
        return cands, brands, names

    async def handle_event(self, c: Candidate, parsed: ParsedEvent, kind: ItemKind, images: list[Path], now: datetime) -> int | None:
        today = now.astimezone(self.settings.tz).date()
        pdates = parse_dates(parsed.dates)
        with self.db.session() as s:
            cands, brands, names = self._candidates(s, c, parsed)
        new = facts_from_parsed(parsed, brands, account=c.account.handle, now=now)
        cand_facts = {e.id: facts_from_event(e, brands, account=names.get(e.account_id)) for e in cands}
        found = await self.matcher.resolve(new, [(cand_facts[e.id], e) for e in cands], now,
                                           key=post_key(c.ig_id, parsed.title), caption=c.caption)
        match = min((r.event for r in found), key=lambda e: (e.created_at, e.id)) if found else None
        if match is not None:
            # Only fold in other matches that are not themselves clearly a different market from the target.
            found = [r for r in found if r.event.id == match.id or compare(
                cand_facts[r.event.id], cand_facts[match.id], r.event.created_at or now).verdict != DIFFERENT]
        if found:
            log.info("match %s", kv(ig_id=c.ig_id, title=parsed.title[:60], event_id=match.id,
                                    via=",".join(f"{r.event.id}:{r.via}" for r in found)))
        merged: list[int] = []
        with self.db.session() as s:
            if match is not None:
                ev = s.get(Event, match.id)
                # The post ties together events Scout stored separately: fold them into the oldest.
                for r in found:
                    if r.event.id != ev.id:
                        dup = s.get(Event, r.event.id)
                        if dup is not None and dup.status != "past":
                            merge_events(s, ev, dup)
                            merged.append(dup.id)
                outcome = merge_into(ev, parsed, kind, self.settings.tz)
                if c.url and not ev.source_url:
                    ev.source_url = c.url
                notify = outcome.significant and ev.user_decision in ("yes", "maybe")
                ev_id = ev.id
                ev_snapshot = ev
            else:
                if pdates and max(pdates) < today:
                    log.info("event dropped %s", kv(ig_id=c.ig_id, title=parsed.title[:60], reason="already_over"))
                    return None  # announcement of something already over
                if kind in (ItemKind.reminder, ItemKind.update, ItemKind.last_day) and not pdates:
                    log.info("event dropped %s", kv(ig_id=c.ig_id, title=parsed.title[:60], reason="followup_unseen"))
                    return None  # follow-up of a market Scout never saw; too little to alert on
                ev = new_event_from(parsed, c, images, self.settings.tz)
                ev.created_at = now
                filtered = parsed.region == Region.outside and self.settings.region_filter == "madrid"
                if filtered:
                    ev.user_decision = "filtered"
                s.add(ev)
                s.flush()
                ev_id = ev.id
                ev_snapshot = ev
                notify = not filtered
                outcome = None

        for dup_id in merged:
            await hooks.emit("events_merged", ev_id, merged_id=dup_id)
        alert = bool(notify and (match is None or outcome))
        log.info("event %s", kv(ig_id=c.ig_id, action="created" if match is None else "matched", event_id=ev_id,
                                decision=ev_snapshot.user_decision, changes="; ".join(outcome.changes) if outcome and
                                outcome.changes else None, alert=alert))
        if match is None and notify:
            await self.notifier.new_event(ev_snapshot, kind.value)
        elif match is not None and notify and outcome:
            await self.notifier.event_update(ev_snapshot, outcome.changes)
        if match is None:
            await hooks.emit("event_created", ev_id, kind=kind.value)
        elif outcome and outcome.significant:
            await hooks.emit("event_updated", ev_id, changes=outcome.changes)
        return ev_id

    # ----------------------------------------------------------------- upkeep
    async def dedupe_events(self) -> int:
        """Merge open events that are the same market, with the same rules as live matching plus the
        Gemini tie-breaker for inconclusive pairs. The oldest event is kept; each merge emits
        "events_merged" so the calendar/booking modules can drop the duplicate's entries."""
        now = utcnow()
        with self.db.session() as s:
            events = list(s.scalars(select(Event).where(Event.status != "past").order_by(Event.created_at, Event.id)))
            names = {a.id: a.handle for a in s.scalars(select(Account))}

        def group(e: Event) -> set[str]:
            return {h for h in (names.get(e.account_id), (e.organizer or "").lower()) if h}

        gone: set[int] = set()
        pairs: list[tuple[int, int]] = []
        for i in range(len(events)):
            keep = events[i]
            if keep.id in gone:
                continue
            for dup in events[i + 1:]:
                if dup.id in gone or not (dup.account_id == keep.account_id or group(dup) & group(keep)):
                    continue
                brands = group(keep) | group(dup)
                kf = facts_from_event(keep, brands, account=names.get(keep.account_id))
                df = facts_from_event(dup, brands, account=names.get(dup.account_id))
                cmp = compare(df, kf, dup.created_at or now)
                if cmp.verdict == DIFFERENT:
                    continue
                caption = None
                if cmp.verdict == MAYBE:
                    with self.db.session() as s:
                        caption = latest_caption(s, dup.id)
                same, via = await self.matcher.decide(cmp, kf, df, keep.id, f"ev:{dup.id}", caption)
                log.info("dedupe pair %s", kv(keep=keep.id, dup=dup.id, verdict=cmp.verdict, score=round(cmp.score, 2),
                                              same=same, via=via))
                if not same:
                    continue
                with self.db.session() as s:
                    merge_events(s, s.get(Event, keep.id), s.get(Event, dup.id))
                with self.db.session() as s:
                    keep = events[i] = s.get(Event, keep.id)  # refreshed (address/dates may have grown)
                gone.add(dup.id)
                pairs.append((keep.id, dup.id))
        for keep_id, dup_id in pairs:
            await hooks.emit("events_merged", keep_id, merged_id=dup_id)
        return len(pairs)

    def mark_past_events(self) -> int:
        today = datetime.now(self.settings.tz).date()
        n = 0
        with self.db.session() as s:
            for ev in s.scalars(select(Event).where(Event.status != "past")):
                ds = ev.date_list
                if ds and max(ds) < today:
                    ev.status = "past"
                    n += 1
                elif not ds and (utcnow() - ev.created_at) > timedelta(days=60):
                    ev.status = "past"  # teaser that never materialised
                    n += 1
        return n


# ---------------------------------------------------------------- event helpers
def _opens_at(value: str | None, tz) -> datetime | None:  # noqa: ANN001
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def new_event_from(parsed: ParsedEvent, c: Candidate, images: list[Path], tz) -> Event:  # noqa: ANN001
    dates = parse_dates(parsed.dates)
    ev = Event(
        account_id=c.account.id,
        title=parsed.title[:300],
        address=parsed.address,
        area=parsed.area,
        region=parsed.region.value,
        organizer=(parsed.organizer or "").lstrip("@") or None,
        booking_method=parsed.booking_method.value,
        booking_url=parsed.booking_url,
        booking_opens_at=_opens_at(parsed.booking_opens_at, tz),
        booking_scope=parsed.booking_scope,
        booking_status=parsed.booking_status.value,
        link_in_bio=parsed.link_in_bio,
        price=parsed.price,
        summary=parsed.summary_en,
        status=event_status(dates, parsed.address),
        source_url=c.url,
        media_path=str(images[0]) if images else None,
    )
    ev.date_list = dates
    ev.session_list = [sess.model_dump() for sess in parsed.sessions]
    return ev


def merge_into(ev: Event, parsed: ParsedEvent, kind: ItemKind, tz) -> MergeOutcome:  # noqa: ANN001
    """Fill gaps and record what changed. Never blanks a field Scout already knows."""
    out = MergeOutcome()
    new_dates = parse_dates(parsed.dates)
    if new_dates and not ev.date_list:
        ev.date_list = new_dates
        out.changes.append("date announced: " + ", ".join(d.strftime("%a %d %b") for d in new_dates))
    elif new_dates and set(new_dates) - set(ev.date_list) and kind == ItemKind.update:
        ev.date_list = sorted(set(ev.date_list) | set(new_dates))
        out.changes.append("dates extended")
    if parsed.address:
        before = ev.address
        addr, changed = upgrade_address(ev.address, parsed.address)
        if changed:
            ev.address = addr
            old_spec, new_spec = address_specificity(before), address_specificity(addr)
            if old_spec < 2:
                out.changes.append(f"address: {addr}")
            elif new_spec > old_spec:
                out.changes.append(f"exact address: {addr}")  # street known before, door number now
            # same detail, fuller wording: stored silently
    if parsed.area and not ev.area:
        ev.area = parsed.area
    if parsed.sessions and parsed.sessions != [] and json_sessions(parsed) != ev.sessions:
        had = bool(ev.session_list)
        ev.session_list = [sess.model_dump() for sess in parsed.sessions]
        if had and kind == ItemKind.update:
            out.changes.append("opening hours updated")
    if parsed.booking_method.value not in ("unknown",) and ev.booking_method in ("unknown", "free_entry") and parsed.booking_method.value != ev.booking_method:
        ev.booking_method = parsed.booking_method.value
        out.changes.append(f"booking: {parsed.booking_method.value.replace('_', ' ')}")
    if parsed.booking_url and parsed.booking_url != ev.booking_url:
        ev.booking_url = parsed.booking_url
        out.changes.append("booking link posted")
    opens = _opens_at(parsed.booking_opens_at, tz)
    if opens and opens != ev.booking_opens_at:
        ev.booking_opens_at = opens
        out.changes.append("booking opens " + opens.astimezone(tz).strftime("%a %d %b %H:%M"))
    if parsed.booking_scope and not ev.booking_scope:
        ev.booking_scope = parsed.booking_scope
    if parsed.booking_status.value in ("sold_out", "cancellations") and parsed.booking_status.value != ev.booking_status:
        ev.booking_status = parsed.booking_status.value
        out.changes.append("tickets available again (cancellations)" if parsed.booking_status.value == "cancellations" else "booking sold out")
    if kind == ItemKind.last_day and parsed.change:
        out.changes.append(parsed.change)
    elif kind == ItemKind.update and parsed.change and not out.changes:
        out.changes.append(parsed.change)
    if parsed.link_in_bio:
        ev.link_in_bio = True
    if parsed.region.value != "unknown" and ev.region == "unknown":
        ev.region = parsed.region.value
    ev.status = event_status(ev.date_list, ev.address) if ev.status != "past" else "past"
    for ch in out.changes:
        ev.log_change(ch)
    return out


def json_sessions(parsed: ParsedEvent) -> str:
    return json.dumps([s.model_dump() for s in parsed.sessions], ensure_ascii=False)

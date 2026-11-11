"""Duplicate markets: the Gemini tie-breaker, its verdict cache, and folding one stored event into another.

`matching.compare` decides most pairs on its own. Only the inconclusive ones ("maybe": same account,
overlapping dates or a recent teaser, but different wording/places) go to the tie-breaker, and each
(event, post) pair is asked at most once. When Gemini is unavailable the deterministic lean decides.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .db import BookingWatch, Database, Event, SourceItem, UTCDateTime, utcnow
from .matching import (
    MAYBE,
    SAME,
    Comparison,
    Facts,
    address_contains,
    address_specificity,
    event_status,
    is_estate_only,
    rank,
)
from .obs import STATS, kv

log = logging.getLogger(__name__)

DECISION_RANK = {"yes": 3, "maybe": 2, "no": 1, "pending": 0, "filtered": -1}
MIN_CONFIDENCE = 0.6
MAX_TIEBREAKS_PER_ITEM = 3


# ------------------------------------------------------------------ tie-breaker
class SameMarketVerdict(BaseModel):
    same_market: bool = Field(description="True if both describe the same real-world market (same place and days)")
    confidence: float = Field(description="0-1")
    reason: str = Field(description="One short English sentence")


class TieBreaker(Protocol):
    async def same_market(self, a: dict, b: dict, caption: str | None) -> SameMarketVerdict | None: ...


TIEBREAK_PROMPT = """\
You compare two vintage-market / house-clearance-sale announcements from Instagram accounts in Spain
and decide whether they are the SAME real-world market (one event announced several times) or two
DIFFERENT markets. Organizers first post teasers (dates or area only), then re-announce the same market
with more detail (full street address, hours, booking link); titles are machine-written and worded
differently each time. An estate/area name and a street inside it can be the same place ("Polígono
Cobo Calleja" and "C. Villafranca del Bierzo 56, Fuenlabrada"). Different towns on the same day,
different regions, or the same venue on new dates ("second round") are DIFFERENT markets.
Answer JSON only."""


class GeminiTieBreaker:
    """Text-only, tiny structured call on the parser's client and model."""

    def __init__(self, client, model: str):  # noqa: ANN001
        self.client = client
        self.model = model

    @classmethod
    def from_parser(cls, parser) -> "GeminiTieBreaker | None":  # noqa: ANN001
        client, model = getattr(parser, "client", None), getattr(parser, "model", None)
        return cls(client, model) if client is not None and model else None

    async def same_market(self, a: dict, b: dict, caption: str | None) -> SameMarketVerdict | None:
        from google.genai import types

        text = (
            "MARKET A (already known):\n" + json.dumps(a, ensure_ascii=False, default=str)
            + "\n\nMARKET B (newer):\n" + json.dumps(b, ensure_ascii=False, default=str)
            + ("\n\nNEWEST POST CAPTION:\n" + caption.strip()[:1500] if caption and caption.strip() else "")
        )
        config = types.GenerateContentConfig(
            system_instruction=TIEBREAK_PROMPT, response_mime_type="application/json",
            response_schema=SameMarketVerdict, temperature=0.0,
        )
        from .parser.gemini import generate
        # Same per-model free-tier budgets and fallback models as the parser; one round only.
        resp, model, latency = await generate(self.client, self.model, [text], config, {"task": "tiebreak"},
                                              rounds=1)
        usage = getattr(resp, "usage_metadata", None)
        total = getattr(usage, "total_token_count", None)
        if isinstance(total, int):
            STATS.incr("gemini_tokens", total)
        log.info("gemini tiebreak %s", kv(model=model, latency_ms=latency, total_tokens=total))
        if isinstance(resp.parsed, SameMarketVerdict):
            return resp.parsed
        return SameMarketVerdict.model_validate_json(resp.text or "{}")


# ------------------------------------------------------------------ verdict cache
class _CacheBase(DeclarativeBase):
    pass


class MatchVerdict(_CacheBase):
    __tablename__ = "match_verdicts"
    __table_args__ = (UniqueConstraint("event_id", "other_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    other_key: Mapped[str] = mapped_column(String(200))  # "<ig_id>#<title hash>" or "ev:<event id>"
    same: Mapped[bool] = mapped_column(Boolean)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


def post_key(ig_id: str, title: str) -> str:
    """One post can hold several markets (roundups), so the parsed title is part of the key."""
    return f"{ig_id}#{hashlib.sha1(title.encode()).hexdigest()[:8]}"


@dataclass
class Resolution:
    event: Event
    comparison: Comparison
    via: str  # rules | gemini | cache | fallback


class Matcher:
    """Deterministic comparison first; Gemini only for the inconclusive zone."""

    def __init__(self, db: Database, tiebreaker: TieBreaker | None = None):
        self.db = db
        self.tiebreaker = tiebreaker
        MatchVerdict.__table__.create(db.engine, checkfirst=True)

    # cache --------------------------------------------------------------
    def _cached(self, event_id: int, key: str) -> MatchVerdict | None:
        with self.db.session() as s:
            return s.scalar(select(MatchVerdict).where(MatchVerdict.event_id == event_id, MatchVerdict.other_key == key))

    def _store(self, event_id: int, key: str, v: SameMarketVerdict) -> None:
        with self.db.session() as s:
            if s.scalar(select(MatchVerdict.id).where(MatchVerdict.event_id == event_id, MatchVerdict.other_key == key)):
                return
            s.add(MatchVerdict(event_id=event_id, other_key=key, same=bool(v.same_market),
                               confidence=float(v.confidence or 0), reason=(v.reason or "")[:500]))

    async def decide(self, cmp: Comparison, old: Facts, new: Facts, event_id: int, key: str,
                     caption: str | None) -> tuple[bool, str]:
        """(same?, via) for one pair."""
        if cmp.verdict == SAME:
            return True, "rules"
        if cmp.verdict != MAYBE:
            return False, "rules"
        hit = self._cached(event_id, key)
        if hit is not None:
            return (hit.same if hit.confidence >= MIN_CONFIDENCE else cmp.lean), "cache"
        if self.tiebreaker is None:
            return cmp.lean, "fallback"
        try:
            v = await self.tiebreaker.same_market(old.describe(), new.describe(), caption)
        except Exception as exc:  # noqa: BLE001 - never let the tie-breaker break the pipeline
            log.warning("tiebreak failed %s: %s", kv(event_id=event_id, key=key), exc)
            return cmp.lean, "fallback"
        if v is None:
            return cmp.lean, "fallback"
        self._store(event_id, key, v)
        log.info("tiebreak %s", kv(event_id=event_id, key=key, same=v.same_market, confidence=v.confidence,
                                   score=round(cmp.score, 2), reason=v.reason[:80]))
        return (bool(v.same_market) if v.confidence >= MIN_CONFIDENCE else cmp.lean), "gemini"

    async def resolve(self, new: Facts, candidates: Sequence[tuple[Facts, Event]], now: datetime,
                      key: str, caption: str | None) -> list[Resolution]:
        """Every candidate that is the same market as `new`. Rule-certain ones first; the tie-breaker
        is only asked when no candidate is certain, at most MAX_TIEBREAKS_PER_ITEM times."""
        ranked = rank(new, candidates, now)
        by_id = {ev.id: f for f, ev in candidates}
        sure = [Resolution(ev, cmp, "rules") for cmp, ev in ranked if cmp.verdict == SAME]
        if sure:
            return sure
        out: list[Resolution] = []
        asked = 0
        for cmp, ev in ranked:
            if asked >= MAX_TIEBREAKS_PER_ITEM:
                if cmp.lean:
                    out.append(Resolution(ev, cmp, "fallback"))
                continue
            same, via = await self.decide(cmp, by_id[ev.id], new, ev.id, key, caption)
            asked += via == "gemini"
            if same:
                out.append(Resolution(ev, cmp, via))
                break  # one confirmed match is enough for an inconclusive post
        return out


# ------------------------------------------------------------------ event merge
def _more_specific(new: str | None, old: str | None) -> bool:
    if not new:
        return False
    if not old:
        return True
    sn, so = address_specificity(new), address_specificity(old)
    if sn != so:
        return sn > so
    return len(new) > len(old) and address_contains(new, old)


def upgrade_address(current: str | None, offered: str | None) -> tuple[str | None, bool]:
    """(address to keep, changed?). An estate-only address is kept as a prefix of the street address
    ("Polígono industrial Cobo Calleja, C. Villafranca del Bierzo, 56")."""
    if not _more_specific(offered, current):
        return current, False
    if current and is_estate_only(current) and not address_contains(offered, current):
        return f"{current}, {offered}"[:300], True
    return offered[:300], True


def merge_events(s: Session, keep: Event, dup: Event) -> list[str]:
    """Fold `dup` into `keep` inside session `s`. `keep` keeps its id, card, calendar ids and the
    strongest answer. Returns what changed on `keep`. The caller emits "events_merged" after commit."""
    changes: list[str] = []
    addr, changed = upgrade_address(keep.address, dup.address)
    if changed:
        keep.address = addr
        changes.append(f"address: {addr}")
    if not keep.date_list and dup.date_list:
        keep.date_list = dup.date_list
        changes.append("dates")
    if len(dup.session_list) > len(keep.session_list):
        keep.session_list = dup.session_list
    for col in ("area", "booking_url", "booking_opens_at", "booking_scope", "price", "organizer", "source_url",
                "media_path", "summary", "telegram_message_id"):
        if getattr(keep, col) in (None, "") and getattr(dup, col) not in (None, ""):
            setattr(keep, col, getattr(dup, col))
    if keep.booking_method in ("unknown", "free_entry") and dup.booking_method not in ("unknown", "free_entry"):
        keep.booking_method = dup.booking_method
        changes.append(f"booking: {dup.booking_method}")
    if dup.booking_status not in ("unknown", None) and (keep.booking_status == "unknown"
                                                       or (dup.created_at or utcnow()) > (keep.created_at or utcnow())):
        keep.booking_status = dup.booking_status
    if keep.region == "unknown" and dup.region != "unknown":
        keep.region = dup.region
    keep.link_in_bio = bool(keep.link_in_bio or dup.link_in_bio)
    if DECISION_RANK.get(dup.user_decision, 0) > DECISION_RANK.get(keep.user_decision, 0):
        keep.user_decision = dup.user_decision
    notes = keep.note_list + [n for n in dup.note_list if n not in keep.note_list]
    if len(notes) != len(keep.note_list):
        keep.notes = json.dumps(sorted(notes, key=lambda n: n.get("at") or ""), ensure_ascii=False)
    if keep.booked_start is None and dup.booked_start is not None:
        keep.booked_start, keep.booked_end, keep.booked_via = dup.booked_start, dup.booked_end, dup.booked_via
        if not keep.ticket_path:
            keep.ticket_path = dup.ticket_path
    elif not keep.ticket_path and dup.ticket_path:
        keep.ticket_path = dup.ticket_path
    if keep.status != "past":
        keep.status = event_status(keep.date_list, keep.address)
    keep.log_change(f"merged duplicate event {dup.id}")

    for item in s.scalars(select(SourceItem).where(SourceItem.event_ids.contains(str(dup.id)))):
        ids = json.loads(item.event_ids or "[]")
        if dup.id in ids:
            item.event_ids = json.dumps(sorted({keep.id if x == dup.id else x for x in ids}))
    keep_watch = s.scalar(select(BookingWatch).where(BookingWatch.event_id == keep.id))
    for w in list(s.scalars(select(BookingWatch).where(BookingWatch.event_id == dup.id))):
        if keep_watch is not None:
            s.delete(w)
        else:
            w.event_id = keep.id
            keep_watch = w
    s.flush()

    dup.status = "past"
    dup.user_decision = "filtered"
    dup.log_change(f"duplicate of {keep.id}")
    log.info("events merged %s", kv(keep=keep.id, dup=dup.id, title=keep.title[:60], changes=len(changes)))
    return changes


def latest_caption(s: Session, event_id: int) -> str | None:
    rows = [r for r in s.scalars(select(SourceItem).where(SourceItem.event_ids.contains(str(event_id)))
                                  .order_by(SourceItem.posted_at.desc()))
            if event_id in json.loads(r.event_ids or "[]")]
    return rows[0].caption if rows else None

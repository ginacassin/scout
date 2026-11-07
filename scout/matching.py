"""Decide whether a parsed event is new or belongs to one Scout already knows, and merge it.

Pure functions over plain values so they can be unit-tested without a database.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol, Sequence

from .parser.schema import ParsedEvent

STOPWORDS = {
    "market", "mercadillo", "mercadillos", "en", "casa", "de", "del", "la", "el", "the", "a", "in",
    "puertas", "abiertas", "open", "doors", "house", "sale", "flat", "piso", "madrid", "vintage",
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def tokens(text: str | None) -> set[str]:
    return {t for t in _norm(text or "").split() if len(t) > 2 and t not in STOPWORDS}


def similarity(a: str | None, b: str | None) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def parse_dates(values: Sequence[str]) -> list[date]:
    out = []
    for v in values:
        try:
            out.append(date.fromisoformat(v[:10]))
        except ValueError:
            continue
    return sorted(set(out))


class EventLike(Protocol):
    id: int
    title: str
    address: str | None
    area: str | None
    created_at: datetime

    @property
    def date_list(self) -> list[date]: ...


def find_match(parsed: ParsedEvent, candidates: Sequence[EventLike], now: datetime) -> EventLike | None:
    """Candidates are the open events of the same account (or a collab partner)."""
    pdates = set(parse_dates(parsed.dates))
    best: tuple[float, EventLike] | None = None

    for ev in candidates:
        edates = set(ev.date_list)
        score = 0.0
        if pdates and edates:
            if not (pdates & edates):
                # Same venue on new dates is a new event ("segunda vuelta").
                continue
            score += 2.0
        # Different places on the same weekend are different markets (roundup posts, clashes).
        if parsed.address and ev.address and similarity(parsed.address, ev.address) < 0.3:
            continue
        if not (parsed.address and ev.address) and parsed.area and ev.area and similarity(parsed.area, ev.area) < 0.3:
            continue
        title_sim = max(similarity(parsed.title, ev.title), similarity(parsed.address, ev.address))
        score += title_sim
        if parsed.address and ev.address and similarity(parsed.address, ev.address) >= 0.5:
            score += 1.0
        if parsed.area and ev.area and similarity(parsed.area, ev.area) >= 0.5:
            score += 0.5
        if not edates and pdates:
            # A dated announcement after a dateless teaser from the same account = its confirmation,
            # if the teaser is recent.
            if now - ev.created_at <= timedelta(days=21):
                score += 1.0
        if not pdates and not edates and now - ev.created_at <= timedelta(days=14):
            # Two dateless teasers in two weeks from one account are almost always the same market.
            score += 1.0
        if not pdates and edates and title_sim == 0 and not (parsed.area and ev.area):
            # A dateless post with nothing in common with a dated event.
            continue
        if score >= 1.0 and (best is None or score > best[0]):
            best = (score, ev)
    return best[1] if best else None


@dataclass
class MergeOutcome:
    changes: list[str] = field(default_factory=list)  # human-readable, English

    @property
    def gained_date(self) -> bool:
        return any(c.startswith("date") for c in self.changes)

    @property
    def gained_address(self) -> bool:
        return any(c.startswith("address") for c in self.changes)

    @property
    def significant(self) -> bool:
        return bool(self.changes)


def event_status(dates: list[date], address: str | None) -> str:
    if not dates:
        return "teaser"
    return "confirmed" if address else "announced"

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


# ----------------------------------------------------------------- place words
# Street-type words, building/floor words and fillers that say nothing about *which* street.
STREET_WORDS = {
    "calle", "c", "cl", "cll", "avenida", "av", "avda", "avd", "plaza", "pza", "pl", "paseo", "po", "pso",
    "camino", "cno", "carretera", "ctra", "ronda", "glorieta", "travesia", "trv", "costanilla", "cuesta",
    "via", "pasaje", "callejon", "bulevar", "urbanizacion", "urb", "poligono", "pol", "ind", "industrial",
    "nave", "naves", "esquina", "esq", "escalera", "esc", "piso", "planta", "pta", "puerta", "derecha",
    "dcha", "izquierda", "izda", "izq", "edificio", "edif", "bajo", "bajos", "atico", "local", "portal",
    "numero", "num", "no", "sn", "s", "n", "y", "e", "de", "del", "la", "las", "el", "los", "en", "con",
    "entre", "junto", "a", "al", "comunidad", "spain", "espana", "centro", "comercial", "parque",
    "empresarial", "km", "the", "street", "st", "avenue", "square", "warehouse", "in", "at", "near",
}
STREET_PREFIXES = {
    "calle", "c", "cl", "cll", "avenida", "av", "avda", "avd", "plaza", "pza", "paseo", "po", "pso", "camino",
    "carretera", "ctra", "ronda", "glorieta", "travesia", "costanilla", "cuesta", "via", "pasaje", "callejon",
    "bulevar",
}
ESTATE_PREFIXES = {"poligono", "pol", "urbanizacion", "urb", "centro", "parque", "nave"}

# Generic title words: same-account titles share the brand, so these carry no identity.
TITLE_STOPWORDS = STOPWORDS | {
    "markets", "industrial", "estate", "warehouse", "nave", "second", "round", "edition", "weekend", "pop",
    "flea", "rastro", "vecinal", "home", "homes", "estate", "clearance", "sales", "vaciado", "garage",
    "and", "with", "from", "for", "new", "next", "proximo", "next", "special", "edicion", "segunda", "vuelta",
}

# Municipalities (and estates that belong to one) used to tell markets in different towns apart.
# Madrid city itself is left out: it is also the region, and neighbourhoods are worded inconsistently.
TOWNS: dict[str, str] = {t: t for t in (
    "alcobendas", "coslada", "aranjuez", "navacerrada", "majadahonda", "las rozas", "pozuelo", "fuenlabrada",
    "getafe", "leganes", "mostoles", "alcorcon", "parla", "alcala de henares", "torrejon de ardoz",
    "san sebastian de los reyes", "rivas vaciamadrid", "boadilla del monte", "villaviciosa de odon",
    "tres cantos", "colmenar viejo", "collado villalba", "galapagar", "torrelodones", "el escorial",
    "san lorenzo de el escorial", "guadarrama", "cercedilla", "villanueva de la canada", "arganda del rey",
    "valdemoro", "pinto", "ciempozuelos", "chinchon", "san fernando de henares", "mejorada del campo",
    "algete", "alcobendas", "el molar", "manzanares el real", "miraflores de la sierra", "brunete",
    "villalba", "humanes", "grinon", "navalcarnero", "moralzarzal", "becerril de la sierra",
    # outside the Comunidad
    "alicante", "segovia", "malaga", "barcelona", "sevilla", "valencia", "toledo", "guadalajara",
    "avila", "cuenca", "zaragoza", "bilbao", "valladolid", "salamanca", "marbella", "murcia",
)}
TOWNS.update({
    "cobo calleja": "fuenlabrada",
    "pozuelo de alarcon": "pozuelo",
    "las rozas de madrid": "las rozas",
    "sanse": "san sebastian de los reyes",
    "rivas": "rivas vaciamadrid",
    "alcala": "alcala de henares",
    "boadilla": "boadilla del monte",
    "torrejon": "torrejon de ardoz",
    "el escorial": "san lorenzo de el escorial",
    "san lorenzo de el escorial": "san lorenzo de el escorial",
})
_TOWN_RES = sorted(TOWNS, key=len, reverse=True)


def fold(text: str | None) -> str:
    """Accent-folded, lower-case, punctuation turned into spaces ("C/ Martínez" -> "c martinez")."""
    return " ".join(_norm(text or "").split())


def _segments(address: str | None) -> list[str]:
    return [fold(seg) for seg in re.split(r"[,;·|\n]+", address or "") if fold(seg)]


def _seg_kind(seg: str) -> str:
    first = seg.split()[0] if seg else ""
    if first in STREET_PREFIXES:
        return "street"
    if first in ESTATE_PREFIXES:
        return "estate"
    return "other"


def towns_in(text: str | None, *, skip_streets: bool = False) -> set[str]:
    """Known towns named in `text`. With skip_streets, address segments that start with a street word
    are ignored: "Avenida de Madrid", "Calle de Toledo" or "C. Villafranca del Bierzo" are streets."""
    out: set[str] = set()
    parts = _segments(text) if skip_streets else [fold(text)]
    for seg in parts:
        if skip_streets and _seg_kind(seg) == "street":
            continue
        padded = f" {seg} "
        for name in _TOWN_RES:
            if f" {name} " in padded:
                out.add(TOWNS[name])
                padded = padded.replace(f" {name} ", " ")
    return out


def address_numbers(address: str | None) -> set[str]:
    """House numbers (postal codes left out)."""
    return {n for n in re.findall(r"\b(\d{1,4})\b", fold(address))}


def street_tokens(address: str | None) -> set[str]:
    """The words that name the street(s): types, floors, numbers, towns and fillers removed."""
    town_words = {name for name in TOWNS if " " not in name} | {"madrid"}
    out = set()
    for seg in _segments(address):
        in_street = _seg_kind(seg) == "street"  # "Avenida de Madrid": here "madrid" names the street
        for t in seg.split():
            if t in STREET_WORDS or any(ch.isdigit() for ch in t) or len(t) < 2:
                continue
            if t in town_words and not in_street:
                continue
            out.add(t)
    return out


def is_estate_only(address: str | None) -> bool:
    """'Polígono industrial Cobo Calleja': names an area (an estate), not a street door."""
    segs = _segments(address)
    if not segs or address_numbers(address):
        return False
    return all(_seg_kind(s) != "street" for s in segs) and _seg_kind(segs[0]) == "estate"


def address_specificity(address: str | None) -> int:
    """0 none, 1 estate/area only, 2 street, 3 street with number."""
    if not address or not fold(address):
        return 0
    if is_estate_only(address):
        return 1
    return 3 if address_numbers(address) else 2


def address_contains(a: str | None, b: str | None) -> bool:
    """True when the street words of the shorter address all appear in the longer one
    ("Calle de Martínez Villergas" in "C/ Caribe 5 y 7 esq c/ Martínez Villergas, Edificio Maya")."""
    ta, tb = street_tokens(a), street_tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return small <= big


def _brand_free(toks: set[str], brands: set[str]) -> set[str]:
    squashed = [re.sub(r"[^a-z0-9]", "", fold(b)) for b in brands if b]
    return {t for t in toks if not (len(t) >= 4 and any(t in s for s in squashed))}


def title_tokens(title: str | None, brands: set[str] = frozenset()) -> set[str]:
    toks = {t for t in _norm(title or "").split() if len(t) > 2 and t not in TITLE_STOPWORDS}
    return _brand_free(toks, brands)


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


# ------------------------------------------------------------------- comparing
@dataclass
class Facts:
    """What matching needs to know about one market, from a parsed post or a stored event."""

    title: str
    dates: set[date]
    address: str | None = None
    area: str | None = None
    region: str = "unknown"
    created_at: datetime | None = None
    brands: set[str] = field(default_factory=set)  # handles of the account/organizer/collabs
    summary: str | None = None
    sessions: list = field(default_factory=list)
    account: str | None = None

    @property
    def street(self) -> str | None:
        return None if is_estate_only(self.address) else self.address

    @property
    def towns(self) -> set[str]:
        return (towns_in(self.address, skip_streets=True) | towns_in(self.area) | towns_in(self.title))

    def describe(self) -> dict:
        return {
            "title": self.title, "dates": [d.isoformat() for d in sorted(self.dates)], "sessions": self.sessions,
            "address": self.address, "area": self.area, "region": self.region, "summary": self.summary,
            "account": self.account,
        }


def facts_from_parsed(parsed: ParsedEvent, brands: set[str] = frozenset(), account: str | None = None,
                      now: datetime | None = None) -> Facts:
    return Facts(
        title=parsed.title, dates=set(parse_dates(parsed.dates)), address=parsed.address, area=parsed.area,
        region=getattr(parsed.region, "value", parsed.region) or "unknown", created_at=now,
        brands=set(brands) | ({parsed.organizer} if parsed.organizer else set()),
        summary=parsed.summary_en, sessions=[s.model_dump() for s in parsed.sessions], account=account,
    )


def facts_from_event(ev, brands: set[str] = frozenset(), account: str | None = None) -> Facts:  # noqa: ANN001
    org = getattr(ev, "organizer", None)
    sessions = getattr(ev, "session_list", None)
    return Facts(
        title=ev.title, dates=set(ev.date_list), address=ev.address, area=ev.area,
        region=getattr(ev, "region", None) or "unknown", created_at=ev.created_at,
        brands=set(brands) | ({org} if org else set()), summary=getattr(ev, "summary", None),
        sessions=sessions if isinstance(sessions, list) else [], account=account,
    )


SAME, MAYBE, DIFFERENT = "same", "maybe", "different"
SAME_SCORE = 3.0
MAYBE_SCORE = 1.5
LEAN_SCORE = 2.25
TEASER_DAYS = 21
DATELESS_PAIR_DAYS = 14


@dataclass
class Comparison:
    verdict: str  # same | maybe | different
    score: float = 0.0
    lean: bool = False  # for "maybe": what to decide when no tie-breaker is available
    reasons: list[str] = field(default_factory=list)

    @property
    def fallback_same(self) -> bool:
        return self.verdict == SAME or (self.verdict == MAYBE and self.lean)


def compare(new: Facts, old: Facts, now: datetime) -> Comparison:
    """Is `new` (a post, or the younger event) the same real-world market as `old`?

    Both are assumed to come from the same account or organizer group. Hard separators
    (different region, different town, disjoint dates, different street or door) win over everything.
    """
    why: list[str] = []
    brands = new.brands | old.brands

    # -- hard separators
    if new.region in ("madrid", "outside") and old.region in ("madrid", "outside") and new.region != old.region:
        return Comparison(DIFFERENT, reasons=["region differs"])
    if new.dates and old.dates and not (new.dates & old.dates):
        return Comparison(DIFFERENT, reasons=["dates disjoint"])  # same venue, new dates = new event
    tn, to = new.towns, old.towns
    if tn and to and not (tn & to):
        return Comparison(DIFFERENT, reasons=[f"towns differ ({'/'.join(sorted(tn))} vs {'/'.join(sorted(to))})"])

    score = 0.0
    ttn, tto = title_tokens(new.title, brands), title_tokens(old.title, brands)
    title_sim = _jaccard(ttn, tto)

    # -- streets
    sn, so = new.street, old.street
    street_conflict = False
    addr_link = False
    if sn and so:
        stn, sto = street_tokens(sn), street_tokens(so)
        if address_contains(sn, so):
            nn, no = address_numbers(sn), address_numbers(so)
            if stn == sto and nn and no and not (nn & no):
                return Comparison(DIFFERENT, reasons=["same street, different number"])
            score += 2.0
            addr_link = True
            why.append("address contained")
        elif stn & sto:
            ratio = len(stn & sto) / min(len(stn), len(sto))
            score += 1.5 if ratio >= 0.5 else 0.75
            addr_link = True
            why.append(f"address overlap {ratio:.2f}")
        elif stn and sto:
            street_conflict = True

    # -- place words of one side found in the other's address ("Martínez Villergas" area/title vs address)
    def place_words(f: Facts, title_toks: set[str]) -> set[str]:
        return title_toks | title_tokens(f.area, brands) | (street_tokens(f.address) if is_estate_only(f.address) else set())

    pn, po = place_words(new, ttn), place_words(old, tto)
    cross = False
    if so and pn and (pn & street_tokens(so)):
        cross = True
    if sn and po and (po & street_tokens(sn)):
        cross = True
    if cross and not (sn and so):
        score += 1.0
        why.append("place named in the other's address")

    if street_conflict:
        if title_sim >= 0.5 or cross:
            return Comparison(MAYBE, score=score + 1.0, lean=False, reasons=["different streets but similar titles"])
        return Comparison(DIFFERENT, reasons=["different streets"])

    # -- dates
    teaser_old = bool(new.dates) and not old.dates
    teaser_new = not new.dates and bool(old.dates)
    if new.dates and old.dates:
        score += 2.0 if new.dates == old.dates else 1.5
        why.append("same dates" if new.dates == old.dates else "dates overlap")
    elif teaser_old:
        age = (now - old.created_at) if old.created_at else timedelta(0)
        if age > timedelta(days=TEASER_DAYS):
            return Comparison(DIFFERENT, reasons=["old dateless teaser"])
        score += 1.0
        why.append("recent dateless teaser")
    elif not new.dates and not old.dates:
        age = (now - old.created_at) if old.created_at else timedelta(0)
        if age > timedelta(days=DATELESS_PAIR_DAYS):
            return Comparison(DIFFERENT, reasons=["two dateless posts far apart"])
        score += 1.0
        why.append("two recent dateless posts")

    # -- title / area / town
    if title_sim >= 0.5:
        score += 1.5
    elif title_sim > 0:
        score += title_sim
    if title_sim:
        why.append(f"title {title_sim:.2f}")
    an, ao = title_tokens(new.area, brands), title_tokens(old.area, brands)
    if an & ao:
        score += 0.5
        why.append("area overlap")
    if tn and tn == to:
        score += 0.75
        why.append("same town")

    geo_overlap = bool(cross or (an & ao) or (tn & to) or title_sim > 0 or addr_link)
    if teaser_new and not geo_overlap:
        return Comparison(DIFFERENT, reasons=["dateless post with nothing in common"])

    if score >= SAME_SCORE:
        return Comparison(SAME, score=score, lean=True, reasons=why)
    if teaser_old or (not new.dates and not old.dates):
        # A teaser is usually the market announced later; lean "same" unless both name places
        # that share nothing.
        lean = geo_overlap or not (_has_place(new) and _has_place(old))
        return Comparison(MAYBE, score=score, lean=lean, reasons=why)
    if score >= MAYBE_SCORE:
        return Comparison(MAYBE, score=score, lean=score >= LEAN_SCORE, reasons=why)
    return Comparison(DIFFERENT, score=score, reasons=why or ["nothing in common"])


def _has_place(f: Facts) -> bool:
    return bool(f.address or f.area or f.towns)


def rank(new: Facts, candidates: Sequence[tuple[Facts, EventLike]], now: datetime) -> list[tuple[Comparison, EventLike]]:
    """Non-different candidates, sure matches first, then by score."""
    out = []
    for facts, ev in candidates:
        cmp = compare(new, facts, now)
        if cmp.verdict != DIFFERENT:
            out.append((cmp, ev))
    out.sort(key=lambda x: (x[0].verdict != SAME, -x[0].score, x[1].created_at))
    return out


def find_match(parsed: ParsedEvent, candidates: Sequence[EventLike], now: datetime,
               brands: set[str] = frozenset()) -> EventLike | None:
    """Deterministic decision only (no tie-breaker). Candidates are the open events of the same
    account (or a collab partner / organizer). Among sure matches the oldest event wins."""
    new = facts_from_parsed(parsed, brands, now=now)
    ranked = rank(new, [(facts_from_event(ev, brands), ev) for ev in candidates], now)
    sure = [ev for cmp, ev in ranked if cmp.verdict == SAME]
    if sure:
        return min(sure, key=lambda e: (e.created_at, e.id))
    leaning = [ev for cmp, ev in ranked if cmp.fallback_same]
    return leaning[0] if leaning else None


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

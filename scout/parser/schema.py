"""What Gemini must return for one post, story or bio. Used as `response_schema`."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ItemKind(str, Enum):
    new_event = "new_event"  # first real announcement of a market (has at least a date or clear plan)
    teaser = "teaser"  # photos/hints of a coming market, date and/or address not given
    update = "update"  # new info for a known market: address, hours, cancellations, discounts
    reminder = "reminder"  # repeats a known market's details, nothing new
    last_day = "last_day"  # "último día", discounts on the final day
    preview = "preview"  # photos of pieces that will be on sale at an upcoming market, no new facts
    unrelated = "unrelated"  # thank-yous, decor tips, team photos, essays, past-event recaps


class Region(str, Enum):
    madrid = "madrid"  # Comunidad de Madrid, incl. Fuenlabrada, Majadahonda, Las Rozas, Alcobendas, Aranjuez…
    outside = "outside"  # Alicante, Segovia, Málaga, Barcelona, Sevilla…
    unknown = "unknown"


class BookingMethod(str, Enum):
    free_entry = "free_entry"
    eventbrite = "eventbrite"
    other_link = "other_link"
    dm = "dm"
    unknown = "unknown"


class BookingStatus(str, Enum):
    not_open = "not_open"
    open = "open"
    sold_out = "sold_out"
    cancellations = "cancellations"
    unknown = "unknown"


class Session(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    start: Optional[str] = Field(None, description="HH:MM 24h, Europe/Madrid")
    end: Optional[str] = Field(None, description="HH:MM 24h")
    booking_required: bool = Field(description="True if this time slot needs a ticket/appointment (cita, entrada, QR)")


class ParsedEvent(BaseModel):
    title: str = Field(description="Short English title, e.g. 'Alma Market — flat in Chamberí'")
    dates: list[str] = Field(description="Every day of the market as YYYY-MM-DD; empty if not announced")
    sessions: list[Session] = Field(description="One entry per opening slot; split days with morning/afternoon hours into two")
    address: Optional[str] = Field(None, description="Street address only if stated; never invent")
    area: Optional[str] = Field(None, description="Neighbourhood or town, e.g. 'Chamberí (Almagro)', 'Las Rozas'")
    region: Region
    organizer: Optional[str] = Field(None, description="Instagram handle of the organizer if named, without @")
    booking_method: BookingMethod
    booking_url: Optional[str] = Field(None, description="Only a URL that appears in the text or links")
    booking_opens_at: Optional[str] = Field(None, description="ISO 8601 local datetime when tickets/appointments open, e.g. 2026-11-10T10:00:00")
    booking_scope: Optional[str] = Field(None, description="English, which slots need booking, e.g. 'Fri 13 10:00–13:00'")
    booking_status: BookingStatus
    link_in_bio: bool = Field(description="True if the post says the link is in the profile/bio")
    price: Optional[str] = Field(None, description="'Free' or amount")
    summary_en: str = Field(description="At most 2 short English sentences on what is for sale")
    change: Optional[str] = Field(None, description="For update/last_day: what is new, in English, e.g. 'cancellations: tickets available again'")
    confidence: float = Field(description="0–1, how sure the dates/address are right")


class ParseResult(BaseModel):
    kind: ItemKind
    events: list[ParsedEvent] = Field(description="Empty for unrelated and preview. Several when one post lists several markets.")
    items_for_sale: list[str] = Field(
        default_factory=list,
        description="When the images show actual pieces for sale at an upcoming market: short English names of "
                    "the notable ones, most prominent first, at most 5 (e.g. 'rattan armchair', 'brass floor lamp'). "
                    "Empty for flyers, text, people, rooms without clear pieces, or past markets.")

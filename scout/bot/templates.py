"""Message text and keyboards. HTML parse mode; captions stay under Telegram's 1,024 characters."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..db import Event

CAPTION_LIMIT = 1024

DECISION_FOOTER = {
    "yes": "✅ Going",
    "maybe": "🤔 Watching for date/address",
    "no": "❌ Skipped",
}

BOOKING_LABEL = {
    "free_entry": "Free entry",
    "eventbrite": "Eventbrite booking",
    "other_link": "Booking link",
    "dm": "Book by DM",
    "unknown": "",
}


def fmt_day(d: date) -> str:
    return d.strftime("%a %-d %b")


def fmt_dt(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    return local.strftime("%a %-d %b %H:%M")


def schedule_lines(ev: Event) -> list[str]:
    sessions = ev.session_list
    if not sessions:
        days = ev.date_list
        return [" · ".join(fmt_day(d) for d in days)] if days else []
    by_day: "OrderedDict[str, list[str]]" = OrderedDict()
    for s in sorted(sessions, key=lambda s: (s.get("date") or "", s.get("start") or "")):
        try:
            label = fmt_day(date.fromisoformat(s["date"]))
        except (KeyError, ValueError):
            continue
        span = f"{s.get('start') or '?'}–{s.get('end') or '?'}" if s.get("start") or s.get("end") else ""
        if s.get("booking_required"):
            span = (span + " 🎟").strip()
        by_day.setdefault(label, [])
        if span:
            by_day[label].append(span)
    return [f"{day}" + (f" · {', '.join(spans)}" if spans else "") for day, spans in by_day.items()]


def event_caption(ev: Event, handle: str, tz: ZoneInfo, header: str | None = None) -> str:
    lines: list[str] = []
    if header:
        lines.append(escape(header))
    lines.append(f"🪑 <b>{escape(ev.title)}</b>")
    lines.append(f"<i>@{escape(handle)}</i>")

    sched = schedule_lines(ev)
    if sched:
        lines.append("📅 " + escape(sched[0]))
        lines += ["      " + escape(x) for x in sched[1:]]
    else:
        lines.append("📅 Date not announced yet")

    if ev.address:
        lines.append(f"📍 {escape(ev.address)}")
    elif ev.area:
        lines.append(f"📍 {escape(ev.area)} <i>(exact address not announced yet)</i>")
    else:
        lines.append("📍 Location not announced yet")

    booking = BOOKING_LABEL.get(ev.booking_method, "")
    if ev.booking_method not in ("free_entry", "unknown"):
        scope = f": {escape(ev.booking_scope)}" if ev.booking_scope else ""
        price = f" · {escape(ev.price)}" if ev.price else ""
        lines.append(f"🎟 {booking}{scope}{price}")
        if ev.booking_status == "sold_out":
            lines.append("⛔ Booked out")
        elif ev.booking_status == "cancellations":
            lines.append("🔁 Tickets released again (cancellations)")
        if ev.booking_opens_at:
            lines.append(f"⏰ Booking opens {fmt_dt(ev.booking_opens_at, tz)}")
        if ev.link_in_bio and not ev.booking_url:
            lines.append("🔗 Link is in their Instagram bio")
    elif ev.booking_method == "free_entry":
        lines.append(f"🎟 Free entry{' · ' + escape(ev.price) if ev.price and ev.price.lower() != 'free' else ''}")

    booked = booked_line(ev, tz)
    if booked:
        lines.append(booked)

    notes = notes_lines(ev)
    if notes:
        lines.append("")
        lines += notes

    footer: list[str] = []
    if ev.user_decision in DECISION_FOOTER:
        footer = ["", DECISION_FOOTER[ev.user_decision]]

    summary: list[str] = ["", escape(ev.summary)] if ev.summary else []
    text = "\n".join(lines + summary + footer)
    if len(text) > CAPTION_LIMIT and summary:
        # Shorten the summary first so notes and the decision footer survive.
        room = CAPTION_LIMIT - len("\n".join(lines + footer)) - 3
        summary = ["", escape(ev.summary[: max(0, room - 20)]).rstrip() + "…"] if room > 40 else []
        text = "\n".join(lines + summary + footer)
    if len(text) > CAPTION_LIMIT:
        text = text[: CAPTION_LIMIT - 1] + "…"
    return text


def booked_line(ev: Event, tz: ZoneInfo) -> str | None:
    if ev.booked_start is None:
        return None
    who = "auto" if ev.booked_via == "auto" else "by you"
    ticket = " · ticket saved" if ev.ticket_path else ""
    return f"🎟 <b>Booked {fmt_dt(ev.booked_start, tz)}</b> ({who}){ticket}"


def notes_lines(ev: Event) -> list[str]:
    notes = [n.get("text", "") for n in ev.note_list if n.get("text")]
    if not notes:
        return []
    return ["📝 <b>Notes:</b>"] + [f"• {escape(n)}" for n in notes]


# Feature modules (eventbrite watcher, auto-booker) append functions here that return extra
# buttons for an answered card, e.g. "🔔 Reminders on" or "🤖 Auto-book at opening".
EXTRA_ANSWERED_BUTTONS: list = []  # list[Callable[[Event], list[InlineKeyboardButton]]]


def _link_row(ev: Event) -> list[InlineKeyboardButton]:
    row = []
    if ev.source_url:
        row.append(InlineKeyboardButton("📷 Instagram", url=ev.source_url))
    if ev.booking_url and ev.booking_url.startswith(("http://", "https://")):
        row.append(InlineKeyboardButton("🎟 Booking", url=ev.booking_url))
    return row


def decision_keyboard(ev: Event, teaser: bool = False) -> InlineKeyboardMarkup:
    first = [
        InlineKeyboardButton("✅ Interested", callback_data=f"d:{ev.id}:yes"),
        InlineKeyboardButton("🤔 Maybe", callback_data=f"d:{ev.id}:maybe"),
        InlineKeyboardButton("❌ No", callback_data=f"d:{ev.id}:no"),
    ]
    if teaser:
        first = [first[1], first[2]]  # no date yet: Maybe tracks it until confirmed
    rows = [first]
    links = _link_row(ev)
    if links:
        rows.append(links)
    return InlineKeyboardMarkup(rows)


def answered_keyboard(ev: Event) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("↩️ Change answer", callback_data=f"c:{ev.id}")]]
    for fn in EXTRA_ANSWERED_BUTTONS:
        extra = fn(ev)
        if extra:
            rows.append(extra)
    links = _link_row(ev)
    if links:
        rows.append(links)
    return InlineKeyboardMarkup(rows)


def short_line(ev: Event, handle: str) -> str:
    days = ev.date_list
    when = ", ".join(fmt_day(d) for d in days) if days else "date TBA"
    where = ev.address or ev.area or "place TBA"
    return f"• <b>{escape(ev.title)}</b> — {escape(when)} — {escape(where)} (@{escape(handle)})"


HELP = (
    "<b>Scout</b> watches Instagram accounts for vintage markets and estate sales, and tells you when one is announced.\n\n"
    "/add <i>@handle or URL</i> — follow an account (I read its posts and stories right away)\n"
    "/remove — stop following one\n"
    "/list — accounts I follow\n"
    "/upcoming — markets you said yes to\n"
    "/maybe — markets I'm watching for you\n"
    "/events — every market I track, with filters\n"
    "/check — check all accounts now, stories included\n"
    "/pause · /resume — mute or unmute alerts\n"
    "/status — last checks and Apify credit\n"
    "/logs — recent warnings and errors (<code>/logs all</code> for everything)\n"
    "/booked <i>id HH:MM [date]</i> — tell me you booked a slot · /unbook <i>id</i>\n"
    "/hunt <i>id [before HH:MM]</i> — watch for released free tickets (or an earlier slot than yours) · <code>/hunt id off</code>\n"
    "/note <i>id text</i> — remember something for a market (or reply to its card) · /notes · /delnote\n\n"
    "You can also paste an Instagram profile link."
)

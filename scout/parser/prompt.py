"""System prompt for the Gemini parser. Built from the patterns found in the six accounts on 7 Nov 2026."""

SYSTEM_PROMPT = """\
You read Spanish Instagram posts, stories and profile bios from accounts that announce
vintage markets, flea markets, house-clearance sales ("mercadillos en casa", "vaciado de
casas", "puertas abiertas", "market") and estate sales, mostly in the Comunidad de Madrid.
Return JSON matching the schema. Write every text field in English.

Rules
- Read the text inside the images too: flyers often hold the only date, hours or address.
- Resolve relative dates against the POST DATE given below: "mañana domingo", "este
  sábado", "el martes a las 10:00", "lunes 9 a las 00h". Pick the next matching day on or
  after the post date. Years are the post's year unless the month has already passed.
- Never invent an address, date, time or URL. Use null or an empty list.
- "address" is only a street address; a neighbourhood or town alone goes in "area".
- One post can list several different markets (e.g. a weekend roundup): return one event
  per market.
- Booking often applies only to part of the market ("con cita el viernes por la mañana",
  "QR obligatorio de 10 a 14", "PRIMER DÍA con entrada"). Mark each session's
  booking_required and describe it in booking_scope. "Sin cita", "entrada libre", "acceso
  libre", "entrada gratuita" mean free entry for that slot.
- booking_method = eventbrite when Eventbrite is named (also misspelt "Evenbrite") or an
  eventbrite URL appears; other_link for Linktree/other ticket links; dm for "escríbenos".
- booking_status: "completo", "agotado", "no se permite la entrada sin reserva" = sold_out;
  "cancelaciones", "se han liberado" = cancellations; tickets available from a future
  time = not_open.
- link_in_bio = true for "link en bio", "enlace en el perfil", "link en nuestro perfil".
- kind:
  * unrelated: thank-you or recap of a past market, decor tips, team photos, essays,
    milestones, visitor montages, with no upcoming market details.
  * teaser: photos or hints of an upcoming market without a date ("próximo market",
    "adelanto", "esta casa tiene una historia"), or with a date but deliberately no
    location.
  * new_event: announces a market with dates.
  * reminder: repeats an upcoming market already described, nothing new.
  * update: adds new facts (address, hours, booking time, cancellations, discounts).
  * last_day: "último día", final-day discounts.
  * preview: photos or videos showing pieces that will be on sale (furniture, lamps, art,
    tableware, clothes…) for the account's upcoming market, with no new date, hours,
    address or booking facts. Return no events. Many stories in a row are like this.
    A teaser that also shows pieces is still a teaser. A story that is just a photo of a
    chair, lamp, table or similar piece from an account that runs markets is a preview,
    not unrelated.
- organizer: the Instagram handle of whoever runs the market, without @. Accounts often
  repost or round up other people's markets (RESHARES lines, "via @x", "organiza @x",
  a reposted flyer showing another handle): then it is that other account. Use null when
  the account itself organises it.
- items_for_sale: whenever the images show actual pieces for an upcoming market (preview,
  teaser, new_event…), name the notable ones briefly in English, most prominent first, at
  most 5. Leave it empty for flyers, pure text, people, recaps of past markets.
- region = madrid for any town in the Comunidad de Madrid (Madrid city, Alcobendas,
  Coslada, Aranjuez, Navacerrada, Majadahonda, Las Rozas, Pozuelo, Fuenlabrada, Cobo
  Calleja…); outside for other provinces (Alicante, Segovia, Málaga, Barcelona, Sevilla…);
  unknown if the text gives no place at all.
"""


def build_user_prompt(
    *,
    handle: str,
    item_kind: str,
    posted_at_local: str,
    today_local: str,
    caption: str,
    location: str | None,
    coauthors: list[str],
    bio: str | None,
    bio_links: list[str],
    link_urls: list[str],
    reshared_from: str | None = None,
    shared_post_url: str | None = None,
    mentions: list[str] | None = None,
) -> str:
    parts = [
        f"ACCOUNT: @{handle}",
        f"ITEM TYPE: {item_kind}",
        f"POST DATE: {posted_at_local}",
        f"TODAY: {today_local}",
    ]
    if location:
        parts.append(f"TAGGED LOCATION: {location}")
    if coauthors:
        parts.append("COLLAB WITH: " + ", ".join("@" + c for c in coauthors))
    if bio:
        parts.append(f"PROFILE BIO: {bio}")
    if bio_links:
        parts.append("PROFILE LINKS: " + ", ".join(bio_links))
    if link_urls:
        parts.append("LINKS IN THIS ITEM: " + ", ".join(link_urls))
    if reshared_from:
        parts.append(f"RESHARES A STORY BY: @{reshared_from}")
    if shared_post_url:
        parts.append(f"RESHARES SOMEONE ELSE'S POST: {shared_post_url} (its author is usually shown on the image)")
    if mentions:
        parts.append("MENTIONS: " + ", ".join("@" + m for m in mentions))
    parts.append("TEXT:\n" + (caption.strip() or "(no caption — read the images)"))
    return "\n".join(parts)

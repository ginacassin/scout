"""Everything that depends on Eventbrite's page wording or URLs lives here, so a layout change is a
one-file fix. Patterns are Spanish + English (the profile browses eventbrite.es with locale es-ES,
but organisers and A/B tests mix both). All helpers are pure functions and unit-tested.

Verify live (see README): RESERVE_RE, ADVANCE_RE, SUCCESS_TEXT_RE, LOGIN_CHECK_PATH.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

# -- event page: the button that opens the checkout ------------------------------------------
RESERVE_RE = re.compile(
    r"^\s*(?:reserve(?: a spot| now| your spot)?|get tickets|register|"
    r"reservar(?: un sitio| una plaza| plaza| ahora)?|conseguir entradas|consigue entradas|"
    r"obtener entradas|registrarse|reg[ií]strate|apuntarse|ap[uú]ntate|"
    # timed-entry / multi-date events open the checkout (with a date/time picker) from these
    r"check availability|comprobar disponibilidad|ver disponibilidad|select (?:a )?date(?: and time)?|"
    r"seleccionar fecha(?: y hora)?|selecciona (?:una )?fecha|ver fechas|see dates)\s*$",
    re.I,
)

# -- checkout: buttons that move the order forward (ticket step, contact step, final step) ----
ADVANCE_RE = re.compile(
    r"^\s*(?:register|registrarse|reg[ií]strate|reserve(?: now)?|reservar(?: ahora)?|"
    r"check ?out|tramitar pedido|finalizar compra|place order|realizar pedido|"
    r"complete(?: registration| order)?|completar(?: registro| pedido| reserva| inscripci[oó]n)?|"
    r"confirm(?: order)?|confirmar(?: pedido| reserva)?|continue|continuar|next|siguiente|"
    r"get tickets|conseguir entradas|obtener entradas)\s*$",
    re.I,
)

# Stepper buttons on checkouts that use +/- instead of a <select>.
INCREASE_RE = re.compile(r"(?:increase|add one|plus|aumentar|a[ñn]adir|sumar|m[aá]s)\b", re.I)

SOLD_OUT_RE = re.compile(
    r"\b(?:sold out|sales ended|agotad[oa]s?|entradas agotadas|venta finalizada|"
    r"las ventas han finalizado|ya no hay entradas)\b",
    re.I,
)

SUCCESS_TEXT_RE = re.compile(
    r"(?:thanks? for your order|thank you for your order|order confirmed|your order is confirmed|"
    r"you're going|you are going|you're registered|"
    r"gracias por tu pedido|gracias por su pedido|gracias por tu reserva|"
    r"tu pedido (?:se ha|ha sido) (?:confirmado|completado|realizado)|pedido confirmado|"
    r"vas a ir|ya est[aá]s (?:inscrit|registrad)[oa]|"
    r"order\s*(?:#|no\.?|n\.?º|number)\s*:?\s*\d{6,}|"
    r"(?:n[uú]mero de )?pedido\s*(?:#|n\.?º|n[uú]mero)?\s*:?\s*\d{6,})",
    re.I,
)

SUCCESS_URL_RE = re.compile(r"order[-_]?confirmation|checkout[-_]success|/orderconfirmation|thank[-_]?you", re.I)

CHALLENGE_TITLE_RE = re.compile(r"just a moment|un momento|attention required|verify you are human|"
                                r"verifica que eres humano", re.I)

CAPTCHA_HOSTS = ("recaptcha", "hcaptcha", "challenges.cloudflare.com", "arkoselabs", "funcaptcha",
                 "captcha-delivery.com", "geo.captcha", "perimeterx", "px-captcha")

PAYMENT_HOSTS = ("stripe.com", "braintree", "braintreegateway", "adyen", "paypal.com", "checkout.com")
PAYMENT_TEXT_RE = re.compile(r"card number|n[uú]mero de (?:la )?tarjeta|fecha de caducidad|expiry date|"
                             r"\bCVC\b|\bCVV\b|payment method|m[eé]todo de pago", re.I)

SIGNIN_TEXT_RE = re.compile(r"(?:iniciar sesi[oó]n|log ?in|sign ?in).{0,400}(?:contrase[ñn]a|password)", re.I | re.S)

# Page that redirects to /signin when the profile is logged out.
LOGIN_CHECK_PATH = "/account-settings/"


def is_captcha_url(url: str | None) -> bool:
    u = (url or "").lower()
    return any(h in u for h in CAPTCHA_HOSTS)


def is_payment_url(url: str | None) -> bool:
    host = urlparse(url or "").netloc.lower()
    return any(h in host for h in PAYMENT_HOSTS)


def is_checkout_url(url: str | None) -> bool:
    """Embedded checkout iframe (`/checkout-external?eid=`), or a full-page checkout."""
    p = urlparse(url or "")
    if "eventbrite." not in p.netloc.lower():
        return False
    return "checkout" in p.path.lower()


def checkout_external_url(eventbrite_id: str, base_url: str = "https://www.eventbrite.es") -> str:
    return f"{base_url.rstrip('/')}/checkout-external?eid={eventbrite_id}"


def is_success(url: str | None, text: str | None) -> bool:
    return bool(SUCCESS_URL_RE.search(url or "")) or bool(SUCCESS_TEXT_RE.search(text or ""))


def is_sold_out(text: str | None) -> bool:
    return bool(SOLD_OUT_RE.search(text or ""))


def has_payment_form(text: str | None) -> bool:
    return bool(PAYMENT_TEXT_RE.search(text or ""))


def looks_like_quantity_options(values: list[str]) -> bool:
    """A ticket quantity <select> has only small integer options, e.g. ['0','1',...,'10']."""
    vals = [v.strip() for v in values if v is not None and v.strip() != ""]
    return bool(vals) and all(v.isdigit() and int(v) <= 100 for v in vals) and len(vals) >= 2


# Amounts like "12,50 €", "€12.50", "EUR 3", "0,00 €", "$5".
_AMOUNT = r"(\d{1,5}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)"
_CUR = r"(?:€|eur|euros?|\$|usd|£|gbp)"
AMOUNT_RE = re.compile(rf"(?:{_CUR}\s*{_AMOUNT}|{_AMOUNT}\s*{_CUR})", re.I)
TOTAL_RE = re.compile(rf"\btotal\b[^\n\d€$£]{{0,30}}(?:{_CUR}\s*{_AMOUNT}|{_AMOUNT}\s*{_CUR}|(gratis|free|gratuito))", re.I)
FREE_WORD_RE = re.compile(r"\b(?:gratis|free|gratuit[oa]s?)\b", re.I)


def to_decimal(s: str) -> Decimal | None:
    s = s.strip()
    if "," in s and "." in s:
        # whichever comes last is the decimal separator
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else s.replace(",", "")
    elif "." in s:
        head, _, tail = s.rpartition(".")
        s = f"{head.replace('.', '')}.{tail}" if len(tail) <= 2 else s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def amounts(text: str) -> list[Decimal]:
    out = []
    for m in AMOUNT_RE.finditer(text or ""):
        d = to_decimal(m.group(1) or m.group(2))
        if d is not None:
            out.append(d)
    return out


def assess_price(text: str | None) -> tuple[str, str]:
    """('free' | 'paid' | 'unknown', detail) for the visible checkout text.

    Strict on purpose: any non-zero money amount anywhere in the checkout means 'paid'."""
    text = text or ""
    nonzero = [a for a in amounts(text) if a > 0]
    if nonzero:
        return "paid", f"amount {nonzero[0]} on the page"
    m = TOTAL_RE.search(text)
    if m:
        if m.group(3):
            return "free", "total: free"
        val = to_decimal(m.group(1) or m.group(2) or "")
        if val is not None and val == 0:
            return "free", "total 0"
    if any(a == 0 for a in amounts(text)) or FREE_WORD_RE.search(text):
        return "free", "free ticket"
    return "unknown", "no price shown"


def login_state(url: str | None, text: str | None) -> bool | None:
    """After opening LOGIN_CHECK_PATH: redirected to sign-in => False; stayed => True; otherwise None."""
    p = urlparse(url or "")
    path = p.path.lower()
    if is_captcha_url(url):
        return None
    if "/signin" in path or "/login" in path:
        return False
    if SIGNIN_TEXT_RE.search(text or ""):
        return False
    if "eventbrite." in p.netloc.lower() and "account-settings" in path:
        return True
    return None


# -- timed-entry slots (ticket classes like "Viernes 13 · 10:00–10:30" or a date/time picker) ----
# A time-slot control: a short clickable label containing a time ("10:00", "10:00 - 10:30", "10am").
SLOT_TIME_JS = r"(^|[^\d:/.])\d{1,2}([:.]\d{2}|\s*[ap]\.?\s?m\b|h\b)"
# Roles we look at for picker slots. On the event page we skip labels/links (organiser's other events).
SLOT_PICK_SELECTOR_EVENT_PAGE = "button, [role=button], [role=radio], [role=option], [role=gridcell]"
SLOT_PICK_SELECTOR_CHECKOUT = ("button, [role=button], [role=radio], [role=option], [role=gridcell], [role=tab], "
                               "label, [data-testid*=time i], [data-testid*=slot i]")
# Containers whose plain day-number cells ("13") are calendar days.
CALENDAR_SELECTOR = "[role=grid], table, [class*=calendar i], [class*=Calendar], [data-testid*=calendar i]"

# -- after booking: where the ticket QR / PDF is ----------------------------------------------
TICKET_QR_SELECTORS = (
    "img[alt*='QR' i]", "img[src*='qr' i]", "canvas[aria-label*='QR' i]", "svg[aria-label*='QR' i]",
    "[data-testid*='qr' i] img", "[data-testid*='qr' i] canvas", "[data-testid*='qr' i] svg",
    "[class*='qr' i] img", "[class*='qr' i] canvas", "[class*='qr' i] svg", "[data-testid*='qr' i]",
    "[data-testid*='ticket' i] img[src*='barcode' i]", "img[alt*='código' i]", "img[alt*='barcode' i]",
)
MY_TICKETS_RE = re.compile(
    r"^\s*(?:ver (?:mis |las |tus )?entradas|mis entradas|ir a mis entradas|my tickets|go to my tickets|"
    r"view (?:my |your )?tickets?|ver (?:el |tu |mi )?pedido|view (?:your |my )?order|ver entrada)\s*$",
    re.I,
)
DOWNLOAD_TICKET_RE = re.compile(
    r"(?:descargar|download|imprimir|print)\s+(?:las |tus |mis |your |my |the )?(?:entradas?|tickets?|pdf)|"
    r"descargar pdf|download pdf",
    re.I,
)

REASONS = {
    "captcha": "a captcha appeared",
    "paid": "the order isn't free",
    "not_free": "couldn't confirm the order is free",
    "not_logged_in": "the Eventbrite session has expired — send /autobook login",
    "never_opened": "tickets didn't become available in time",
    "sold_out": "it sold out",
    "several_tickets": "there are several ticket types to choose from",
    "no_slot_fits": "no slot fits your route",
    "quantity": "couldn't pick the ticket quantity",
    "extra_fields": "the form asks for extra details",
    "unexpected_page": "the page looked different than expected",
    "timeout": "it took too long",
    "interrupted": "the booker restarted mid-attempt",
    "missed": "the booker was offline at opening time",
    "error": "something went wrong",
}


def reason_text(code: str) -> str:
    return REASONS.get(code, code.replace("_", " "))

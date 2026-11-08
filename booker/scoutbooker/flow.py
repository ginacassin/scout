"""The booking attempt, run once per job starting at opens_at - preopen.

  1. T-90s  check the profile is logged in, open the event page.
  2. T      poll the page (every poll_ms, reloading every reload_every_s) until the
            Reserve / Get tickets button is enabled, click it and find the checkout
            (iframe `/checkout-external` or a full-page checkout). If no button ever shows,
            open checkout-external?eid= directly.
  3.        timed slots (ticket classes like "Viernes 13 · 10:00–10:30", or a date/time picker on the
            event page or in the checkout): pick the earliest one that fits job.constraints (Scout's
            route planner, see slots.py); none fits -> stop with "no slot fits your route".
            Then pick the quantity (exactly one ticket type left, otherwise stop).
  4.        loop: success? -> done (then capture the ticket QR / PDF into /profile/tickets).  captcha / payment form / non-zero amount / empty required
            field / no button to press -> stop.  Otherwise press Register/Continue.
Every stop calls `on_abort` *first* (the "book manually NOW" message), then takes a screenshot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from . import eb_selectors as S
from . import slots as SL
from .config import Config
from .jobs import Job, utcnow

log = logging.getLogger(__name__)

MAX_STEPS = 5
CLICK_SETTLE_S = 20


class Abort(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class BookResult:
    ok: bool
    code: str
    detail: str = ""
    screenshot: Path | None = None
    submitted: bool = False  # a final-looking button was pressed: the order may exist
    slot: SL.Slot | None = None  # the timed slot we picked, if the event has them
    ticket: Path | None = None   # captured QR png / ticket pdf


AbortHook = Callable[[str, str, bool], Awaitable[None]]  # (code, detail, submitted)
LoginCheck = Callable[[], Awaitable[bool | None]]


class Attempt:
    def __init__(self, ctx, job: Job, cfg: Config, check_login: LoginCheck, on_abort: AbortHook,  # noqa: ANN001
                 clock: Callable[[], datetime] = utcnow):
        self.ctx = ctx
        self.job = job
        self.cfg = cfg
        self.check_login = check_login
        self.on_abort = on_abort
        self.clock = clock
        self.page = None
        self.submitted = False
        self.chosen: SL.Slot | None = None

    # ------------------------------------------------------------------- entry
    async def run(self) -> BookResult:
        budget = self.cfg.preopen_s + self.cfg.window_s + 240
        try:
            self.page = await self.ctx.new_page()
            try:
                await asyncio.wait_for(self._book(), timeout=budget)
            except (asyncio.TimeoutError, TimeoutError):
                raise Abort("timeout", f"no result after {budget}s") from None
            shot = await self._shot("booked")
            ticket = None
            try:
                ticket = await asyncio.wait_for(self._capture_ticket(), timeout=TICKET_BUDGET_S)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  (never let the ticket spoil a booking)
                log.warning("Ticket capture for %s failed", self.job.event_id, exc_info=True)
            return BookResult(True, "booked", screenshot=shot, submitted=True, slot=self.chosen, ticket=ticket)
        except asyncio.CancelledError:
            raise
        except Abort as a:
            return await self._fail(a.code, a.detail)
        except Exception as exc:  # noqa: BLE001  (Playwright errors, closed page, ...)
            log.exception("Booking %s crashed", self.job.event_id)
            return await self._fail("error", f"{type(exc).__name__}: {str(exc)[:150]}")
        finally:
            if self.page is not None:
                try:
                    await self.page.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _fail(self, code: str, detail: str) -> BookResult:
        log.warning("Booking %s stopped: %s %s", self.job.event_id, code, detail)
        await self.on_abort(code, detail, self.submitted)  # tell the user before anything slow
        shot = await self._shot(f"failed-{code}")
        return BookResult(False, code, detail, shot, self.submitted, slot=self.chosen)

    async def _shot(self, label: str) -> Path | None:
        if self.page is None:
            return None
        self.cfg.shots_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.shots_dir / f"{self.job.event_id}-{label}-{int(time.time())}.png"
        try:
            await self.page.screenshot(path=str(path), timeout=5000)
            return path
        except Exception:  # noqa: BLE001
            return None

    # -------------------------------------------------------------------- flow
    async def _book(self) -> None:
        job, page = self.job, self.page
        opens = job.opens
        if self.clock() < opens:  # skip pre-flight if we started late (restart inside the window)
            if await self.check_login() is False:
                raise Abort("not_logged_in")
        await page.goto(job.event_url, wait_until="domcontentloaded", timeout=30_000)
        await self._guard()
        await self._sleep_until(opens)
        frame = await self._open_checkout(opens)
        await self._wait_ready(frame, 5)
        frame = self._refresh_frame(frame)
        if not await self._pick_slot(frame):
            await self._select_quantity(frame)
        await self._advance(frame)

    async def _sleep_until(self, when: datetime) -> None:
        while (remaining := (when - self.clock()).total_seconds()) > 0:
            await asyncio.sleep(min(remaining, 5.0) if remaining > 1 else remaining)

    async def _guard(self) -> None:
        """Captcha anywhere -> stop at once. We never try to solve one."""
        for f in self.page.frames:
            if S.is_captcha_url(f.url):
                raise Abort("captcha", f.url[:80])
        try:
            title = await self.page.title()
        except Exception:  # noqa: BLE001
            title = ""
        if S.CHALLENGE_TITLE_RE.search(title or ""):
            raise Abort("captcha", title)

    @staticmethod
    async def _text(frame) -> str:  # noqa: ANN001
        try:
            return await frame.evaluate("() => document.body ? document.body.innerText.slice(0, 20000) : ''")
        except Exception:  # noqa: BLE001  (frame navigating or detached)
            return ""

    @staticmethod
    async def _first_usable(locator, need_enabled: bool = True):  # noqa: ANN001, ANN205
        """First visible (and enabled) element of a locator, or None."""
        try:
            n = min(await locator.count(), 10)
        except Exception:  # noqa: BLE001
            return None
        for i in range(n):
            el = locator.nth(i)
            try:
                if not await el.is_visible():
                    continue
                if need_enabled and (not await el.is_enabled() or await el.get_attribute("aria-disabled") == "true"):
                    continue
                return el
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _find_reserve(self, enabled: bool):  # noqa: ANN202
        for role in ("button", "link"):
            el = await self._first_usable(self.page.get_by_role(role, name=S.RESERVE_RE), need_enabled=enabled)
            if el is not None:
                return el
        return None

    def _checkout_frame(self):  # noqa: ANN202
        if S.is_checkout_url(self.page.url):
            return self.page.main_frame
        for f in self.page.frames:
            if f is not self.page.main_frame and S.is_checkout_url(f.url):
                return f
        return None

    async def _wait_checkout_frame(self, timeout: float):  # noqa: ANN202
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            await self._guard()
            f = self._checkout_frame()
            if f is not None:
                try:
                    await f.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:  # noqa: BLE001
                    pass
                return f
            await asyncio.sleep(0.25)
        return None

    async def _checkout_ready(self, frame) -> bool:  # noqa: ANN001
        if await self._quantity_selects(frame):
            return True
        if await self._first_usable(frame.get_by_role("button", name=S.ADVANCE_RE)) is not None:
            return True
        # timed-entry checkout that starts with a date/time picker
        if await self._picker_slots(frame, S.SLOT_PICK_SELECTOR_CHECKOUT):
            return True
        try:
            return await frame.locator("[role=grid]").count() > 0
        except Exception:  # noqa: BLE001
            return False

    async def _wait_ready(self, frame, timeout: float) -> None:  # noqa: ANN001
        """The iframe exists before its React app renders the ticket list."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            await self._guard()
            frame = self._refresh_frame(frame)
            if await self._checkout_ready(frame) or await self._is_success():
                return
            await asyncio.sleep(0.3)

    async def _open_checkout(self, opens: datetime):  # noqa: ANN202
        cfg, page, job = self.cfg, self.page, self.job
        deadline = opens + timedelta(seconds=cfg.window_s)
        fallback_at = opens + timedelta(seconds=20)
        standalone = False
        seen_button = False
        last_load = time.monotonic()
        while True:
            now = self.clock()
            if now > deadline:
                raise Abort("never_opened", f"nothing to book {cfg.window_s // 60} min after opening")
            await self._guard()
            if standalone:
                if await self._checkout_ready(page.main_frame):
                    return page.main_frame
            else:
                btn = await self._find_reserve(enabled=False)
                if btn is not None:
                    seen_button = True
                    if await btn.is_enabled() and await btn.get_attribute("aria-disabled") != "true":
                        if self.chosen is None:  # timed-entry pages may show the slots next to the button
                            await self._pick_from_picker(page.main_frame, S.SLOT_PICK_SELECTOR_EVENT_PAGE)
                        log.info("Reserve button enabled, clicking")
                        await btn.click(timeout=5000)
                        frame = await self._wait_checkout_frame(20)
                        if frame is not None:
                            await self._wait_ready(frame, 15)
                            return frame
                        log.warning("Click didn't open a checkout; using checkout-external")
                        standalone = True
                elif now >= fallback_at and not seen_button:
                    log.warning("No reserve button on the event page; using checkout-external")
                    standalone = True
                if standalone:
                    await page.goto(S.checkout_external_url(job.eventbrite_id, cfg.base_url),
                                    wait_until="domcontentloaded", timeout=30_000)
                    last_load = time.monotonic()
                    continue
                if now >= opens and S.is_sold_out(await self._text(page.main_frame)):
                    raise Abort("sold_out")
            if now >= opens and time.monotonic() - last_load >= cfg.reload_every_s:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=15_000)
                except Exception:  # noqa: BLE001  (slow reload: keep polling)
                    log.warning("Reload failed", exc_info=True)
                last_load = time.monotonic()
            await asyncio.sleep(cfg.poll_ms / 1000)

    async def _quantity_selects(self, frame) -> list:  # noqa: ANN001
        out = []
        try:
            loc = frame.locator("select")
            n = min(await loc.count(), 20)
            for i in range(n):
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                values = await el.evaluate("el => Array.from(el.options).map(o => o.value)")
                if S.looks_like_quantity_options(values):
                    out.append((el, values))
        except Exception:  # noqa: BLE001
            return out
        return out

    async def _select_quantity(self, frame) -> None:  # noqa: ANN001
        q = self.job.quantity
        selects = await self._quantity_selects(frame)
        if len(selects) > 1:
            raise Abort("several_tickets", f"{len(selects)} ticket types")
        if selects:
            el, values = selects[0]
            if str(q) not in values:
                raise Abort("quantity", f"wanted {q}, options {values[:8]}")
            await el.select_option(str(q), timeout=5000)
            log.info("Quantity %s selected", q)
            return
        plus = frame.get_by_role("button", name=S.INCREASE_RE)
        visible = []
        for i in range(min(await plus.count(), 10)):
            if await plus.nth(i).is_visible():
                visible.append(plus.nth(i))
        if len(visible) > 1:
            raise Abort("several_tickets", f"{len(visible)} ticket types")
        if visible:
            current = await self._stepper_value(frame)
            clicks = q - current if current is not None else q
            for _ in range(max(0, clicks)):
                await visible[0].click(timeout=5000)
                await asyncio.sleep(0.2)
            after = await self._stepper_value(frame)
            if after is not None and after != q:
                raise Abort("quantity", f"stepper shows {after}, wanted {q}")
            log.info("Quantity %s set with stepper (was %s)", q, current)
            return
        log.info("No quantity control: assuming a single preselected ticket")

    @staticmethod
    async def _stepper_value(frame) -> int | None:  # noqa: ANN001
        try:
            loc = frame.locator("input[type=number], [role=spinbutton]")
            if await loc.count() != 1:
                return None
            el = loc.first
            val = await el.input_value() if await el.evaluate("e => 'value' in e") else await el.get_attribute(
                "aria-valuenow")
            return int(str(val).strip())
        except Exception:  # noqa: BLE001
            return None

    async def _missing_required(self, frame) -> list[str]:  # noqa: ANN001
        js = """() => Array.from(document.querySelectorAll('input,select,textarea')).filter(el => {
            const req = el.required || el.getAttribute('aria-required') === 'true';
            if (!req || el.disabled || el.type === 'hidden') return false;
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return false;
            if (el.type === 'checkbox') return !el.checked;
            if (el.type === 'radio') return !document.querySelector(
                'input[type=radio][name="' + CSS.escape(el.name) + '"]:checked');
            return !String(el.value || '').trim();
        }).map(el => el.getAttribute('aria-label') || el.name || el.id || el.type)"""
        try:
            return list(dict.fromkeys(await frame.evaluate(js)))
        except Exception:  # noqa: BLE001
            return []

    async def _is_success(self) -> bool:
        if S.is_success(self.page.url, None):
            return True
        for f in self.page.frames:
            if S.is_success(f.url, await self._text(f)):
                return True
        return False

    def _refresh_frame(self, frame):  # noqa: ANN001, ANN202
        """The checkout iframe can be replaced after a step; fall back to the current one."""
        try:
            if not frame.is_detached():
                return frame
        except Exception:  # noqa: BLE001
            pass
        return self._checkout_frame() or self.page.main_frame

    async def _advance(self, frame) -> None:  # noqa: ANN001
        seen_free = False
        for step in range(MAX_STEPS):
            frame = self._refresh_frame(frame)
            await self._guard()
            if await self._is_success():
                return
            if any(S.is_payment_url(f.url) for f in self.page.frames):
                raise Abort("paid", "payment form")
            text = await self._text(frame)
            if S.has_payment_form(text):
                raise Abort("paid", "payment form")
            status, detail = S.assess_price(text)
            if status == "paid":
                raise Abort("paid", detail)
            seen_free = seen_free or status == "free"
            missing = await self._missing_required(frame)
            if missing:
                raise Abort("extra_fields", "empty: " + ", ".join(missing[:5]))
            btn = await self._first_usable(frame.get_by_role("button", name=S.ADVANCE_RE))
            if btn is None:
                if step > 0 and await self._wait_success(15):
                    return
                raise Abort("unexpected_page", f"no Register/Continue button (step {step + 1})")
            if not seen_free:
                raise Abort("not_free", detail)
            label = (await btn.inner_text()).strip()
            log.info("Step %d: pressing %r", step + 1, label)
            self.submitted = True  # until the checkout shows another step, assume this click placed the order
            await btn.click(timeout=5000)
            outcome = await self._wait_after_click(frame, text)
            if outcome == "success":
                return
            if outcome == "step":
                self.submitted = False
        raise Abort("unexpected_page", f"still no confirmation after {MAX_STEPS} steps")

    async def _wait_success(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            await self._guard()
            if await self._is_success():
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_after_click(self, frame, before: str) -> str:  # noqa: ANN001
        """'success', 'step' (the checkout shows a new step) or 'timeout' after CLICK_SETTLE_S."""
        start = time.monotonic()
        while time.monotonic() - start < CLICK_SETTLE_S:
            await asyncio.sleep(0.4)
            await self._guard()
            if await self._is_success():
                return "success"
            if time.monotonic() - start < 0.8:
                continue
            frame = self._refresh_frame(frame)
            now = await self._text(frame)
            if now and now != before and await self._first_usable(frame.get_by_role("button", name=S.ADVANCE_RE)):
                await asyncio.sleep(0.5)  # let the new step finish rendering
                return "step"
        return "timeout"

    # ---------------------------------------------------------------- slots
    def _ref_day(self):  # noqa: ANN202
        return SL.constraint_day(self.job.constraints) or self.clock().astimezone(self.cfg.tz).date()

    async def _ticket_rows(self, frame) -> list[SL.Slot | None]:  # noqa: ANN001
        """Every ticket class with a quantity control, parsed as a slot (None when its name has no time)."""
        try:
            rows = await frame.evaluate(_ROWS_JS, S.INCREASE_RE.pattern)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for r in rows or []:
            out.append(SL.parse_slot(r.get("text", ""), self._ref_day(), self.cfg.tz, index=int(r["i"]),
                                     kind=r.get("kind", "")))
        return out

    async def _picker_slots(self, frame, selector: str) -> list[SL.Slot]:  # noqa: ANN001
        try:
            items = await frame.evaluate(_PICKER_JS, [selector, S.SLOT_TIME_JS])
        except Exception:  # noqa: BLE001
            return []
        out = []
        for it in items or []:
            sl = SL.parse_slot(it.get("text", ""), self._ref_day(), self.cfg.tz, index=int(it["i"]), kind="pick",
                               available=None if not it.get("disabled") else False)
            if sl is not None:
                out.append(sl)
        return out

    async def _click_date(self, frame) -> bool:  # noqa: ANN001
        day = SL.constraint_day(self.job.constraints)
        if day is None:
            return False
        try:
            i = await frame.evaluate(_DATE_JS, [SL.date_patterns(day), str(day.day), S.CALENDAR_SELECTOR])
        except Exception:  # noqa: BLE001
            return False
        if i is None:
            return False
        try:
            await frame.locator(f'[data-scout-date="{i}"]').first.click(timeout=5000)
        except Exception:  # noqa: BLE001
            return False
        log.info("Picked date %s in the date picker", day)
        return True

    def _no_fit(self, rejected) -> None:  # noqa: ANN001
        raise Abort("no_slot_fits", SL.describe(rejected, self.cfg.tz))

    def _remember(self, slot: SL.Slot) -> None:
        self.chosen = slot
        log.info("Chose slot %s (%s)", slot.short(self.cfg.tz), slot.label)

    async def _pick_from_picker(self, frame, selector: str) -> bool:  # noqa: ANN001
        """Click the best time button in a date/time picker. False when there's no picker."""
        picks = await self._picker_slots(frame, selector)
        if not picks and await self._click_date(frame):
            end = time.monotonic() + 6
            while not picks and time.monotonic() < end:
                await asyncio.sleep(0.4)
                picks = await self._picker_slots(frame, selector)
        if not picks:
            return False
        chosen, rejected = SL.choose(picks, self.job.constraints, self.cfg.tz)
        if chosen is None:
            self._no_fit(rejected)
        await frame.locator(f'[data-scout-pick="{chosen.index}"]').first.click(timeout=5000)
        self._remember(chosen)
        await asyncio.sleep(0.8)  # the picker re-renders (ticket list / continue button)
        return True

    async def _pick_slot(self, frame) -> bool:  # noqa: ANN001
        """Timed ticket classes: set the quantity on the chosen one and return True (quantity done).
        Picker: click the time and return False (the quantity step still runs). No slots: False."""
        rows = await self._ticket_rows(frame)
        timed = [r for r in rows if r is not None]
        if timed:
            chosen, rejected = SL.choose(timed, self.job.constraints, self.cfg.tz)
            if chosen is None:
                self._no_fit(rejected)
            await self._set_row_quantity(frame, chosen)
            self._remember(chosen)
            return True
        if len(rows) > 1 or self.chosen is not None:
            return False  # several untimed ticket types -> _select_quantity stops as before
        await self._pick_from_picker(frame, S.SLOT_PICK_SELECTOR_CHECKOUT)
        return False

    async def _set_row_quantity(self, frame, slot: SL.Slot) -> None:  # noqa: ANN001
        q = self.job.quantity
        el = frame.locator(f'[data-scout-slot="{slot.index}"]').first
        if slot.kind == "select":
            values = await el.evaluate("el => Array.from(el.options).map(o => o.value)")
            if str(q) not in values:
                raise Abort("quantity", f"wanted {q}, options {values[:8]}")
            await el.select_option(str(q), timeout=5000)
        else:
            for _ in range(q):
                await el.click(timeout=5000)
                await asyncio.sleep(0.2)
        log.info("Quantity %s set on slot %s", q, slot.label)

    # --------------------------------------------------------------- ticket
    def _ticket_path(self, ext: str) -> Path:
        self.cfg.tickets_dir.mkdir(parents=True, exist_ok=True)
        return self.cfg.tickets_dir / f"{self.job.event_id}.{ext}"

    async def _qr_shot(self, page) -> Path | None:  # noqa: ANN001
        for f in page.frames:
            for sel in S.TICKET_QR_SELECTORS:
                try:
                    el = await self._first_usable(f.locator(sel), need_enabled=False)
                    if el is None:
                        continue
                    box = await el.bounding_box()
                    if not box or box["width"] < 60 or box["height"] < 60:
                        continue  # icons, not a scannable code
                    path = self._ticket_path("png")
                    await el.screenshot(path=str(path), timeout=5000)
                    log.info("Ticket QR captured with %s", sel)
                    return path
                except Exception:  # noqa: BLE001
                    continue
        return None

    async def _find_link(self, page, pattern):  # noqa: ANN001, ANN202
        for f in page.frames:
            for role in ("link", "button"):
                el = await self._first_usable(f.get_by_role(role, name=pattern))
                if el is not None:
                    return el
        return None

    async def _capture_ticket(self) -> Path | None:
        """(a) QR on the confirmation page, (b) QR on the order's "Ver entradas" page, (c) ticket PDF."""
        page = self.page
        await asyncio.sleep(1.5)  # let the confirmation render its ticket
        if (p := await self._qr_shot(page)) is not None:
            return p
        link = await self._find_link(page, S.MY_TICKETS_RE)
        if link is not None:
            try:
                href = await link.get_attribute("href")
                target = page
                if href and href.startswith(("http", "/")):
                    target = await self.ctx.new_page()
                    await target.goto(href if href.startswith("http") else self.cfg.base_url + href,
                                      wait_until="domcontentloaded", timeout=20_000)
                else:
                    await link.click(timeout=5000)
                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                await asyncio.sleep(2)
                if (p := await self._qr_shot(target)) is not None:
                    return p
                if (p := await self._download_pdf(target)) is not None:
                    return p
            except Exception:  # noqa: BLE001
                log.warning("My-tickets page didn't give a QR", exc_info=True)
        return await self._download_pdf(page)

    async def _download_pdf(self, page) -> Path | None:  # noqa: ANN001
        el = await self._find_link(page, S.DOWNLOAD_TICKET_RE)
        if el is None:
            for f in page.frames:
                el = await self._first_usable(f.locator("a[href*='.pdf' i], a[href*='pdf' i][href*='ticket' i]"),
                                              need_enabled=False)
                if el is not None:
                    break
        if el is None:
            return None
        href = await el.get_attribute("href")
        if href and href.startswith(("http", "/")):
            url = href if href.startswith("http") else self.cfg.base_url + href
            r = await self.ctx.request.get(url, timeout=20_000)
            body = await r.body()
            if r.ok and body[:4] == b"%PDF":
                path = self._ticket_path("pdf")
                path.write_bytes(body)
                log.info("Ticket PDF downloaded")
                return path
        async with page.expect_download(timeout=20_000) as info:
            await el.click(timeout=5000)
        dl = await info.value
        path = self._ticket_path("pdf")
        await dl.save_as(str(path))
        log.info("Ticket PDF downloaded via click")
        return path


TICKET_BUDGET_S = 60

# Marks each ticket-class quantity control with data-scout-slot=i and returns the text of its row
# (the largest ancestor that contains no other quantity control).
_ROWS_JS = r"""(incSrc) => {
  const inc = new RegExp(incSrc, 'i');
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const isQty = s => { const v = Array.from(s.options).map(o => o.value.trim()).filter(x => x !== '');
                       return v.length >= 2 && v.every(x => /^\d+$/.test(x) && +x <= 100); };
  const controls = [];
  document.querySelectorAll('select').forEach(s => { if (vis(s) && isQty(s)) controls.push([s, 'select']); });
  document.querySelectorAll('button,[role=button]').forEach(b => {
    const n = (b.getAttribute('aria-label') || b.innerText || '').trim();
    if (vis(b) && inc.test(n)) controls.push([b, 'stepper']);
  });
  const all = controls.map(c => c[0]);
  return controls.map(([el, kind], i) => {
    let box = el;
    while (box.parentElement && box.parentElement !== document.body) {
      const p = box.parentElement;
      if (all.some(o => o !== el && p.contains(o))) break;
      box = p;
      if ((box.innerText || '').length > 400) break;
    }
    el.setAttribute('data-scout-slot', String(i));
    return {i, kind, text: (box.innerText || '').slice(0, 300)};
  });
}"""

# Marks each clickable time-slot button with data-scout-pick=i (innermost matches only).
_PICKER_JS = r"""([selector, timeSrc]) => {
  const re = new RegExp(timeSrc, 'i');
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const label = el => ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')).trim();
  const found = Array.from(document.querySelectorAll(selector)).filter(el => {
    const t = label(el); return vis(el) && t.length <= 90 && re.test(t);
  });
  const inner = found.filter(el => !found.some(o => o !== el && el.contains(o)));
  return inner.slice(0, 80).map((el, i) => {
    el.setAttribute('data-scout-pick', String(i));
    const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true' ||
                     /disabled|unavailable|sold/i.test(el.className || '');
    return {i, text: label(el), disabled};
  });
}"""

# Marks the date-picker cell for the wanted day with data-scout-date=0; returns 0 or null.
_DATE_JS = r"""([patterns, dayNum, calSel]) => {
  const res = patterns.map(p => new RegExp(p, 'i'));
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const usable = el => vis(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  const sel = "button, [role=button], [role=gridcell], [role=option], [role=radio], [role=tab], td";
  const els = Array.from(document.querySelectorAll(sel)).filter(usable);
  const label = el => ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')).trim();
  let hit = els.find(el => res.some(r => r.test(label(el))));
  if (!hit) hit = els.find(el => (el.innerText || '').trim() === dayNum && el.closest(calSel));
  if (!hit) return null;
  hit.setAttribute('data-scout-date', '0');
  return 0;
}"""


async def book(ctx, job: Job, cfg: Config, check_login: LoginCheck, on_abort: AbortHook) -> BookResult:  # noqa: ANN001
    return await Attempt(ctx, job, cfg, check_login, on_abort).run()

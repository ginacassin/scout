"""The booking attempt, run once per job starting at opens_at - preopen.

  1. T-90s  check the profile is logged in, open the event page.
  2. T      poll the page (every poll_ms, reloading every reload_every_s) until the
            Reserve / Get tickets button is enabled, click it and find the checkout
            (iframe `/checkout-external` or a full-page checkout). If no button ever shows,
            open checkout-external?eid= directly.
  3.        pick the quantity (exactly one ticket type, otherwise stop).
  4.        loop: success? -> done.  captcha / payment form / non-zero amount / empty required
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
            return BookResult(True, "booked", screenshot=shot, submitted=True)
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
        return BookResult(False, code, detail, shot, self.submitted)

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
        return await self._first_usable(frame.get_by_role("button", name=S.ADVANCE_RE)) is not None

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


async def book(ctx, job: Job, cfg: Config, check_login: LoginCheck, on_abort: AbortHook) -> BookResult:  # noqa: ANN001
    return await Attempt(ctx, job, cfg, check_login, on_abort).run()

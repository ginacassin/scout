"""One persistent Chromium profile (/profile/chromium) shared by bookings, login checks and login mode.

Chromium locks its profile, so there is exactly one BrowserContext at a time; users borrow it with
`async with manager.context()` and it closes when the last user is done (frees ~400 MB on the Pi).
It always runs headed on the Xvfb display: fewer bot checks than headless, and login mode simply
shows the same display over noVNC.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from typing import AsyncIterator

from . import eb_selectors as S
from .config import Config

log = logging.getLogger(__name__)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--password-store=basic",
    "--start-maximized",
]


class BrowserManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self._ctx = None
        self._users = 0
        self._lock = asyncio.Lock()
        self.version: str | None = None
        self.logged_in: bool | None = None
        self.login_active = False
        self._login_page = None
        self._vnc_procs: list[asyncio.subprocess.Process] = []
        self._login_timer: asyncio.Task | None = None

    # ------------------------------------------------------------------ context
    def _clear_stale_locks(self) -> None:
        # Left behind if the container was killed; Chromium then refuses the profile ("in use").
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = self.cfg.chromium_dir / name
            if p.is_symlink() or p.exists():
                p.unlink(missing_ok=True)

    async def _open(self) -> None:
        from playwright.async_api import async_playwright

        self.cfg.chromium_dir.mkdir(parents=True, exist_ok=True)
        self._clear_stale_locks()
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(self.cfg.chromium_dir),
            headless=False,
            args=LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"],
            locale="es-ES",
            timezone_id=str(self.cfg.tz),
            viewport={"width": 1280, "height": 860},
            env={**os.environ, "DISPLAY": self.cfg.display},
        )
        self._ctx.on("close", lambda _ctx: self._on_closed())
        try:
            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            ua = await page.evaluate("navigator.userAgent")
            self.version = next((t for t in ua.split() if t.startswith(("Chrome/", "HeadlessChrome/"))), ua[:60])
        except Exception:  # noqa: BLE001
            pass
        log.info("Chromium started (%s)", self.version)

    def _on_closed(self) -> None:
        self._ctx = None

    async def _close(self) -> None:
        ctx, pw = self._ctx, self._pw
        self._ctx = self._pw = None
        try:
            if ctx:
                await ctx.close()
        finally:
            if pw:
                await pw.stop()
        log.info("Chromium closed")

    @asynccontextmanager
    async def context(self) -> AsyncIterator:
        async with self._lock:
            if self._ctx is None:
                if self._pw is not None:  # browser crashed: clean up the driver first
                    await self._pw.stop()
                    self._pw = None
                await self._open()
            self._users += 1
            ctx = self._ctx
        try:
            yield ctx
        finally:
            async with self._lock:
                self._users -= 1
                if self._users == 0 and not self.login_active and self._ctx is not None:
                    await self._close()

    @property
    def state(self) -> str:
        if self._ctx is None:
            return "closed"
        return "login mode" if self.login_active else "open"

    # -------------------------------------------------------------- login check
    async def check_login(self, ctx=None) -> bool | None:  # noqa: ANN001
        """Open the account page; Eventbrite redirects to /signin when the session is gone."""
        if ctx is None:
            async with self.context() as c:
                return await self.check_login(c)
        page = await ctx.new_page()
        result: bool | None = None
        try:
            await page.goto(self.cfg.base_url + S.LOGIN_CHECK_PATH, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1500)  # client-side redirects
            text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 5000) : ''")
            result = S.login_state(page.url, text)
            log.info("Login check: %s (%s)", result, page.url)
        except Exception:  # noqa: BLE001
            log.exception("Login check failed")
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
        if result is not None:
            self.logged_in = result
        return result

    # --------------------------------------------------------------- login mode
    async def login_start(self) -> None:
        """Show Chromium at the Eventbrite sign-in page over noVNC. The user types their password
        into Eventbrite themselves; we never see it. The session stays in the profile."""
        if self.login_active:
            return
        async with self._lock:
            if self._ctx is None:
                await self._open()
            self._users += 1
            self.login_active = True
        try:
            self._login_page = await self._ctx.new_page()
            await self._login_page.goto(self.cfg.base_url + "/signin/", wait_until="domcontentloaded", timeout=30_000)
            await self._login_page.bring_to_front()
        except Exception:  # noqa: BLE001
            log.exception("Opening the sign-in page failed (the window is still usable over VNC)")
        await self._start_vnc()
        self._login_timer = asyncio.get_running_loop().create_task(self._login_autostop())

    async def _login_autostop(self) -> None:
        await asyncio.sleep(self.cfg.login_timeout_s)
        log.info("Login mode timed out")
        await self.login_stop()

    async def login_stop(self) -> None:
        if not self.login_active:
            return
        if self._login_timer and self._login_timer is not asyncio.current_task():
            self._login_timer.cancel()
        self._login_timer = None
        await self._stop_vnc()
        try:
            if self._login_page:
                await self._login_page.close()
        except Exception:  # noqa: BLE001
            pass
        self._login_page = None
        async with self._lock:
            self.login_active = False
            self._users -= 1
            if self._users == 0 and self._ctx is not None:
                await self._close()

    async def _start_vnc(self) -> None:
        vnc = ["x11vnc", "-display", self.cfg.display, "-rfbport", "5900", "-localhost", "-forever", "-shared",
               "-quiet", "-noxdamage"]
        vnc += ["-passwd", self.cfg.vnc_password] if self.cfg.vnc_password else ["-nopw"]
        web = ["websockify", "--web", "/usr/share/novnc", "6080", "localhost:5900"]
        for cmd in (vnc, web):
            try:
                self._vnc_procs.append(await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
            except FileNotFoundError:
                log.error("%s is not installed", cmd[0])
        log.info("noVNC on :6080 (%s)", self.cfg.vnc_public_url)

    async def _stop_vnc(self) -> None:
        for p in self._vnc_procs:
            if p.returncode is None:
                p.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except asyncio.TimeoutError:
                    p.kill()
        self._vnc_procs = []

    async def shutdown(self) -> None:
        await self._stop_vnc()
        if self._ctx is not None or self._pw is not None:
            await self._close()

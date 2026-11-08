"""HTTP API on :8100 (Docker network only; Scout reaches it as http://scout-booker:8100).

POST   /book            {event_id, eventbrite_id, event_url, opens_at, title, quantity?, constraints?}
DELETE /book/{event_id}
GET    /jobs
GET    /jobs/{event_id}/ticket   -> the captured QR png / ticket pdf
GET    /health          -> {ok, logged_in, browser}
POST   /check-login     -> {logged_in}
POST   /login/start     -> {ok, vnc_url}
POST   /login/stop      -> {ok}
Every request needs header X-Booker-Token == BOOKER_TOKEN (when BOOKER_TOKEN is set).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from dataclasses import asdict
from datetime import timedelta

from aiohttp import web

from . import flow
from .browser import BrowserManager
from .config import Config
from .jobs import Job, JobLocked, JobStore, Scheduler, parse_iso, utcnow
from .notify import Telegram

log = logging.getLogger(__name__)

_HM = re.compile(r"^\d{1,2}:\d{2}$")


def clean_constraints(raw) -> dict | None:  # noqa: ANN001
    """Validate the planner's constraint object; raises ValueError when it's malformed."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("constraints must be an object")
    out: dict = {}
    if raw.get("date") is not None:
        from datetime import date
        out["date"] = date.fromisoformat(str(raw["date"])).isoformat()
    for k in ("earliest", "latest"):
        v = raw.get(k)
        if v is not None:
            if not _HM.match(str(v)):
                raise ValueError(f"{k} must be HH:MM")
            out[k] = str(v)
        else:
            out[k] = None
    avoid = []
    for pair in raw.get("avoid") or []:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(_HM.match(str(x)) for x in pair)):
            raise ValueError("avoid must be a list of [HH:MM, HH:MM]")
        avoid.append([str(pair[0]), str(pair[1])])
    out["avoid"] = avoid
    out["prefer"] = "latest" if raw.get("prefer") == "latest" else "earliest"
    out["visit_minutes"] = max(0, min(240, int(raw.get("visit_minutes") or 30)))
    if raw.get("tz"):
        out["tz"] = str(raw["tz"])[:64]
    return out


class Booker:
    def __init__(self, cfg: Config, browser: BrowserManager | None = None, tg: Telegram | None = None,
                 store: JobStore | None = None):
        self.cfg = cfg
        self.browser = browser or BrowserManager(cfg)
        self.tg = tg or Telegram(cfg.bot_token, cfg.chat_id, cfg.tz, cfg.base_url)
        self.store = store or JobStore(cfg.jobs_path)
        self.scheduler = Scheduler(self.store, self.run_job, preopen_s=cfg.preopen_s)
        self._bg: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:  # noqa: ANN001
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------------- jobs
    async def run_job(self, job: Job) -> tuple[str, str]:
        warned = False

        async def on_abort(code: str, detail: str, submitted: bool) -> None:
            nonlocal warned
            warned = True
            await self.tg.manual(job, code, detail, maybe_done=submitted)

        try:
            async with self.browser.context() as ctx:
                res = await flow.book(ctx, job, self.cfg,
                                      check_login=lambda: self.browser.check_login(ctx), on_abort=on_abort)
        except Exception as exc:  # noqa: BLE001  (browser didn't start)
            log.exception("Job %s: browser failed", job.event_id)
            if not warned:
                await self.tg.manual(job, "error", f"browser didn't start: {str(exc)[:120]}")
            return "failed:error", str(exc)[:200]
        if res.slot is not None and res.slot.start is not None:
            self.store.set_result(job.event_id, res.slot.start.isoformat(),
                                  res.slot.end.isoformat() if res.slot.end else "", res.slot.label)
        if res.ok:
            if res.ticket is not None:
                self.store.set_result(job.event_id, ticket=res.ticket.name)
            await self.tg.booked(job, res.screenshot)
            return "booked", res.detail
        if res.screenshot:
            await self.tg.photo(res.screenshot, "What the booker saw")
        return f"failed:{res.code}", res.detail

    async def startup(self) -> None:
        for job in self.store.recover(self.cfg.window_s):
            code = job.state.split(":", 1)[1]
            # Only nag about recent ones; an ancient missed job isn't actionable.
            if utcnow() - job.opens < timedelta(hours=12):
                await self.tg.manual(job, code, job.detail, maybe_done=code == "interrupted")
        self.scheduler.start()
        self._spawn(self._initial_login_check())

    async def _initial_login_check(self) -> None:
        try:
            await self.browser.check_login()
        except Exception:  # noqa: BLE001
            log.exception("Initial login check failed")

    async def _announce(self, job: Job) -> None:
        if self.browser.logged_in is None:
            try:
                await self.browser.check_login()
            except Exception:  # noqa: BLE001
                log.exception("Login check failed")
        await self.tg.armed(job, self.browser.logged_in)

    # -------------------------------------------------------------- handlers
    async def post_book(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            event_id = int(body["event_id"])
            eventbrite_id = str(body["eventbrite_id"]).strip()
            event_url = str(body.get("event_url") or "").strip()
            opens = parse_iso(str(body["opens_at"]))
            title = str(body.get("title") or f"event {event_id}")[:200]
            quantity = int(body.get("quantity") or self.cfg.quantity)
            constraints = clean_constraints(body.get("constraints"))
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"bad request: {exc}"}, status=400)
        if not eventbrite_id.isdigit():
            return web.json_response({"error": "eventbrite_id must be numeric"}, status=400)
        if not event_url.startswith("https://"):
            event_url = f"{self.cfg.base_url}/e/{eventbrite_id}"
        if utcnow() > opens + timedelta(seconds=self.cfg.window_s):
            return web.json_response({"error": "sales already opened"}, status=400)
        quantity = max(1, min(10, quantity))
        prev = self.store.get(event_id)
        prev_core = ((prev.opens_at, prev.eventbrite_id, prev.event_url, prev.quantity)
                     if prev is not None and prev.state == "scheduled" else None)
        try:
            job, changed = self.store.arm(event_id, eventbrite_id, event_url, opens, title, quantity, constraints)
        except JobLocked as exc:
            return web.json_response({"error": "already attempted", "job": asdict(exc.job)}, status=409)
        core_changed = prev_core != (job.opens_at, job.eventbrite_id, job.event_url, job.quantity)
        if changed and core_changed:
            self.scheduler.schedule(job)
            self._spawn(self._announce(job))
        elif changed:
            log.info("Job %s: new route constraints %s", event_id, constraints)  # re-plan; no new message
            if event_id not in self.scheduler.tasks:
                self.scheduler.schedule(job)
        elif event_id not in self.scheduler.tasks:
            self.scheduler.schedule(job)
        return web.json_response({"job": asdict(job), "changed": changed})

    async def delete_book(self, request: web.Request) -> web.Response:
        try:
            event_id = int(request.match_info["event_id"])
        except ValueError:
            return web.json_response({"error": "bad event id"}, status=400)
        try:
            job = self.store.cancel(event_id)
        except JobLocked as exc:
            return web.json_response({"error": "too late to cancel", "job": asdict(exc.job)}, status=409)
        if job is None:
            return web.json_response({"error": "not found"}, status=404)
        self.scheduler.unschedule(event_id)
        return web.json_response({"job": asdict(job)})

    async def get_jobs(self, request: web.Request) -> web.Response:
        return web.json_response({"jobs": [asdict(j) for j in self.store.all()]})

    async def get_ticket(self, request: web.Request) -> web.StreamResponse:
        try:
            event_id = int(request.match_info["event_id"])
        except ValueError:
            return web.json_response({"error": "bad event id"}, status=400)
        job = self.store.get(event_id)
        if job is None or not job.ticket:
            return web.json_response({"error": "no ticket"}, status=404)
        path = (self.cfg.tickets_dir / job.ticket).resolve()
        if path.parent != self.cfg.tickets_dir.resolve() or not path.is_file():
            return web.json_response({"error": "no ticket"}, status=404)
        ctype = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png"
        return web.FileResponse(path, headers={"Content-Type": ctype,
                                               "Content-Disposition": f'attachment; filename="{path.name}"'})

    async def get_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "logged_in": self.browser.logged_in,
                                  "browser": f"chromium {self.browser.version or '?'} ({self.browser.state})"})

    async def post_check_login(self, request: web.Request) -> web.Response:
        return web.json_response({"logged_in": await self.browser.check_login()})

    async def post_login_start(self, request: web.Request) -> web.Response:
        await self.browser.login_start()
        return web.json_response({"ok": True, "vnc_url": self.cfg.vnc_public_url,
                                  "closes_in_s": self.cfg.login_timeout_s})

    async def post_login_stop(self, request: web.Request) -> web.Response:
        await self.browser.login_stop()
        return web.json_response({"ok": True})


def token_middleware(token: str):  # noqa: ANN201
    @web.middleware
    async def mw(request: web.Request, handler):  # noqa: ANN001, ANN202
        if token and not hmac.compare_digest(request.headers.get("X-Booker-Token", ""), token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return mw


def make_app(booker: Booker) -> web.Application:
    app = web.Application(middlewares=[token_middleware(booker.cfg.token)])
    app.add_routes([
        web.post("/book", booker.post_book),
        web.delete("/book/{event_id}", booker.delete_book),
        web.get("/jobs", booker.get_jobs),
        web.get("/jobs/{event_id}/ticket", booker.get_ticket),
        web.get("/health", booker.get_health),
        web.post("/check-login", booker.post_check_login),
        web.post("/login/start", booker.post_login_start),
        web.post("/login/stop", booker.post_login_stop),
    ])

    async def on_startup(_app: web.Application) -> None:
        await booker.startup()

    async def on_cleanup(_app: web.Application) -> None:
        await booker.browser.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

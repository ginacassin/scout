"""HTTP API with a fake browser and a fake Telegram (no Chromium, no network)."""

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from scoutbooker.config import Config  # noqa: E402
from scoutbooker.jobs import JobStore, utcnow  # noqa: E402
from scoutbooker.server import Booker, make_app  # noqa: E402


class FakeBrowser:
    logged_in = True
    version = "Chrome/1"
    state = "closed"

    def __init__(self):
        self.login = []

    async def check_login(self, ctx=None):  # noqa: ANN001
        return self.logged_in

    async def login_start(self):
        self.login.append("start")

    async def login_stop(self):
        self.login.append("stop")

    async def shutdown(self):
        pass


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def armed(self, job, logged_in):  # noqa: ANN001
        self.sent.append(("armed", job.event_id))

    async def manual(self, job, code, detail="", maybe_done=False):  # noqa: ANN001
        self.sent.append(("manual", job.event_id, code))

    async def booked(self, job, shot):  # noqa: ANN001
        self.sent.append(("booked", job.event_id))

    async def photo(self, path, caption):  # noqa: ANN001
        pass


@pytest.fixture
async def client(tmp_path):
    cfg = Config(token="s3cret", profile_dir=tmp_path)
    booker = Booker(cfg, browser=FakeBrowser(), tg=FakeTelegram(), store=JobStore(cfg.jobs_path))
    c = TestClient(TestServer(make_app(booker)), headers={"X-Booker-Token": "s3cret"})
    await c.start_server()
    c.booker = booker
    yield c
    for t in list(booker.scheduler.tasks.values()):
        t.cancel()
    await c.close()


OPENS = (utcnow() + timedelta(days=1)).replace(microsecond=0).isoformat()


def body(**kw):  # noqa: ANN003
    d = {"event_id": 7, "eventbrite_id": "987654321", "event_url": "https://www.eventbrite.es/e/m-987654321",
         "opens_at": OPENS, "title": "Mercadillo"}
    d.update(kw)
    return d


async def test_token_required(client):
    r = await client.get("/health", headers={"X-Booker-Token": "wrong"})
    assert r.status == 401
    r = await client.get("/health")
    assert (await r.json())["logged_in"] is True


async def test_book_list_cancel(client):
    r = await client.post("/book", json=body())
    assert r.status == 200
    job = (await r.json())["job"]
    assert job["state"] == "scheduled" and job["quantity"] == 1
    assert 7 in client.booker.scheduler.tasks
    r = await client.post("/book", json=body())  # idempotent
    assert (await r.json())["changed"] is False

    jobs = (await (await client.get("/jobs")).json())["jobs"]
    assert [j["event_id"] for j in jobs] == [7]

    r = await client.delete("/book/7")
    assert (await r.json())["job"]["state"] == "cancelled"
    assert 7 not in client.booker.scheduler.tasks
    assert (await client.delete("/book/8")).status == 404


async def test_book_rejects_bad_input(client):
    assert (await client.post("/book", json=body(eventbrite_id="abc"))).status == 400
    assert (await client.post("/book", json=body(opens_at=(utcnow() - timedelta(hours=1)).isoformat()))).status == 400
    assert (await client.post("/book", json={"event_id": 1})).status == 400


async def test_attempted_job_is_locked(client):
    await client.post("/book", json=body())
    client.booker.scheduler.unschedule(7)
    client.booker.store.mark_running(7)
    client.booker.store.finish(7, "failed:captcha")
    r = await client.post("/book", json=body())
    assert r.status == 409
    assert (await client.delete("/book/7")).status == 409


async def test_login_endpoints(client):
    r = await client.post("/login/start")
    assert (await r.json())["vnc_url"].endswith("/vnc.html")
    await client.post("/login/stop")
    assert client.booker.browser.login == ["start", "stop"]
    assert (await (await client.post("/check-login")).json())["logged_in"] is True


async def test_run_job_reports_browser_failure(client):
    class Broken(FakeBrowser):
        def context(self):
            raise RuntimeError("no display")

    client.booker.browser = Broken()
    job, _ = client.booker.store.arm(9, "1", "https://x", utcnow() + timedelta(hours=1), "T", 1)
    state, _ = await client.booker.run_job(job)
    assert state == "failed:error"
    assert ("manual", 9, "error") in client.booker.tg.sent


# ------------------------------------------------------------ route constraints, slots, tickets
CONS = {"date": "2026-11-13", "earliest": "10:00", "latest": "13:00", "avoid": [["10:05", "11:55"]],
        "prefer": "earliest", "visit_minutes": 30, "tz": "Europe/Madrid"}


async def test_constraints_stored_and_replan_does_not_reannounce(client):
    r = await client.post("/book", json=body(constraints=CONS))
    assert (await r.json())["job"]["constraints"] == CONS
    await asyncio.sleep(0)
    announced = [m for m in client.booker.tg.sent if m[0] == "armed"]
    new = {**CONS, "avoid": [["10:05", "12:30"]]}
    r = await client.post("/book", json=body(constraints=new))
    data = await r.json()
    assert data["changed"] is True and data["job"]["constraints"]["avoid"] == [["10:05", "12:30"]]
    await asyncio.sleep(0)
    assert [m for m in client.booker.tg.sent if m[0] == "armed"] == announced  # no second "armed" message
    assert client.booker.store.get(7).constraints == new
    r = await client.post("/book", json=body(constraints=new))
    assert (await r.json())["changed"] is False


@pytest.mark.parametrize("bad", [{"avoid": [["10"]]}, {"earliest": "ten"}, {"date": "13/11"}, "x"])
async def test_bad_constraints_rejected(client, bad):
    assert (await client.post("/book", json=body(constraints=bad))).status == 400


async def test_run_job_records_slot_and_ticket_and_serves_it(client, monkeypatch):
    from scoutbooker import flow, slots

    booker = client.booker
    tickets = booker.cfg.tickets_dir
    tickets.mkdir(parents=True)
    (tickets / "7.png").write_bytes(b"\x89PNGqr")
    slot = slots.parse_slot("Viernes 13 · 10:00–10:30", __import__("datetime").date(2026, 11, 13), booker.cfg.tz)

    class Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):  # noqa: ANN002
            return False

    booker.browser.context = lambda: Ctx()

    async def fake_book(ctx, job, cfg, check_login, on_abort):  # noqa: ANN001
        return flow.BookResult(True, "booked", slot=slot, ticket=tickets / "7.png")

    monkeypatch.setattr(flow, "book", fake_book)
    job, _ = booker.store.arm(7, "1", "https://x", utcnow() + timedelta(hours=1), "T", 1, CONS)
    assert (await booker.run_job(job)) == ("booked", "")
    j = booker.store.get(7)
    assert (j.slot_start, j.slot_end, j.ticket) == ("2026-11-13T10:00:00+01:00", "2026-11-13T10:30:00+01:00", "7.png")

    r = await client.get("/jobs/7/ticket")
    assert r.status == 200 and r.headers["Content-Type"] == "image/png" and await r.read() == b"\x89PNGqr"
    assert (await client.get("/jobs/7/ticket", headers={"X-Booker-Token": "nope"})).status == 401
    assert (await client.get("/jobs/8/ticket")).status == 404
    j.ticket = "../jobs.json"  # never serve anything outside /profile/tickets
    assert (await client.get("/jobs/7/ticket")).status == 404


async def test_no_slot_fits_is_reported_to_the_user(client, monkeypatch):
    from scoutbooker import flow

    booker = client.booker

    class Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):  # noqa: ANN002
            return False

    booker.browser.context = lambda: Ctx()

    async def fake_book(ctx, job, cfg, check_login, on_abort):  # noqa: ANN001
        await on_abort("no_slot_fits", "Slots seen: Fri 13 10:30–11:00 (clashes with 10:05–11:55)", False)
        return flow.BookResult(False, "no_slot_fits", "Slots seen: …")

    monkeypatch.setattr(flow, "book", fake_book)
    job, _ = booker.store.arm(7, "1", "https://x", utcnow() + timedelta(hours=1), "T", 1, CONS)
    state, _ = await booker.run_job(job)
    assert state == "failed:no_slot_fits" and ("manual", 7, "no_slot_fits") in booker.tg.sent


async def test_booked_message_includes_slot():
    import httpx

    from scoutbooker.jobs import Job
    from scoutbooker.notify import Telegram

    sent = []

    def handler(request):  # noqa: ANN001
        sent.append(request.content.decode())
        return httpx.Response(200, json={"ok": True})

    tg = Telegram("1:x", 42, Config().tz, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    job = Job(7, "1", "https://x", OPENS, "Alma Market", slot_start="2026-11-13T10:00:00+01:00",
              slot_end="2026-11-13T10:30:00+01:00", ticket="7.png")
    await tg.booked(job, None)
    assert "Booked!" in sent[0] and "Fri 13 Nov 10:00–10:30" in sent[0] and "Ticket QR saved" in sent[0]

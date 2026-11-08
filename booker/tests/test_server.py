"""HTTP API with a fake browser and a fake Telegram (no Chromium, no network)."""

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

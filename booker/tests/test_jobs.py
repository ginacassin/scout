"""Job persistence, scheduling and the pure selector helpers. No browser needed."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scoutbooker import eb_selectors as S
from scoutbooker.jobs import JobLocked, JobStore, Scheduler, parse_iso

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def arm(store, event_id=1, opens=None, **kw):  # noqa: ANN001
    return store.arm(event_id, kw.get("eid", "123456789"), "https://www.eventbrite.es/e/x-123456789",
                     opens or T0 + timedelta(hours=1), kw.get("title", "Market"), kw.get("quantity", 1))


# ------------------------------------------------------------------ JobStore
def test_arm_persists_and_reloads(tmp_path):
    clock = Clock(T0)
    store = JobStore(tmp_path / "jobs.json", clock)
    job, changed = arm(store)
    assert changed and job.state == "scheduled" and job.attempts == 0
    again = JobStore(tmp_path / "jobs.json", clock)
    assert again.get(1).opens == T0 + timedelta(hours=1)
    assert again.get(1).title == "Market"


def test_rearm_identical_is_not_a_change_but_new_time_is(tmp_path):
    store = JobStore(tmp_path / "jobs.json", Clock(T0))
    arm(store)
    assert arm(store)[1] is False
    job, changed = arm(store, opens=T0 + timedelta(hours=2))
    assert changed and job.opens == T0 + timedelta(hours=2)


def test_single_attempt_ever(tmp_path):
    store = JobStore(tmp_path / "jobs.json", Clock(T0))
    arm(store)
    assert store.mark_running(1) is True
    assert store.mark_running(1) is False
    with pytest.raises(JobLocked):
        arm(store)
    store.finish(1, "booked")
    reloaded = JobStore(tmp_path / "jobs.json", Clock(T0))
    assert reloaded.mark_running(1) is False
    assert reloaded.runnable() == []


def test_cancel_rules(tmp_path):
    clock = Clock(T0)
    store = JobStore(tmp_path / "jobs.json", clock)
    assert store.cancel(99) is None
    arm(store)
    assert store.cancel(1).state == "cancelled"
    job, changed = arm(store)  # re-arming a cancelled, never-attempted job is fine
    assert changed and job.state == "scheduled"

    store.mark_running(1)
    assert store.cancel(1).state == "cancelled"  # still before opening: OK

    arm(store, event_id=2)
    store.mark_running(2)
    clock.now = T0 + timedelta(hours=1, seconds=1)  # past opening: too late
    with pytest.raises(JobLocked):
        store.cancel(2)
    store.finish(2, "failed:captcha")
    with pytest.raises(JobLocked):
        store.cancel(2)


def test_finish_does_not_override_cancel(tmp_path):
    store = JobStore(tmp_path / "jobs.json", Clock(T0))
    arm(store)
    store.mark_running(1)
    store.cancel(1)
    store.finish(1, "booked")
    assert store.get(1).state == "cancelled"


def test_recover_after_restart(tmp_path):
    clock = Clock(T0)
    store = JobStore(tmp_path / "jobs.json", clock)
    arm(store, 1, opens=T0 + timedelta(minutes=1))
    store.mark_running(1)
    arm(store, 2, opens=T0 - timedelta(minutes=10))
    arm(store, 3, opens=T0 - timedelta(minutes=1))  # inside the 3-minute window: still runnable
    arm(store, 4, opens=T0 + timedelta(days=1))
    failed = JobStore(tmp_path / "jobs.json", clock).recover(window_s=180)
    states = {j.event_id: j.state for j in failed}
    assert states == {1: "failed:interrupted", 2: "failed:missed"}
    reloaded = JobStore(tmp_path / "jobs.json", clock)
    assert [j.event_id for j in reloaded.runnable()] == [3, 4]


def test_corrupt_file_is_set_aside(tmp_path):
    p = tmp_path / "jobs.json"
    p.write_text("{not json")
    store = JobStore(p, Clock(T0))
    assert store.all() == []
    assert (tmp_path / "jobs.corrupt").exists()


def test_parse_iso_assumes_utc():
    assert parse_iso("2026-11-20T09:00:00") == T0
    assert parse_iso("2026-11-20T10:00:00+01:00") == T0
    assert parse_iso("2026-11-20T09:00:00Z") == T0


# ------------------------------------------------------------------ Scheduler
async def test_scheduler_runs_once_at_preopen(tmp_path):
    clock = Clock(T0)
    store = JobStore(tmp_path / "jobs.json", clock)
    calls = []

    async def runner(job):  # noqa: ANN001
        calls.append(clock())
        return "booked", ""

    sched = Scheduler(store, runner, preopen_s=90, clock=clock, sleep=clock.sleep)
    job, _ = arm(store, opens=T0 + timedelta(minutes=10))
    await sched.schedule(job)
    assert calls == [T0 + timedelta(minutes=10) - timedelta(seconds=90)]
    assert store.get(1).state == "booked" and store.get(1).attempts == 1

    sched.start()  # nothing runnable any more
    assert sched.tasks == {}


async def test_scheduler_runner_crash_becomes_failed(tmp_path):
    clock = Clock(T0)
    store = JobStore(tmp_path / "jobs.json", clock)

    async def runner(job):  # noqa: ANN001
        raise RuntimeError("boom")

    sched = Scheduler(store, runner, clock=clock, sleep=clock.sleep)
    job, _ = arm(store)
    await sched.schedule(job)
    assert store.get(1).state == "failed:error"
    assert "boom" in store.get(1).detail


async def test_scheduler_cancel_before_wake(tmp_path):
    store = JobStore(tmp_path / "jobs.json", Clock(T0))
    calls = []

    async def runner(job):  # noqa: ANN001
        calls.append(job)
        return "booked", ""

    never = asyncio.Event()

    async def blocking_sleep(_s):  # noqa: ANN001
        await never.wait()

    sched = Scheduler(store, runner, clock=Clock(T0), sleep=blocking_sleep)
    job, _ = arm(store)
    task = sched.schedule(job)
    await asyncio.sleep(0)
    store.cancel(1)
    sched.unschedule(1)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [] and store.get(1).state == "cancelled" and store.get(1).attempts == 0


async def test_reschedule_replaces_task(tmp_path):
    clock = Clock(T0)
    store = JobStore(tmp_path / "jobs.json", clock)
    calls = []

    async def runner(job):  # noqa: ANN001
        calls.append(job.opens)
        return "booked", ""

    never = asyncio.Event()

    async def blocking_sleep(_s):  # noqa: ANN001
        await never.wait()

    sched = Scheduler(store, runner, clock=clock, sleep=blocking_sleep)
    job, _ = arm(store)
    first = sched.schedule(job)
    await asyncio.sleep(0)
    job, _ = arm(store, opens=T0 + timedelta(hours=3))
    sched.sleep = clock.sleep
    second = sched.schedule(job)
    await second
    assert first.cancelled()
    assert calls == [T0 + timedelta(hours=3)]


# ------------------------------------------------------------------ selectors
@pytest.mark.parametrize("url,expected", [
    ("https://www.google.com/recaptcha/api2/anchor?k=x", True),
    ("https://newassets.hcaptcha.com/captcha/v1/abc", True),
    ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b", True),
    ("https://www.eventbrite.es/checkout-external?eid=1", False),
    ("", False),
    (None, False),
])
def test_is_captcha_url(url, expected):
    assert S.is_captcha_url(url) is expected


@pytest.mark.parametrize("url,expected", [
    ("https://www.eventbrite.es/checkout-external?eid=123&parent=x", True),
    ("https://www.eventbrite.com/checkout/abc", True),
    ("https://www.eventbrite.es/e/mercadillo-123", False),
    ("https://evil.example/checkout", False),
    ("about:blank", False),
])
def test_is_checkout_url(url, expected):
    assert S.is_checkout_url(url) is expected


def test_checkout_external_url():
    assert S.checkout_external_url("123") == "https://www.eventbrite.es/checkout-external?eid=123"


@pytest.mark.parametrize("raw,value", [
    ("0,00", Decimal("0.00")), ("12,50", Decimal("12.50")), ("1.234,56", Decimal("1234.56")),
    ("1,234.56", Decimal("1234.56")), ("1.000", Decimal("1000")), ("5", Decimal("5")),
])
def test_to_decimal(raw, value):
    assert S.to_decimal(raw) == value


@pytest.mark.parametrize("text,status", [
    ("Entrada general\nGratis\nTotal 0,00 €", "free"),
    ("General admission  Free\nTotal: €0.00", "free"),
    ("Resumen del pedido\nTotal\nGratis", "free"),
    ("Entrada general  12,50 €\nTotal 12,50 €", "paid"),
    ("Entrada  Gratis\nGastos de gestión 0,99 €\nTotal 0,99 €", "paid"),
    ("Donation $5", "paid"),
    ("Selecciona tus entradas\nEntrada general", "unknown"),
])
def test_assess_price(text, status):
    assert S.assess_price(text)[0] == status


@pytest.mark.parametrize("url,text,expected", [
    ("https://www.eventbrite.es/checkout-external?eid=1", "¡Gracias por tu pedido!", True),
    ("https://www.eventbrite.es/checkout-external?eid=1", "Pedido n.º 1234567890", True),
    ("https://www.eventbrite.com/checkout-external", "Order #9876543210 confirmed", True),
    ("https://www.eventbrite.es/x/order-confirmation/1", "", True),
    ("https://www.eventbrite.es/checkout-external?eid=1", "Resumen del pedido\nTotal 0,00 €\nRegistrarse", False),
    ("https://www.eventbrite.es/checkout-external?eid=1", "Información de contacto\nTu pedido", False),
])
def test_is_success(url, text, expected):
    assert S.is_success(url, text) is expected


@pytest.mark.parametrize("label", ["Reservar un sitio", "Conseguir entradas", "Get tickets", "Reserve a spot",
                                   "  Registrarse ", "Obtener entradas"])
def test_reserve_labels(label):
    assert S.RESERVE_RE.search(label)


@pytest.mark.parametrize("label", ["Seguir", "Compartir", "Más información", "Contact the organizer"])
def test_not_reserve_labels(label):
    assert not S.RESERVE_RE.search(label)


@pytest.mark.parametrize("label,ok", [("Registrarse", True), ("Realizar pedido", True), ("Continuar", True),
                                      ("Place order", True), ("Cancelar", False), ("Volver", False),
                                      ("Iniciar sesión", False)])
def test_advance_labels(label, ok):
    assert bool(S.ADVANCE_RE.search(label)) is ok


def test_quantity_options():
    assert S.looks_like_quantity_options(["0", "1", "2", "3"])
    assert not S.looks_like_quantity_options(["ES", "FR"])
    assert not S.looks_like_quantity_options(["1"])
    assert not S.looks_like_quantity_options([])


@pytest.mark.parametrize("url,text,expected", [
    ("https://www.eventbrite.es/signin/?referrer=%2Faccount-settings%2F", "", False),
    ("https://www.eventbrite.es/account-settings/", "Información de la cuenta", True),
    ("https://www.eventbrite.es/account-settings/", "Iniciar sesión\nCorreo electrónico\nContraseña", False),
    ("https://challenges.cloudflare.com/x", "", None),
    ("about:blank", "", None),
])
def test_login_state(url, text, expected):
    assert S.login_state(url, text) is expected


def test_sold_out_and_payment_text():
    assert S.is_sold_out("Entradas agotadas")
    assert not S.is_sold_out("La venta comienza el 20 nov")
    assert S.has_payment_form("Número de tarjeta")
    assert not S.has_payment_form("Información de contacto")


def test_reason_text():
    assert "captcha" in S.reason_text("captcha")
    assert S.reason_text("weird_thing") == "weird thing"

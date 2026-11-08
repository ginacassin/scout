"""Attempt._pick_slot glue with a fake Playwright frame (no browser)."""

from datetime import datetime, timezone

import pytest

from scoutbooker import flow
from scoutbooker.config import Config
from scoutbooker.jobs import Job

CONS = {"date": "2026-11-13", "earliest": "10:00", "latest": "13:00", "avoid": [["10:05", "11:55"]],
        "prefer": "earliest", "visit_minutes": 30}


class El:
    def __init__(self, log, sel):  # noqa: ANN001
        self.log, self.sel = log, sel

    @property
    def first(self):  # noqa: ANN201
        return self

    async def evaluate(self, js):  # noqa: ANN001
        return ["0", "1", "2"]

    async def select_option(self, v, timeout=None):  # noqa: ANN001
        self.log.append(("select", self.sel, v))

    async def click(self, timeout=None):  # noqa: ANN001
        self.log.append(("click", self.sel))

    async def count(self):
        return 0


class Frame:
    def __init__(self, rows=None, picks=None):  # noqa: ANN001
        self.rows, self.picks, self.log = rows or [], picks or [], []

    async def evaluate(self, js, arg=None):  # noqa: ANN001
        if js is flow._ROWS_JS:
            return self.rows
        if js is flow._PICKER_JS:
            return self.picks
        if js is flow._DATE_JS:
            return None
        raise AssertionError(js)

    def locator(self, sel):  # noqa: ANN001
        return El(self.log, sel)


def attempt(constraints=CONS):  # noqa: ANN001
    job = Job(7, "1", "https://x", "2026-11-10T09:00:00+00:00", "T", constraints=constraints)

    async def noop(*a):  # noqa: ANN002
        return None

    return flow.Attempt(None, job, Config(), noop, noop, clock=lambda: datetime(2026, 11, 10, tzinfo=timezone.utc))


async def test_ticket_class_rows_pick_route_slot():
    f = Frame(rows=[{"i": 0, "kind": "select", "text": "Viernes 13 · 10:30–11:00\nGratis"},
                    {"i": 1, "kind": "select", "text": "Viernes 13 · 10:00–10:30\nGratis"},
                    {"i": 2, "kind": "select", "text": "Viernes 13 · 12:00–12:30\nGratis"}])
    a = attempt()
    assert await a._pick_slot(f) is True
    assert f.log == [("select", '[data-scout-slot="1"]', "1")]
    assert a.chosen.start.isoformat() == "2026-11-13T10:00:00+01:00"


async def test_rows_none_fit_aborts_with_list():
    f = Frame(rows=[{"i": 0, "kind": "stepper", "text": "Viernes 13 · 10:30–11:00"}])
    with pytest.raises(flow.Abort) as e:
        await attempt()._pick_slot(f)
    assert e.value.code == "no_slot_fits" and "10:30–11:00 (clashes with 10:05–11:55)" in e.value.detail


async def test_untimed_rows_fall_through_to_quantity():
    f = Frame(rows=[{"i": 0, "kind": "select", "text": "Entrada general"},
                    {"i": 1, "kind": "select", "text": "Entrada VIP"}])
    assert await attempt()._pick_slot(f) is False and f.log == []


async def test_picker_clicks_best_time_button():
    f = Frame(picks=[{"i": 0, "text": "10:00", "disabled": True}, {"i": 1, "text": "10:30", "disabled": False},
                     {"i": 2, "text": "12:00", "disabled": False}])
    a = attempt()
    assert await a._pick_slot(f) is False  # quantity step still runs
    assert f.log == [("click", '[data-scout-pick="2"]')]
    assert a.chosen.start.strftime("%H:%M") == "12:00"

"""Timed-slot parsing and route-aware selection (pure functions)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from scoutbooker import eb_selectors as S
from scoutbooker import slots as SL

TZ = ZoneInfo("Europe/Madrid")
DAY = date(2026, 11, 13)


def at(hm: str, d: date = DAY) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


@pytest.mark.parametrize("label,start,end", [
    ("Viernes 13 · 10:00–10:30", at("10:00"), at("10:30")),
    ("vie, 13 nov, 10:00 - 10:30", at("10:00"), at("10:30")),
    ("Friday, November 13 · 10am - 10:30am", at("10:00"), at("10:30")),
    ("13/11 10:00-10:30", at("10:00"), at("10:30")),
    ("Cita 13 de noviembre de 10:15 a 10:30", at("10:15"), at("10:30")),
    ("10:00 - 11:00", at("10:00"), at("11:00")),
    ("10 a 11h", at("10:00"), at("11:00")),
    ("11:30 - 12:30pm", at("11:30"), at("12:30")),
    ("1:00 PM - 1:30 PM", at("13:00"), at("13:30")),
    ("Turno 10h", at("10:00"), None),
    ("Sábado 14 · 10:00–11:00", at("10:00", date(2026, 11, 14)), at("11:00", date(2026, 11, 14))),
    ("2026-11-14 17:00", at("17:00", date(2026, 11, 14)), None),
])
def test_parse_slot(label, start, end):
    s = SL.parse_slot(label, DAY, TZ)
    assert (s.start, s.end) == (start, end)
    assert s.available


@pytest.mark.parametrize("label", ["Entrada general", "Viernes 13", "Entrada 13 noviembre", "13.11.2026",
                                   "Pack 2 personas"])
def test_not_a_slot(label):
    assert SL.parse_slot(label, DAY, TZ) is None


def test_sold_out_label_and_iso_output():
    s = SL.parse_slot("Viernes 13 · 10:00–10:30  Agotado", DAY, TZ)
    assert not s.available
    s = SL.parse_slot("Viernes 13 · 10:00–10:30", DAY, TZ)
    assert s.start.isoformat() == "2026-11-13T10:00:00+01:00"


def test_weekday_day_resolves_to_neighbouring_month():
    s = SL.parse_slot("Martes 1 · 10:00–10:30", DAY, TZ)
    assert s.start.date() == date(2026, 12, 1)


CONS = {"date": "2026-11-13", "earliest": "10:00", "latest": "13:00", "avoid": [["10:05", "11:55"]],
        "prefer": "earliest", "visit_minutes": 30}


def slots(*labels):  # noqa: ANN002
    return [SL.parse_slot(l, DAY, TZ, index=i) for i, l in enumerate(labels)]


def test_choose_earliest_that_fits_the_route():
    got, rejected = SL.choose(slots("Viernes 13 · 10:30–11:00", "Viernes 13 · 09:30–10:00",
                                    "Viernes 13 · 10:00–10:30", "Viernes 13 · 12:00–12:30"), CONS, TZ)
    assert got.start == at("10:00") and got.index == 2
    assert [why for _, why in rejected] == ["before 10:00"]


def test_choose_skips_sold_out_other_day_and_clashes():
    got, rejected = SL.choose(slots("Viernes 13 · 10:00–10:30 Agotado", "Jueves 12 · 12:00–12:30",
                                    "Viernes 13 · 10:30–11:00", "Viernes 13 · 11:55–12:25"), CONS, TZ)
    assert got.start == at("11:55")
    whys = {s.short(TZ): why for s, why in rejected}
    assert whys == {"Fri 13 10:00–10:30": "sold out", "Thu 12 12:00–12:30": "other day",
                    "Fri 13 10:30–11:00": "clashes with 10:05–11:55"}


def test_long_slot_must_end_in_time_before_next_market():
    cons = {**CONS, "avoid": [["10:15", "12:00"]]}
    # 1-h slot from 10:00: you stay until 11:00, 30' later than a 30' visit -> 10:30 > 10:15 -> clash
    assert not SL.check(SL.parse_slot("10:00 - 11:00", DAY, TZ), cons, TZ)[0]
    assert SL.check(SL.parse_slot("10:00 - 10:30", DAY, TZ), cons, TZ)[0]
    assert SL.check(SL.parse_slot("10:00 - 10:45", DAY, TZ), cons, TZ)[0]  # 15' overrun: 10:15 still ok
    assert not SL.check(SL.parse_slot("12:30 - 13:30", DAY, TZ), cons, TZ)[0]  # ends after latest


def test_nothing_fits_explains_what_was_seen():
    got, rejected = SL.choose(slots("Viernes 13 · 10:30–11:00", "Viernes 13 · 11:00–11:30 agotado"), CONS, TZ)
    assert got is None
    text = SL.describe(rejected, TZ)
    assert text == "Slots seen: Fri 13 10:30–11:00 (clashes with 10:05–11:55), Fri 13 11:00–11:30 (sold out)"
    assert S.reason_text("no_slot_fits") == "no slot fits your route"


def test_no_constraints_takes_earliest_available_and_prefer_latest():
    got, _ = SL.choose(slots("12:00 - 12:30", "10:00 - 10:30 agotado", "11:00 - 11:30"), None, TZ)
    assert got.start == at("11:00")
    got, _ = SL.choose(slots("10:30 - 11:00", "12:00 - 12:30"), {**CONS, "avoid": [], "prefer": "latest"}, TZ)
    assert got.start == at("12:00")


def test_date_patterns_match_picker_labels():
    import re
    pats = [re.compile(p, re.I) for p in SL.date_patterns(DAY)]
    for label in ("viernes, 13 de noviembre de 2026", "Friday, November 13, 2026", "13 nov", "2026-11-13", "13/11"):
        assert any(p.search(label) for p in pats), label
    assert not any(p.search("14 de noviembre") for p in pats)


def test_slot_time_js_pattern_is_valid_python_regex_too():
    import re
    r = re.compile(S.SLOT_TIME_JS, re.I)
    assert r.search("10:00 - 10:30") and r.search("Viernes 13 · 10am") and not r.search("Viernes 13")


@pytest.mark.parametrize("label", ["Ver entradas", "Mis entradas", "View tickets", "My tickets", "Ver pedido"])
def test_my_tickets_labels(label):
    assert S.MY_TICKETS_RE.search(label)


@pytest.mark.parametrize("label", ["Descargar entradas", "Download tickets", "Imprimir entradas", "Descargar PDF"])
def test_download_labels(label):
    assert S.DOWNLOAD_TICKET_RE.search(label)


@pytest.mark.parametrize("label", ["Comprobar disponibilidad", "Check availability", "Seleccionar fecha"])
def test_timed_entry_reserve_labels(label):
    assert S.RESERVE_RE.search(label)

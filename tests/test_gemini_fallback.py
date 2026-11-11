"""Gemini fallbacks: a busy or out-of-quota model hands the call to the next free model."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scout.parser import gemini as G


class Client:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))

    async def _gen(self, model, contents, config):
        self.calls.append(model)
        b = self.behaviour.get(model, "ok")
        if isinstance(b, list):
            b = b.pop(0) if b else "ok"
        if b != "ok":
            raise Exception(b)
        return SimpleNamespace(text="{}", parsed=None, usage_metadata=None)


BUSY = "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand.'}}"
QUOTA = "429 RESOURCE_EXHAUSTED. Please retry in 2.5s."


@pytest.fixture(autouse=True)
def chain(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "fb-lite,fb-flash")
    G._DEAD.clear()
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(G.asyncio, "sleep", fake_sleep)
    return slept


async def test_busy_main_model_falls_back_at_once(chain):
    c = Client({"main": BUSY})
    resp, model, _ = await G.generate(c, "main", ["x"], None, {"handle": "exedra.barcelona"})
    assert model == "fb-lite" and c.calls == ["main", "fb-lite"] and chain == []


async def test_quota_and_busy_skip_to_the_third(chain):
    c = Client({"main": QUOTA, "fb-lite": BUSY})
    _, model, _ = await G.generate(c, "main", ["x"], None, {})
    assert model == "fb-flash"


async def test_all_failing_waits_then_goes_round_again(chain):
    c = Client({"main": [BUSY, "ok"], "fb-lite": BUSY, "fb-flash": QUOTA})
    _, model, _ = await G.generate(c, "main", ["x"], None, {}, rounds=2)
    assert model == "main" and c.calls == ["main", "fb-lite", "fb-flash", "main"]
    assert chain == [3.5]  # what the 429 asked (+1 s)


async def test_unknown_fallback_model_is_dropped(chain):
    c = Client({"main": BUSY, "fb-lite": "404 NOT_FOUND models/fb-lite is not found"})
    await G.generate(c, "main", ["x"], None, {})
    assert "fb-lite" in G._DEAD and G.model_chain("main") == ["main", "fb-flash"]


async def test_bad_request_is_not_retried_elsewhere(chain):
    c = Client({"main": "400 INVALID_ARGUMENT: bad schema"})
    with pytest.raises(Exception, match="INVALID_ARGUMENT"):
        await G.generate(c, "main", ["x"], None, {})
    assert c.calls == ["main"]


def test_fallbacks_can_be_disabled(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    assert G.model_chain("gemini-3.5-flash-lite") == ["gemini-3.5-flash-lite"]

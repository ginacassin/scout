"""Gemini client: caption + images in, ParseResult out."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

from google import genai
from google.genai import types

from ..obs import STATS, kv
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .schema import GeminiParseResult, ParseResult

log = logging.getLogger(__name__)


class RateLimiter:
    """At most `per_minute` calls in any 60 s window, shared by every Gemini caller in the process.

    The free tier allows 15 requests a minute per model; a story run can bring 60+ items at once.
    """

    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
                if len(self._calls) < self.per_minute:
                    self._calls.append(now)
                    return
                await asyncio.sleep(60 - (now - self._calls[0]) + 0.1)


LIMITER = RateLimiter(int(os.environ.get("GEMINI_RPM", "12")))  # the main model
_LIMITERS: dict[str, RateLimiter] = {}

# Free-tier Flash models to try when the main one is overloaded (503) or out of quota (429). Each model
# has its own free quota. Override with GEMINI_FALLBACK_MODELS (comma-separated, empty to disable).
DEFAULT_FALLBACKS = "gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.8-flash"
_DEAD: set[str] = set()  # models Google says don't exist for this key (404): skipped from then on


def limiter_for(model: str, primary: str | None = None) -> RateLimiter:
    if primary is None or model == primary:
        return LIMITER
    if model not in _LIMITERS:
        _LIMITERS[model] = RateLimiter(int(os.environ.get("GEMINI_RPM", "12")))
    return _LIMITERS[model]


def model_chain(primary: str) -> list[str]:
    raw = os.environ.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS)
    chain = [primary] + [m.strip() for m in raw.split(",") if m.strip()]
    return [m for m in dict.fromkeys(chain) if m not in _DEAD]


def error_kind(exc: Exception) -> str:
    text = str(exc)
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return "quota"
    if any(k in text for k in ("503", "UNAVAILABLE", "overloaded", "500", "INTERNAL", "502", "504",
                               "DEADLINE_EXCEEDED")):
        return "busy"
    if "404" in text or "NOT_FOUND" in text or "is not found" in text:
        return "missing"
    return "other"


async def generate(client, primary: str, contents, config, label: dict, rounds: int = 2):  # noqa: ANN001
    """generate_content with fallbacks: on a busy/out-of-quota model, try the next free model at once;
    when all fail, wait (what a 429 asks, or a short backoff) and go round again. Returns (resp, model)."""
    last_exc: Exception | None = None
    for rnd in range(rounds):
        for model in model_chain(primary):
            await limiter_for(model, primary).wait()
            t0 = time.perf_counter()
            try:
                STATS.incr("gemini_calls")
                resp = await client.aio.models.generate_content(model=model, contents=contents, config=config)
                if model != primary:
                    STATS.incr("gemini_fallbacks")
                    log.info("gemini fallback used %s", kv(model=model, instead_of=primary, **label))
                return resp, model, int((time.perf_counter() - t0) * 1000)
            except Exception as exc:  # noqa: BLE001 - next model, then next round, then raised
                last_exc = exc
                kind = error_kind(exc)
                log.warning("Gemini call failed %s: %s", kv(model=model, round=rnd + 1, kind=kind,
                                                            latency_ms=int((time.perf_counter() - t0) * 1000),
                                                            **label), str(exc)[:300])
                if kind == "missing" and model != primary:
                    _DEAD.add(model)
                if kind == "other" and model == primary:
                    raise  # a bad request fails the same way on every model
        if rnd < rounds - 1 and last_exc is not None:
            await asyncio.sleep(retry_delay(last_exc, rnd))
    assert last_exc is not None
    raise last_exc


def retry_delay(exc: Exception, attempt: int) -> float:
    """Seconds to wait before retrying: what a 429 asks for (capped at 65 s), else a short backoff."""
    text = str(exc)
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        m = re.search(r"retry in ([0-9.]+)s", text) or re.search(r"retryDelay'?: '([0-9.]+)s", text)
        return min(float(m.group(1)) + 1, 65.0) if m else 60.0
    return 2 ** attempt * 3


class GeminiParser:
    def __init__(self, api_key: str, model: str):
        self.client = genai.Client(api_key=api_key)
        self.model = model

    async def parse(
        self,
        *,
        handle: str,
        item_kind: str,
        posted_at_local: str,
        today_local: str,
        caption: str,
        images: list[Path],
        location: str | None = None,
        coauthors: list[str] | None = None,
        bio: str | None = None,
        bio_links: list[str] | None = None,
        link_urls: list[str] | None = None,
        reshared_from: str | None = None,
        shared_post_url: str | None = None,
        mentions: list[str] | None = None,
    ) -> ParseResult:
        text = build_user_prompt(
            handle=handle,
            item_kind=item_kind,
            posted_at_local=posted_at_local,
            today_local=today_local,
            caption=caption,
            location=location,
            coauthors=coauthors or [],
            bio=bio,
            bio_links=bio_links or [],
            link_urls=link_urls or [],
            reshared_from=reshared_from,
            shared_post_url=shared_post_url,
            mentions=mentions or [],
        )
        contents: list[types.Part | str] = [text]
        for img in images[:4]:
            contents.append(types.Part.from_bytes(data=img.read_bytes(), mime_type="image/jpeg"))

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=GeminiParseResult,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            temperature=0.1,
        )
        resp, model, latency = await generate(self.client, self.model, contents, config,
                                              {"handle": handle}, rounds=2)
        self._log_usage(resp, handle, item_kind, len(contents) - 1, latency, model)
        if isinstance(resp.parsed, ParseResult):
            return resp.parsed
        return GeminiParseResult.model_validate_json(resp.text or "{}")

    def _log_usage(self, resp, handle: str, item_kind: str, images: int, latency_ms: int,  # noqa: ANN001
                   model: str | None = None) -> None:
        usage = getattr(resp, "usage_metadata", None)
        prompt = getattr(usage, "prompt_token_count", None)
        cand = getattr(usage, "candidates_token_count", None)
        total = getattr(usage, "total_token_count", None)
        if isinstance(total, int):
            STATS.incr("gemini_tokens", total)
        log.info("gemini call %s", kv(model=model or self.model, handle=handle, item=item_kind, images=images,
                                      latency_ms=latency_ms, prompt_tokens=prompt, candidates_tokens=cand,
                                      total_tokens=total))

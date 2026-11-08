"""Gemini client: caption + images in, ParseResult out."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from google import genai
from google.genai import types

from ..obs import STATS, kv
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .schema import ParseResult

log = logging.getLogger(__name__)


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
        )
        contents: list[types.Part | str] = [text]
        for img in images[:4]:
            contents.append(types.Part.from_bytes(data=img.read_bytes(), mime_type="image/jpeg"))

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ParseResult,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            temperature=0.1,
        )
        last_exc: Exception | None = None
        for attempt in range(3):
            t0 = time.perf_counter()
            try:
                STATS.incr("gemini_calls")
                resp = await self.client.aio.models.generate_content(model=self.model, contents=contents, config=config)
                self._log_usage(resp, handle, item_kind, len(contents) - 1, int((time.perf_counter() - t0) * 1000))
                if isinstance(resp.parsed, ParseResult):
                    return resp.parsed
                return ParseResult.model_validate_json(resp.text or "{}")
            except Exception as exc:  # noqa: BLE001 - retried, then raised
                last_exc = exc
                log.warning("Gemini parse failed %s: %s", kv(model=self.model, handle=handle, attempt=attempt + 1,
                                                             latency_ms=int((time.perf_counter() - t0) * 1000)), exc)
                await asyncio.sleep(2 ** attempt * 3)
        assert last_exc is not None
        raise last_exc

    def _log_usage(self, resp, handle: str, item_kind: str, images: int, latency_ms: int) -> None:  # noqa: ANN001
        usage = getattr(resp, "usage_metadata", None)
        prompt = getattr(usage, "prompt_token_count", None)
        cand = getattr(usage, "candidates_token_count", None)
        total = getattr(usage, "total_token_count", None)
        if isinstance(total, int):
            STATS.incr("gemini_tokens", total)
        log.info("gemini call %s", kv(model=self.model, handle=handle, item=item_kind, images=images,
                                      latency_ms=latency_ms, prompt_tokens=prompt, candidates_tokens=cand,
                                      total_tokens=total))

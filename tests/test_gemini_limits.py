import asyncio

import pytest

from scout.parser.gemini import RateLimiter, retry_delay


def test_retry_delay_honours_429_hint():
    exc = Exception("429 RESOURCE_EXHAUSTED. ... Please retry in 48.717352136s. ...")
    assert 49 <= retry_delay(exc, 0) <= 50
    assert retry_delay(Exception("429 RESOURCE_EXHAUSTED no hint"), 0) == 60
    assert retry_delay(Exception("429 ... retry in 300s"), 0) == 65
    assert retry_delay(Exception("500 boom"), 1) == 6


@pytest.mark.asyncio
async def test_rate_limiter_blocks_after_quota(monkeypatch):
    lim = RateLimiter(2)
    await lim.wait()
    await lim.wait()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lim.wait(), timeout=0.2)

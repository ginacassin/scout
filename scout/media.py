"""Download post/story images right away: Instagram CDN links expire within hours."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


async def download_images(urls: list[str], dest_dir: Path, prefix: str, limit: int = 4) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for i, url in enumerate(urls[:limit]):
            path = dest_dir / f"{_SAFE.sub('_', prefix)}_{i}.jpg"
            if path.exists():
                saved.append(path)
                continue
            try:
                r = await client.get(url)
                r.raise_for_status()
                path.write_bytes(r.content)
                saved.append(path)
            except httpx.HTTPError as exc:
                log.warning("Image download failed for %s: %s", prefix, exc)
    return saved


def prune_media(media_dir: Path, max_age_days: int = 30) -> int:
    if not media_dir.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in media_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    return removed

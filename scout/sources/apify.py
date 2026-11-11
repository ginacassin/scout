"""Apify adapter.

Profiles (bio, bio links, latest 12 posts) come from `apify/instagram-scraper` in
`details` mode: one billed result per account. Stories come from
`data-slayer/instagram-stories-scraper` (no login).

A test run on 7 Nov 2026 reported success but silently skipped one of six
accounts, so `profiles()` re-runs once for any handle that did not come back.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from apify_client import ApifyClientAsync

from ..obs import STATS, kv
from .base import InstagramSource, PostItem, ProfileSnapshot, StoryItem

log = logging.getLogger(__name__)

PROFILE_ACTOR = "apify/instagram-scraper"
STORIES_ACTOR = "data-slayer/instagram-stories-scraper"


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_post(raw: dict[str, Any]) -> PostItem | None:
    shortcode = raw.get("shortCode")
    if not shortcode:
        return None
    images: list[str] = []
    for child in raw.get("childPosts") or []:
        if child.get("displayUrl"):
            images.append(child["displayUrl"])
    if not images:
        images = [u for u in (raw.get("images") or []) if isinstance(u, str)]
    if not images and raw.get("displayUrl"):
        images = [raw["displayUrl"]]
    kind = (raw.get("type") or "").lower()
    media_type = "carousel" if kind == "sidecar" else ("video" if kind == "video" else "image")
    return PostItem(
        shortcode=shortcode,
        url=raw.get("url") or f"https://www.instagram.com/p/{shortcode}/",
        owner=raw.get("ownerUsername") or "",
        posted_at=_parse_ts(raw.get("timestamp")),
        caption=raw.get("caption") or "",
        image_urls=images[:4],
        is_pinned=bool(raw.get("isPinned")),
        coauthors=[c.get("username") for c in (raw.get("coauthorProducers") or []) if c.get("username")],
        location=raw.get("locationName"),
        media_type=media_type,
    )


def parse_profile(raw: dict[str, Any]) -> ProfileSnapshot | None:
    handle = (raw.get("username") or "").lower()
    if not handle:
        return None
    links = [link.get("url") for link in (raw.get("externalUrls") or []) if link.get("url")]
    if not links and raw.get("externalUrl"):
        links = [raw["externalUrl"]]
    posts = [p for p in (parse_post(x) for x in raw.get("latestPosts") or []) if p]
    return ProfileSnapshot(
        handle=handle,
        full_name=raw.get("fullName"),
        biography=raw.get("biography") or "",
        bio_links=links,
        posts=posts,
    )


def parse_story(raw: dict[str, Any]) -> StoryItem | None:
    sid = raw.get("id") or raw.get("code")
    owner = (raw.get("username") or raw.get("creator_username") or (raw.get("owner") or {}).get("username") or "").lower()
    if not sid or not owner:
        return None
    image = raw.get("thumbnail_url") or raw.get("media_url")
    versions = ((raw.get("image_versions") or {}).get("items") or [])
    if versions and versions[0].get("url"):
        image = versions[0]["url"]
    links = []
    for sticker in raw.get("story_link_stickers") or []:
        url = (sticker.get("story_link") or {}).get("url")
        if url:
            links.append(url)
    if raw.get("story_link_url"):
        links.append(raw["story_link_url"])
    reshared_from, shared_post_url, mentions = repost_signals(raw, owner)
    return StoryItem(
        story_id=str(sid).split("_")[0],
        owner=owner,
        posted_at=_parse_ts(raw.get("taken_at")),
        image_url=image,
        link_urls=list(dict.fromkeys(links)),
        is_video=bool(raw.get("is_video")),
        reshared_from=reshared_from,
        shared_post_url=shared_post_url,
        mentions=mentions,
    )


def repost_signals(raw: dict[str, Any], owner: str) -> tuple[str | None, str | None, list[str]]:
    """Who a story really comes from. Instagram marks a reshared story with a 'mention_reshare'
    mention of its author, and a reshared post with a story_feed_media sticker whose
    media_compound_str ends in the post owner's user id (equal to ours when resharing our own post)."""
    own_id = str((raw.get("owner") or {}).get("id") or (raw.get("user") or {}).get("id") or "")
    reshared_from, mentions = None, []
    for m in raw.get("reel_mentions") or []:
        user = (m.get("user") or {}).get("username", "").lower()
        if not user or user == owner:
            continue
        if m.get("display_type") == "mention_reshare":
            reshared_from = reshared_from or user
        elif user not in mentions:
            mentions.append(user)
    shared = None
    for fm in raw.get("story_feed_media") or []:
        code = fm.get("media_code")
        post_owner = str(fm.get("media_compound_str") or "").rpartition("_")[2]
        if code and (not own_id or post_owner != own_id):
            shared = f"https://www.instagram.com/p/{code}/"
            break
    return reshared_from, shared, mentions[:8]


class ApifySource(InstagramSource):
    def __init__(self, token: str):
        self.client = ApifyClientAsync(token)

    async def _run(self, actor: str, run_input: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        STATS.incr("apify_runs")
        try:
            run = await self.client.actor(actor).call(
                run_input=run_input,
                max_items=max_items,
                run_timeout=timedelta(minutes=10),
                logger=None,
            )
            if run is None or not run.default_dataset_id:
                raise RuntimeError(f"Apify run of {actor} returned no dataset")
            page = await self.client.dataset(run.default_dataset_id).list_items(clean=True)
        except Exception as exc:
            log.warning("apify run failed %s: %s", kv(actor=actor, duration_ms=int((time.perf_counter() - t0) * 1000)), exc)
            raise
        log.info("apify run %s", kv(actor=actor, run_id=getattr(run, "id", None), status=getattr(run, "status", None),
                                    items=len(page.items), duration_ms=int((time.perf_counter() - t0) * 1000)))
        return page.items

    async def profiles(self, handles: list[str]) -> dict[str, ProfileSnapshot]:
        result: dict[str, ProfileSnapshot] = {}
        todo = [h.lower() for h in handles]
        for attempt in (1, 2):
            if not todo:
                break
            items = await self._run(
                PROFILE_ACTOR,
                {
                    "resultsType": "details",
                    "directUrls": [f"https://www.instagram.com/{h}/" for h in todo],
                    "resultsLimit": 12,
                    "addParentData": False,
                },
                max_items=len(todo),
            )
            for raw in items:
                snap = parse_profile(raw)
                if snap:
                    result[snap.handle] = snap
            todo = [h for h in todo if h not in result]
            if todo and attempt == 1:
                log.warning("Apify skipped %s; retrying once", ", ".join(todo))
        return result

    async def stories(self, handles: list[str]) -> list[StoryItem]:
        if not handles:
            return []
        items = await self._run(STORIES_ACTOR, {"usernames": [h.lower() for h in handles]}, max_items=200)
        return [s for s in (parse_story(x) for x in items) if s]

    async def spend_usd(self) -> tuple[float, float] | None:
        try:
            limits = await self.client.user("me").limits()
            used = float(limits.current.monthly_usage_usd or 0)
            cap = float(limits.limits.max_monthly_usage_usd or 0)
            return used, cap
        except Exception:  # noqa: BLE001 - informational only
            log.exception("Could not read Apify usage")
            return None

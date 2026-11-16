"""A run for a single account must not ask Apify for a cost cap under its minimum."""

from __future__ import annotations

from types import SimpleNamespace

from scout.sources.apify import MIN_ITEMS, ApifySource


class Actor:
    def __init__(self, seen):
        self.seen = seen

    async def call(self, **kw):
        self.seen.update(kw)
        return SimpleNamespace(id="r", status="SUCCEEDED", default_dataset_id="d")


class Dataset:
    async def list_items(self, clean=True):
        return SimpleNamespace(items=[])


async def test_single_profile_run_asks_for_at_least_min_items():
    src = ApifySource.__new__(ApifySource)
    seen: dict = {}
    src.client = SimpleNamespace(actor=lambda a: Actor(seen), dataset=lambda d: Dataset())
    await src.profiles(["segundobis"])
    assert seen["max_items"] == MIN_ITEMS

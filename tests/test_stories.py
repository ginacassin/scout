"""Story reads: one account right after /add, all accounts twice a day."""

from __future__ import annotations

import pytest

from scout.pipeline import STORIES_AT_KEY
from tests.test_pipeline_real_cases import world  # noqa: F401 - fixture


class FakeStories:
    def __init__(self):
        self.calls: list[list[str]] = []

    async def stories(self, handles):
        self.calls.append(sorted(handles))
        return []


@pytest.mark.asyncio
async def test_poll_stories_only_for_given_handles(world):  # noqa: F811
    scout, db, _, _ = world
    for h in ("pujar_con_arte", "almamarket_home", "my_market_mad"):
        db.add_account(h)
    scout.source = FakeStories()
    await scout.poll_stories(["Pujar_Con_Arte"])
    assert scout.source.calls == [["pujar_con_arte"]]
    assert db.get_setting(STORIES_AT_KEY) is None  # a single-account read doesn't count as the full run


@pytest.mark.asyncio
async def test_full_story_poll_reads_everyone_and_records_time(world):  # noqa: F811
    scout, db, _, _ = world
    for h in ("pujar_con_arte", "almamarket_home"):
        db.add_account(h)
    scout.source = FakeStories()
    await scout.poll_stories()
    assert scout.source.calls == [["almamarket_home", "pujar_con_arte"]]
    assert db.get_setting(STORIES_AT_KEY)


@pytest.mark.asyncio
async def test_unknown_handle_costs_nothing(world):  # noqa: F811
    scout, db, _, _ = world
    db.add_account("almamarket_home")
    scout.source = FakeStories()
    assert await scout.poll_stories(["nobody"]) == 0
    assert scout.source.calls == []

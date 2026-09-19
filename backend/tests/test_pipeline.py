"""Pipeline tests: dedup + store + aggregate, including the real fixtures.

The fixture-driven tests are the Phase 2 equivalent of a live run: they push
genuine TikHub responses through the real normalisers and the real repository,
so the whole path is verified **without a billed API call**.
"""

from __future__ import annotations

import pytest

from app.db.database import session_scope
from app.db.repository import database_stats, list_topic_groups
from app.services.pipeline.hot_pipeline import store_items
from app.services.tikhub.base import find_item_list
from app.services.tikhub.client import TikHubClient
from app.services.tikhub.douyin import DouyinAdapter
from app.services.tikhub.weibo import WeiboAdapter
from app.services.tikhub.xiaohongshu import XiaohongshuAdapter

ADAPTERS = {
    "weibo": WeiboAdapter,
    "douyin": DouyinAdapter,
    "xiaohongshu": XiaohongshuAdapter,
}


@pytest.mark.asyncio
async def test_store_items_dedups_and_aggregates(sqlite_db, settings, item_factory):
    items = [
        item_factory("weibo", "w1", "某某重大事件", hot_value=10),
        item_factory("douyin", "d1", "某某重大事件", hot_value=20),  # cross-platform twin
        item_factory("weibo", "w1", "某某重大事件"),  # layer 1 duplicate
        item_factory("weibo", "w9", "某某重大事件"),  # layer 3 duplicate (same platform)
    ]
    async with session_scope(settings) as session:
        result = await store_items(items, settings=settings, session=session)

    assert result.dedup["input"] == 4
    assert result.dedup["removed"] == 2
    assert result.dedup["kept"] == 2
    assert result.stored["inserted"] == 2
    assert result.topics["topics"] == 1
    assert result.topics["cross_platform_topics"] == 1

    async with session_scope(settings) as session:
        groups, _ = await list_topic_groups(session)
    assert groups[0][1] == 2


@pytest.mark.asyncio
async def test_second_run_is_idempotent(sqlite_db, settings, item_factory):
    items = [
        item_factory("weibo", "w1", "某某重大事件", hot_value=10),
        item_factory("douyin", "d1", "某某重大事件", hot_value=20),
    ]
    for _ in range(2):
        async with session_scope(settings) as session:
            result = await store_items(items, settings=settings, session=session)

    assert result.stored["inserted"] == 0, "the same items must not be inserted twice"
    assert result.stored["updated"] == 2
    async with session_scope(settings) as session:
        stats = await database_stats(session)
    assert stats["hot_contents"] == 2
    assert stats["topic_groups"] == 1, "groups must not multiply across runs"


@pytest.mark.asyncio
async def test_real_fixtures_flow_through_the_pipeline(sqlite_db, settings, raw_fixture):
    """Genuine captured responses through normalisers, dedup, and storage."""
    client = TikHubClient(settings)
    collected = []
    try:
        for platform, adapter_class in ADAPTERS.items():
            payload = raw_fixture(platform)
            if payload is None:
                pytest.skip(
                    f"no captured fixture for {platform}; run "
                    f"`python scripts/discover_raw.py --yes --platform {platform}`"
                )
            collected.extend(adapter_class(client).normalize(find_item_list(payload)))
    finally:
        await client.aclose()

    assert len(collected) >= 100, "the fixtures hold ~142 items in total"

    async with session_scope(settings) as session:
        result = await store_items(collected, settings=settings, session=session)

    assert result.dedup["input"] == len(collected)
    assert result.dedup["kept"] + result.dedup["removed"] == len(collected)
    assert result.stored["inserted"] == result.dedup["kept"]
    assert result.window_items >= result.dedup["kept"]

    async with session_scope(settings) as session:
        stats = await database_stats(session)
    assert stats["hot_contents"] == result.dedup["kept"]
    assert set(stats["by_platform"]) <= {"weibo", "douyin", "xiaohongshu"}

    # And a replay must change nothing structural.
    async with session_scope(settings) as session:
        replay = await store_items(collected, settings=settings, session=session)
    assert replay.stored["inserted"] == 0
    async with session_scope(settings) as session:
        stats_after = await database_stats(session)
    assert stats_after["hot_contents"] == stats["hot_contents"]
    assert stats_after["topic_groups"] == stats["topic_groups"]

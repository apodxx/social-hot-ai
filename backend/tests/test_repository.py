"""Repository tests against in-memory SQLite (the schema is portable by design)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.database import session_scope
from app.db.repository import (
    apply_topic_clusters,
    dashboard_stats,
    database_stats,
    delete_empty_topic_groups,
    list_admin_hot_contents,
    list_topic_groups,
    recent_rows,
    topic_members,
    upsert_items,
)
from app.models.hot_content import HotContentRecord
from app.models.topic_group import TopicGroupRecord
from app.services.pipeline.topic_grouping import cluster_topics


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates_in_place(sqlite_db, item_factory):
    first = item_factory(
        "weibo",
        "w1",
        "#某某事件#",
        url="https://s.weibo.com/weibo?q=a&Refer=top",
        hot_value=100,
        rank=1,
    )
    async with session_scope() as session:
        stats = await upsert_items(session, [first])
    assert (stats.inserted, stats.updated) == (1, 0)

    async with session_scope() as session:
        row = (await session.execute(select(HotContentRecord))).scalars().one()
        assert row.title_normalized == "某某事件", "derived key is stored"
        assert row.url_normalized is not None
        assert row.url_normalized.startswith("https://s.weibo.com/weibo?q=a"), (
            "the resource-identifying query parameter is kept"
        )
        assert "refer" not in row.url_normalized.lower(), "tracking parameter dropped"
        assert row.raw_data == {"seed": "w1"}

    refreshed = item_factory("weibo", "w1", "#某某事件#", hot_value=999, rank=7)
    async with session_scope() as session:
        stats = await upsert_items(session, [refreshed])
    assert (stats.inserted, stats.updated) == (0, 1)

    async with session_scope() as session:
        rows = (await session.execute(select(HotContentRecord))).scalars().all()
    assert len(rows) == 1, "layer 1: the unique key prevents a second row"
    assert rows[0].hot_value == 999 and rows[0].rank == 7


@pytest.mark.asyncio
async def test_list_admin_hot_contents_filters_and_counts(sqlite_db, item_factory):
    items = [
        item_factory("weibo", "w1", "甲"),
        item_factory("weibo", "w2", "乙"),
        item_factory("douyin", "d1", "丙"),
    ]
    async with session_scope() as session:
        await upsert_items(session, items)

    async with session_scope() as session:
        rows, total = await list_admin_hot_contents(session, platform="weibo")
        assert total == 2 and len(rows) == 2
        assert {item.platform for item, _, _ in rows} == {"weibo"}
        assert all(analysis is None and rewrite is None for _, analysis, rewrite in rows), (
            "an item with no analysis still appears, with empty AI columns"
        )

    async with session_scope() as session:
        rows, total = await list_admin_hot_contents(session, limit=1, offset=1)
        assert total == 3 and len(rows) == 1

    future = datetime.now(timezone.utc) + timedelta(days=1)
    async with session_scope() as session:
        rows, total = await list_admin_hot_contents(session, since=future)
        assert total == 0

    async with session_scope() as session:
        rows, total = await list_admin_hot_contents(session, keyword="丙")
        assert total == 1 and rows[0][0].title == "丙", "keyword filter matches a substring"


@pytest.mark.asyncio
async def test_list_admin_hot_contents_joins_analysis_and_rewrite(sqlite_db, item_factory):
    from app.models.ai_analysis import AiAnalysisRecord
    from app.models.ai_rewrite import AiRewriteRecord

    async with session_scope() as session:
        await upsert_items(
            session,
            [
                item_factory("weibo", "w1", "被推荐的"),
                item_factory("weibo", "w2", "被否掉的"),
                item_factory("douyin", "d1", "还没分析"),
            ],
        )
        stored = {
            row.title: row
            for row in (await session.execute(select(HotContentRecord))).scalars().all()
        }
        session.add(
            AiAnalysisRecord(
                hot_content_id=stored["被推荐的"].id,
                topic="t",
                summary="s",
                recommended=True,
                selected=True,
                confidence=0.9,
            )
        )
        session.add(
            AiAnalysisRecord(
                hot_content_id=stored["被否掉的"].id,
                topic="t",
                summary="s",
                recommended=False,
                selected=False,
                confidence=0.1,
            )
        )
        session.add(
            AiRewriteRecord(
                hot_content_id=stored["被推荐的"].id,
                xiaohongshu_title="标题",
                xiaohongshu_content="正文",
                status="NEEDS_REVIEW",
            )
        )

    async with session_scope() as session:
        rows, total = await list_admin_hot_contents(session, recommended=True)
        assert total == 1
        item, analysis, rewrite = rows[0]
        assert item.title == "被推荐的"
        assert analysis is not None and analysis.confidence == 0.9
        assert rewrite is not None and rewrite.xiaohongshu_title == "标题", (
            "the list carries the rewrite so the UI needs no per-row request"
        )

    async with session_scope() as session:
        rows, total = await list_admin_hot_contents(session, recommended=False)
        assert total == 1 and rows[0][0].title == "被否掉的", (
            "only explicitly not-recommended analyses match; unanalysed rows are NULL, not False"
        )

    async with session_scope() as session:
        rows, total = await list_admin_hot_contents(session, selected=True)
        assert total == 1 and rows[0][0].title == "被推荐的"

    async with session_scope() as session:
        rows, total = await list_admin_hot_contents(session, selected=False)
        assert total == 1 and rows[0][0].title == "被否掉的"


@pytest.mark.asyncio
async def test_dashboard_stats_counts_the_pipeline(sqlite_db, item_factory):
    from app.models.ai_analysis import AiAnalysisRecord
    from app.models.ai_rewrite import AiRewriteRecord

    async with session_scope() as session:
        await upsert_items(
            session,
            [
                item_factory("weibo", "w1", "甲"),
                item_factory("weibo", "w2", "乙"),
                item_factory("douyin", "d1", "丙"),
            ],
        )
        stored = {
            row.title: row
            for row in (await session.execute(select(HotContentRecord))).scalars().all()
        }
        session.add(
            AiAnalysisRecord(
                hot_content_id=stored["甲"].id,
                recommended=True,
                selected=True,
                needs_verification=True,
            )
        )
        session.add(
            AiAnalysisRecord(
                hot_content_id=stored["乙"].id,
                recommended=False,
                selected=False,
                needs_verification=False,
            )
        )
        session.add(
            AiRewriteRecord(hot_content_id=stored["甲"].id, status="READY_TO_PUBLISH")
        )
        session.add(
            AiRewriteRecord(hot_content_id=stored["乙"].id, status="NEEDS_REVIEW")
        )

    async with session_scope() as session:
        stats = await dashboard_stats(session)

    assert stats["total_items"] == 3
    assert stats["by_platform"] == {"weibo": 2, "douyin": 1}
    assert stats["analyses"] == 2
    assert stats["recommended"] == 1
    assert stats["selected"] == 1
    assert stats["needs_verification"] == 1
    assert stats["rewrites"] == 2
    assert stats["ready_to_publish"] == 1
    assert stats["needs_review"] == 1, "review backlog is what the dashboard is for"

    # The per-platform split respects the window, the AI totals do not.
    future = datetime.now(timezone.utc) + timedelta(days=1)
    async with session_scope() as session:
        windowed = await dashboard_stats(session, since=future)
    assert windowed["total_items"] == 0 and windowed["by_platform"] == {}
    assert windowed["analyses"] == 2 and windowed["rewrites"] == 2


@pytest.mark.asyncio
async def test_topic_clusters_are_persisted_and_reused(sqlite_db, item_factory):
    items = [
        item_factory("weibo", "w1", "某某重大事件", hot_value=10),
        item_factory("douyin", "d1", "某某重大事件", hot_value=20),
    ]
    async with session_scope() as session:
        await upsert_items(session, items)

    async with session_scope() as session:
        rows = await recent_rows(session)
        clusters, ungrouped = cluster_topics(rows, threshold=0.9)
        assert ungrouped == 0
        stats = await apply_topic_clusters(session, clusters)
    assert stats.groups_created == 1
    assert stats.members_assigned == 2

    async with session_scope() as session:
        groups, total = await list_topic_groups(session)
        assert total == 1
        group, member_count = groups[0]
        assert member_count == 2
        assert group.platforms == ["douyin", "weibo"]
        assert group.summary is None, "Phase 2 must not invent a summary"
        members = await topic_members(session, group.id)
        assert {member.platform for member in members} == {"weibo", "douyin"}
        first_group_id = group.id

    # A second pass over the same data adopts the existing group.
    async with session_scope() as session:
        rows = await recent_rows(session)
        clusters, _ = cluster_topics(rows, threshold=0.9)
        stats = await apply_topic_clusters(session, clusters)
    assert stats.groups_created == 0, "topic groups must not multiply per run"
    assert stats.groups_updated == 1

    async with session_scope() as session:
        groups, total = await list_topic_groups(session)
        assert total == 1 and groups[0][0].id == first_group_id


@pytest.mark.asyncio
async def test_cross_platform_only_filter(sqlite_db, item_factory):
    items = [
        item_factory("weibo", "w1", "跨平台事件"),
        item_factory("douyin", "d1", "跨平台事件"),
        item_factory("weibo", "w2", "单平台事件"),
        item_factory("weibo", "w3", "单平台事件"),
    ]
    async with session_scope() as session:
        await upsert_items(session, items)
    async with session_scope() as session:
        rows = await recent_rows(session)
        clusters, _ = cluster_topics(rows, threshold=0.9)
        await apply_topic_clusters(session, clusters)

    async with session_scope() as session:
        _, all_total = await list_topic_groups(session)
        cross, cross_total = await list_topic_groups(session, cross_platform_only=True)
    assert all_total == 2
    assert cross_total == 1 and cross[0][0].topic == "跨平台事件"


@pytest.mark.asyncio
async def test_empty_groups_are_deleted(sqlite_db):
    async with session_scope() as session:
        session.add(
            TopicGroupRecord(topic="孤儿话题", topic_normalized="孤儿话题", platforms=[])
        )
    async with session_scope() as session:
        assert await delete_empty_topic_groups(session) == 1
    async with session_scope() as session:
        assert (await list_topic_groups(session))[1] == 0


@pytest.mark.asyncio
async def test_database_stats(sqlite_db, item_factory):
    items = [
        item_factory("weibo", "w1", "甲事件讨论"),
        item_factory("douyin", "d1", "甲事件讨论"),
        item_factory("xiaohongshu", "x1", "孤立话题"),
    ]
    async with session_scope() as session:
        await upsert_items(session, items)
        rows = await recent_rows(session)
        clusters, _ = cluster_topics(rows, threshold=0.9)
        await apply_topic_clusters(session, clusters)

    async with session_scope() as session:
        stats = await database_stats(session)
    assert stats["hot_contents"] == 3
    assert stats["by_platform"] == {"weibo": 1, "douyin": 1, "xiaohongshu": 1}
    assert stats["topic_groups"] == 1
    assert stats["grouped_contents"] == 2
    assert stats["latest_created_at"] is not None

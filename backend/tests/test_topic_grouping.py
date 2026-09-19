"""Layer 4 tests: cross-platform topic aggregation."""

from __future__ import annotations

from app.services.pipeline.topic_grouping import (
    cluster_summary,
    cluster_topics,
    platform_name,
)


def test_same_topic_on_three_platforms_becomes_one_group(item_factory):
    items = [
        item_factory("weibo", "w1", "某某重大事件", hot_value=500),
        item_factory("douyin", "d1", "某某重大事件最新进展", hot_value=9000),
        item_factory("xiaohongshu", "x1", "#某某重大事件#", hot_value=100),
    ]
    clusters, ungrouped = cluster_topics(items, threshold=0.82)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.size == 3
    assert cluster.platforms == ["douyin", "weibo", "xiaohongshu"]
    assert ungrouped == 0
    # The representative is the highest hot_value member — the topic name is
    # chosen from real provider data, never synthesised.
    assert cluster.topic == "某某重大事件最新进展"
    assert cluster.topic_normalized == "某某重大事件最新进展"


def test_unrelated_topics_stay_separate(item_factory):
    items = [
        item_factory("weibo", "w1", "某某重大事件"),
        item_factory("douyin", "d1", "完全无关的另一话题"),
    ]
    clusters, ungrouped = cluster_topics(items, threshold=0.82)
    assert clusters == []
    assert ungrouped == 2, "singletons are not topics"


def test_min_group_size_is_respected(item_factory):
    items = [
        item_factory("weibo", "w1", "某某重大事件"),
        item_factory("douyin", "d1", "某某重大事件"),
    ]
    clusters, _ = cluster_topics(items, threshold=0.82, min_group_size=3)
    assert clusters == []


def test_same_platform_duplicates_can_still_cluster(item_factory):
    """Clustering is about topics, not platforms: two Weibo items can group."""
    items = [
        item_factory("weibo", "w1", "某某事件的完整经过"),
        item_factory("weibo", "w2", "某某事件"),
    ]
    clusters, _ = cluster_topics(items, threshold=0.9)
    assert len(clusters) == 1
    assert clusters[0].platforms == ["weibo"]


def test_clusters_are_sorted_by_size_then_topic(item_factory):
    items = [
        item_factory("weibo", "w1", "甲事件"),
        item_factory("douyin", "d1", "甲事件"),
        item_factory("xiaohongshu", "x1", "甲事件"),
        item_factory("weibo", "w2", "乙事件"),
        item_factory("douyin", "d2", "乙事件"),
    ]
    clusters, _ = cluster_topics(items, threshold=0.9)
    assert [cluster.size for cluster in clusters] == [3, 2]


def test_platform_name_reads_both_shapes(item_factory):
    from app.models.hot_content import HotContentRecord

    item = item_factory("weibo", "w1", "标题")
    assert platform_name(item) == "weibo"
    # A DB row carries the platform as a plain string.
    row = HotContentRecord(
        platform="douyin", platform_content_id="d1", title="标题", title_normalized="标题"
    )
    assert platform_name(row) == "douyin"


def test_cluster_summary_counts(item_factory):
    items = [
        item_factory("weibo", "w1", "甲事件"),
        item_factory("douyin", "d1", "甲事件"),
        item_factory("weibo", "w2", "孤立话题"),
    ]
    clusters, _ = cluster_topics(items, threshold=0.9)
    summary = cluster_summary(clusters)
    assert summary == {"topics": 1, "grouped_items": 2, "cross_platform_topics": 1}

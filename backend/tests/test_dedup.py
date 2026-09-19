"""Deduplication tests: normalisation, similarity, and the three drop layers.

The load-bearing case is :func:`test_same_title_on_two_platforms_is_not_a_duplicate`:
layer 3 must be scoped per platform, otherwise cross-platform aggregation has
nothing left to aggregate.
"""

from __future__ import annotations

import pytest

from app.services.pipeline.dedup import (
    DedupIndex,
    DedupLayer,
    bigrams,
    deduplicate,
    normalize_title,
    normalize_url,
    title_similarity,
)


# ------------------------------------------------------------- normalisation
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("＃某某事件＃", "某某事件"),  # full-width hashes, NFKC
        ("#某某事件#", "某某事件"),  # hashtag markers unwrapped
        ("某某事件！！！", "某某事件"),  # punctuation dropped
        ("某某 事件", "某某事件"),  # spaces dropped
        ("ABC事件", "abc事件"),  # case folded
        ("某某事件😀🎉", "某某事件"),  # emoji dropped
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_normalized_titles_that_should_collide_do():
    variants = ["#某某事件#", "某某事件！", "某某事件 ", "某某事件😀"]
    assert len({normalize_title(v) for v in variants}) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/a/", "https://example.com/a"),
        ("example.com/a#frag", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&utm_medium=y", "https://example.com/a"),
        ("", None),
        (None, None),
        ("not a url", None),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_url_drops_tracking_but_keeps_the_resource():
    left = normalize_url("https://s.weibo.com/weibo?q=%23x%23&band_rank=1")
    right = normalize_url("https://s.weibo.com/weibo?q=%23x%23&Refer=top")
    assert left == right, "different tracking parameters, same resource"
    assert "band_rank" not in (left or "") and "refer" not in (left or "").lower()
    assert "q=" in (left or ""), "the resource-identifying parameter must survive"


def test_normalize_url_keeps_different_topics_apart():
    """Regression: a blanket query strip once collapsed 52 Weibo items into one.

    Weibo's topic lives in the query string, so dropping it made every hot-search
    link identical and layer 2 then deleted 50 of them.
    """
    first = normalize_url("https://s.weibo.com/weibo?q=%23%E7%94%B2%23&Refer=top")
    second = normalize_url("https://s.weibo.com/weibo?q=%23%E4%B9%99%23&Refer=top")
    assert first is not None and second is not None
    assert first != second


def test_bigrams_of_short_and_long_text():
    assert bigrams("") == set()
    assert bigrams("一") == {"一"}
    assert bigrams("九一八") == {"九一", "一八"}


# --------------------------------------------------------------- similarity
def test_title_similarity_exact_containment_and_unrelated():
    assert title_similarity("某某事件", "某某事件") == 1.0
    assert title_similarity("某某事件", "某某事件最新进展") == 1.0, "containment matches"
    assert title_similarity("", "某某事件") == 0.0
    assert title_similarity("某某事件", "完全无关的话题") < 0.5


# -------------------------------------------------------------------- layers
def test_layer1_same_platform_and_id(item_factory):
    index = DedupIndex()
    first = item_factory("weibo", "w1", "第一条")
    index.add(first)
    match = index.find(item_factory("weibo", "w1", "标题变了也算同一条"))
    assert match is not None and match.layer is DedupLayer.PLATFORM_CONTENT_ID


def test_layer2_same_url_different_id(item_factory):
    index = DedupIndex()
    index.add(item_factory("weibo", "w1", "甲", url="https://s.weibo.com/weibo?q=a&Refer=top"))
    match = index.find(
        item_factory("weibo", "w2", "乙", url="https://s.weibo.com/weibo?q=a&band_rank=9")
    )
    assert match is not None and match.layer is DedupLayer.URL


def test_layer3_same_platform_similar_title(item_factory):
    index = DedupIndex(threshold=0.8)
    index.add(item_factory("douyin", "d1", "某某事件最新进展"))
    match = index.find(item_factory("douyin", "d2", "某某事件"))
    assert match is not None and match.layer is DedupLayer.TITLE_SIMILARITY
    assert match.similarity == 1.0


def test_same_title_on_two_platforms_is_not_a_duplicate(item_factory):
    """Layer 3 is per platform: a cross-platform twin must survive.

    Both items are *different content about one topic*, which is exactly what
    layer 4 aggregates. Dropping one here would delete a platform's data point
    and leave the topic with a single member.
    """
    index = DedupIndex(threshold=0.8)
    index.add(item_factory("weibo", "w1", "某某重大事件"))
    assert index.find(item_factory("douyin", "d1", "某某重大事件")) is None
    assert index.find(item_factory("xiaohongshu", "x1", "某某重大事件")) is None


def test_unrelated_titles_on_one_platform_survive(item_factory):
    index = DedupIndex(threshold=0.8)
    index.add(item_factory("weibo", "w1", "某某重大事件"))
    assert index.find(item_factory("weibo", "w2", "完全不同的话题内容")) is None


def test_short_titles_are_not_similarity_matched(item_factory):
    """Below MIN_TITLE_LENGTH there is not enough signal to call it a duplicate."""
    index = DedupIndex(threshold=0.5)
    index.add(item_factory("weibo", "w1", "牛市"))
    assert index.find(item_factory("weibo", "w2", "牛市来了吗")) is None


# ------------------------------------------------------------------ report
def test_deduplicate_reports_each_layer(item_factory):
    items = [
        item_factory("weibo", "w1", "第一条"),
        item_factory("weibo", "w1", "第一条重复"),  # layer 1
        item_factory("weibo", "w2", "另一条", url="https://s.weibo.com/weibo?q=b"),
        item_factory(
            "weibo", "w3", "第三条", url="https://s.weibo.com/weibo?q=b&Refer=top"
        ),  # layer 2
        item_factory("douyin", "d1", "第一条"),  # kept: different platform
    ]
    report = deduplicate(items, threshold=0.8)
    assert report.counts["input"] == 5
    assert report.counts["kept"] == 3
    assert report.counts["removed"] == 2
    assert report.counts["removed_by_platform_content_id"] == 1
    assert report.counts["removed_by_url"] == 1
    assert [item.platform.value for item in report.kept].count("douyin") == 1


def test_deduplicate_against_a_preloaded_index(item_factory):
    """The window index carries history, so a run also collapses against it."""
    index = DedupIndex(threshold=0.8)
    index.add(item_factory("weibo", "old", "历史里的老话题"))
    report = deduplicate(
        [item_factory("weibo", "new", "历史里的老话题")], threshold=0.8, index=index
    )
    assert report.counts["removed"] == 1
    assert report.counts["removed_by_title_similarity"] == 1

"""Rule-filter tests: the free gate in front of every paid token."""

from __future__ import annotations

import pytest

from app.services.pipeline.rule_filter import (
    rejection_reason,
    rule_filter,
    select_candidates,
)


def test_clean_item_passes(item_factory):
    assert rejection_reason(item_factory("weibo", "w1", "某某重大事件的最新进展")) is None


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("", "empty_content"),
        ("！！！", "empty_content"),
        ("加微信领取福利", "advertising"),
        ("代购直邮低价出", "advertising"),
        ("接单中 wx:abc123", "marketing_contact"),
        ("联系电话13800138000", "marketing_contact"),
        ("!!!!!!!!好家伙", "low_quality_repeated_chars"),
        ("aaaaaaaaaa", "low_quality_repeated_chars"),
        ("短", "low_quality_short_title"),
    ],
)
def test_titles_are_rejected_with_a_reason(item_factory, title, reason):
    assert rejection_reason(item_factory("weibo", "w1", title)) == reason


@pytest.mark.parametrize(
    "title",
    [
        "手把手教你做超长长长长长蛋挞",
        "哈哈哈哈这也太好笑了",
        "超超超好用的语法顺口溜",
    ],
)
def test_cjk_emphasis_is_not_low_quality(item_factory, title):
    """A real capture lost "超长长长长长蛋挞" to the original CJK-inclusive rule.

    Repeated Chinese characters are emphasis, not filler, so the rule only
    covers non-CJK runs.
    """
    assert rejection_reason(item_factory("xiaohongshu", "x1", title)) is None


def test_zero_heat_at_a_non_top_rank_is_abnormal(item_factory):
    """Real Douyin boards put a pinned entry at rank 1 with heat 0; lower ranks must not."""
    assert rejection_reason(item_factory("douyin", "d1", "置顶内容", hot_value=0, rank=1)) is None
    assert (
        rejection_reason(item_factory("douyin", "d2", "异常内容", hot_value=0, rank=7))
        == "abnormal_hot_value"
    )


def test_missing_heat_is_not_abnormal(item_factory):
    """Weibo supplies no heat value at all; ``None`` means unknown, not zero."""
    assert rejection_reason(item_factory("weibo", "w1", "正常的热搜条目", hot_value=None)) is None


def test_negative_counters_are_abnormal(item_factory):
    item = item_factory("xiaohongshu", "x1", "计数异常的内容", likes=-5)
    assert rejection_reason(item) == "abnormal_likes"


def test_rule_filter_reports_per_reason_counts(item_factory):
    report = rule_filter(
        [
            item_factory("weibo", "w1", "正常的第一条"),
            item_factory("weibo", "w2", "加微信领取福利"),
            item_factory("weibo", "w3", "正常第二条内容"),
            item_factory("douyin", "d1", "代购低价出"),
        ]
    )
    assert len(report.kept) == 2
    assert report.reasons == {"advertising": 2}
    assert report.summary()["input"] == 4
    assert report.summary()["kept"] == 2


def test_candidate_selection_keeps_every_platform_present(item_factory):
    """A 300-item board must not crowd out the other two platforms."""
    items = [item_factory("douyin", f"d{i}", f"抖音热点{i}", hot_value=10_000 - i) for i in range(20)]
    items += [item_factory("weibo", "w1", "微博热点一", hot_value=1)]
    items += [item_factory("xiaohongshu", "x1", "小红书热点一", hot_value=1)]

    selected = select_candidates(items, 3)
    assert {item.platform.value for item in selected} == {"douyin", "weibo", "xiaohongshu"}


def test_candidate_selection_prefers_hotter_within_a_platform(item_factory):
    items = [
        item_factory("weibo", "w1", "冷门事件", hot_value=1),
        item_factory("weibo", "w2", "超热事件", hot_value=9999),
    ]
    assert select_candidates(items, 1)[0].platform_content_id == "w2"


def test_candidate_selection_respects_the_limit(item_factory):
    items = [item_factory("weibo", f"w{i}", f"条目{i}", hot_value=i) for i in range(10)]
    assert len(select_candidates(items, 4)) == 4
    assert select_candidates(items, 0) == []

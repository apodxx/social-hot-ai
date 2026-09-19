"""Analyzer tests: batching, strict parsing, selection, persistence, merging.

A stub client replaces DeepSeek, so the whole Phase 3 path runs without spending
a single token.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from app.db.database import session_scope
from app.db.repository import database_stats, list_analyses, recent_rows
from app.services.ai.account_profile import AccountProfile
from app.services.ai.analyzer import (
    AnalysisResult,
    _apportion,
    analyze_items,
    build_batch_prompt,
    build_semantic_prompt,
    describe_item,
    merge_semantic_topics,
    parse_batch,
    parse_semantic_groups,
    run_analysis,
    select_top,
)
from app.services.ai.deepseek import ChatResult, ChatUsage, DeepSeekJSONError


class StubDeepSeekClient:
    """A DeepSeek stand-in: canned answers, recorded prompts, real usage maths."""

    def __init__(self, responses: list[Any], *, model: str = "deepseek-flash") -> None:
        self._responses = list(responses)
        self.model = model
        self.prompts: list[dict[str, str]] = []
        self.total_usage = ChatUsage()
        self.call_count = 0

    async def complete_json(self, *, system: str, user: str, **_: Any):
        self.prompts.append({"system": system, "user": user})
        self.call_count += 1
        payload = self._responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        usage = ChatUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        self.total_usage.add(usage)
        return payload, ChatResult(
            content=json.dumps(payload, ensure_ascii=False), model=self.model, usage=usage
        )

    async def aclose(self) -> None:  # pragma: no cover - nothing to release
        return None


def _result(index: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "index": index,
        "topic": f"话题{index}",
        "summary": f"摘要{index}",
        "why_hot": "因为讨论多",
        "discussion_points": ["讨论点一", "讨论点二"],
        "account_fit": "适合",
        "content_angle": "从技术原理切入",
        "is_duplicate": False,
        "recommended": True,
        "confidence": 0.8,
        "needs_verification": False,
    }
    base.update(overrides)
    return base


PROFILE = AccountProfile(field="AI / 科技", style="通俗", preferred_topics=["AI"])


# ------------------------------------------------------------------- prompts
def test_describe_item_sends_only_cheap_fields(item_factory):
    item = item_factory("weibo", "w1", "标题内容", hot_value=123, rank=4)
    described = describe_item(0, item)
    assert set(described) == {"index", "platform", "title", "hot_value", "rank"}
    assert "raw_data" not in described, "raw provider JSON must not be paid for"


def test_describe_item_truncates_long_titles(item_factory):
    item = item_factory("weibo", "w1", "长" * 400)
    assert len(describe_item(0, item)["title"]) == 120


def test_batch_prompt_carries_profile_items_and_rules(item_factory):
    items = [item_factory("weibo", "w1", "第一条热点"), item_factory("douyin", "d1", "第二条热点")]
    prompt = build_batch_prompt(PROFILE, items, date(2026, 9, 18))
    assert "AI / 科技" in prompt
    assert "第一条热点" in prompt and "第二条热点" in prompt
    assert "2026-09-18" in prompt
    assert "results 必须恰好包含全部 2 条" in prompt
    assert "needs_verification" in prompt


def test_semantic_prompt_lists_every_item(item_factory):
    prompt = build_semantic_prompt([item_factory("weibo", "w1", "甲"), item_factory("douyin", "d1", "乙")])
    assert "甲" in prompt and "乙" in prompt
    assert '"groups"' in prompt


# -------------------------------------------------------------------- parsing
def test_parse_batch_accepts_a_well_formed_answer():
    results, problems = parse_batch({"results": [_result(0), _result(1)]}, range(2))
    assert problems == []
    assert [result.index for result in results] == [0, 1]
    assert results[0].discussion_points == ["讨论点一", "讨论点二"]


def test_parse_batch_tolerates_a_bare_list_and_keyed_mapping():
    from_list, problems = parse_batch([_result(0)], range(1))
    assert not problems and from_list[0].index == 0
    from_map, problems = parse_batch({"0": {k: v for k, v in _result(0).items() if k != "index"}}, range(1))
    assert not problems and from_map[0].index == 0


def test_parse_batch_reports_missing_extra_and_duplicate_indices():
    results, problems = parse_batch(
        {"results": [_result(0), _result(0), _result(9)]}, range(2)
    )
    assert [result.index for result in results] == [0]
    assert any("appeared twice" in problem for problem in problems)
    assert any("was not requested" in problem for problem in problems)
    assert any("no result for indices [1]" in problem for problem in problems)


def test_parse_batch_rejects_a_response_without_results():
    results, problems = parse_batch({"answer": "blah"}, range(1))
    assert results == []
    assert problems and "no 'results' array" in problems[0]


def test_analysis_result_normalises_confidence_and_points():
    high = AnalysisResult.model_validate(_result(0, confidence=87))
    assert high.confidence == pytest.approx(0.87), "a percentage is normalised"
    low = AnalysisResult.model_validate(_result(0, confidence=5))
    assert low.confidence == pytest.approx(0.05), "5 means 5%, not 5.0"
    clamped = AnalysisResult.model_validate(_result(0, confidence=200))
    assert clamped.confidence == 1.0, "a nonsense value is clamped into range"
    negative = AnalysisResult.model_validate(_result(0, confidence=-3))
    assert negative.confidence == 0.0
    as_text = AnalysisResult.model_validate(_result(0, discussion_points="点一；点二\n点三"))
    assert as_text.discussion_points == ["点一", "点二", "点三"]
    missing = AnalysisResult.model_validate({"index": 0})
    assert missing.confidence == 0.0 and missing.discussion_points == []


def test_apportion_sums_to_the_batch_usage():
    usage = ChatUsage(prompt_tokens=101, completion_tokens=52, total_tokens=153)
    shares = _apportion(usage, 3)
    assert sum(share[0] for share in shares) == 101
    assert sum(share[1] for share in shares) == 52
    assert len(shares) == 3
    assert _apportion(usage, 0) == []


def test_parse_semantic_groups_normalises_and_fills_leftovers():
    assert parse_semantic_groups({"groups": [[0, 2], [1]]}, range(3)) == [[0, 2], [1]]
    assert parse_semantic_groups({"groups": [[0, 2]]}, range(3)) == [[0, 2], [1]]
    assert parse_semantic_groups({"groups": [[0, 99], [7]]}, range(2)) == [[0], [1]]
    assert parse_semantic_groups("nonsense", range(2)) == []


# ------------------------------------------------------------------ batching
@pytest.mark.asyncio
async def test_analyze_items_batches_and_apportions_usage(item_factory):
    items = [item_factory("weibo", f"w{i}", f"热点条目{i}") for i in range(3)]
    client = StubDeepSeekClient(
        [
            {"results": [_result(0), _result(1)]},
            {"results": [_result(0)]},
        ]
    )
    pairs, batches, failed, problems = await analyze_items(
        client, items, profile=PROFILE, batch_size=2
    )
    assert batches == 2 and failed == 0 and problems == []
    assert len(pairs) == 3
    assert client.call_count == 2
    # Three items over two batches: 300 prompt tokens total, none lost.
    assert sum(analysis.prompt_tokens for _item, analysis in pairs) == 200
    assert sum(analysis.completion_tokens for _item, analysis in pairs) == 100
    assert pairs[1][0].platform_content_id == "w1", "index maps back to the right item"


@pytest.mark.asyncio
async def test_analyze_items_retries_once_then_records_failure(item_factory):
    items = [item_factory("weibo", "w1", "热点条目一"), item_factory("douyin", "d1", "热点条目二")]
    client = StubDeepSeekClient(
        [
            {"nonsense": True},  # batch 1, first try
            {"nonsense": True},  # batch 1, stricter retry
            {"results": [_result(0)]},  # batch 2 still processed
        ]
    )
    pairs, batches, failed, problems = await analyze_items(
        client, items, profile=PROFILE, batch_size=1
    )
    assert batches == 2
    assert failed == 1, "one bad batch must not stop the run"
    assert len(pairs) == 1
    assert client.call_count == 3
    assert any("batch 1" in problem for problem in problems)


@pytest.mark.asyncio
async def test_analyze_items_survives_unparseable_json(item_factory):
    client = StubDeepSeekClient([DeepSeekJSONError("not json"), DeepSeekJSONError("not json")])
    pairs, batches, failed, problems = await analyze_items(
        client, [item_factory("weibo", "w1", "热点条目")], profile=PROFILE, batch_size=1
    )
    assert pairs == [] and failed == 1
    assert any("unparseable JSON" in problem for problem in problems)


# ----------------------------------------------------------------- selection
def test_select_top_filters_and_orders(item_factory):
    items = [item_factory("weibo", f"w{i}", f"条目{i}", hot_value=i) for i in range(5)]
    pairs = [
        (items[0], AnalysisResult(index=0, recommended=True, confidence=0.6)),
        (items[1], AnalysisResult(index=1, recommended=False, confidence=0.99)),
        (items[2], AnalysisResult(index=2, recommended=True, confidence=0.9)),
        (items[3], AnalysisResult(index=3, recommended=True, confidence=0.7, is_duplicate=True)),
        (items[4], AnalysisResult(index=4, recommended=True, confidence=0.2)),
    ]
    selected = select_top(pairs, max_selected=10, min_confidence=0.5)
    assert [item.platform_content_id for item in selected] == ["w2", "w0"]


def test_select_top_respects_the_cap(item_factory):
    items = [item_factory("weibo", f"w{i}", f"条目{i}") for i in range(5)]
    pairs = [
        (item, AnalysisResult(index=index, recommended=True, confidence=0.9))
        for index, item in enumerate(items)
    ]
    assert len(select_top(pairs, max_selected=2, min_confidence=0.5)) == 2


# --------------------------------------------------------------- full run
async def _seed(sqlite_db, settings, item_factory, items):
    from app.services.pipeline.hot_pipeline import store_items

    async with session_scope(settings) as session:
        await store_items(items, settings=settings, session=session)


@pytest.mark.asyncio
async def test_run_analysis_persists_selects_and_fills_topic_summaries(
    sqlite_db, settings, item_factory
):
    await _seed(
        sqlite_db,
        settings,
        item_factory,
        [
            # A real cross-platform topic (same title, two platforms).
            item_factory("weibo", "w1", "某某重大事件", hot_value=900),
            item_factory("douyin", "d1", "某某重大事件", hot_value=800),
            # An advert that must never reach the model.
            item_factory("weibo", "w2", "加微信领取福利"),
            # A正常 item that the model will decline.
            item_factory("xiaohongshu", "x1", "无关紧要的日常分享", hot_value=10),
        ],
    )

    client = StubDeepSeekClient(
        [
            {
                "results": [
                    _result(0, recommended=True, confidence=0.91),
                    _result(1, recommended=True, confidence=0.55),
                    _result(2, recommended=False, confidence=0.1),
                ]
            }
        ]
    )
    result = await run_analysis(settings=settings, client=client)

    assert result.rule_filter["input"] == 4
    assert result.rule_filter["advertising"] == 1
    assert result.analysed == 3, "the advert is filtered before any token is spent"
    assert result.batches == 1
    assert result.stored["inserted"] == 3
    assert result.selected == 2, "confidence 0.55 passes the 0.5 floor, 0.1 does not"
    assert result.tokens["total_tokens"] == 150
    assert result.estimated_cny > 0

    async with session_scope(settings) as session:
        rows, total = await list_analyses(session, selected_only=True)
        assert total == 2
        assert {analysis.confidence for analysis, _item in rows} == {0.91, 0.55}
        stats = await database_stats(session)
        assert stats["ai_analyses"] == 3
        assert stats["selected_analyses"] == 2

    # The grouped pair shares one topic group, and its summary came from the
    # highest-confidence member — the column Phase 2 deliberately left empty.
    async with session_scope(settings) as session:
        from app.models.topic_group import TopicGroupRecord
        from sqlalchemy import select

        groups = (await session.execute(select(TopicGroupRecord))).scalars().all()
        assert len(groups) == 1
        assert groups[0].summary == "摘要0", "best member's summary fills the group"
        assert groups[0].platforms == ["douyin", "weibo"]


@pytest.mark.asyncio
async def test_run_analysis_is_free_to_repeat(sqlite_db, settings, item_factory):
    await _seed(sqlite_db, settings, item_factory, [item_factory("weibo", "w1", "某某重大事件")])
    first_client = StubDeepSeekClient([{"results": [_result(0)]}])
    await run_analysis(settings=settings, client=first_client)
    assert first_client.call_count == 1

    second_client = StubDeepSeekClient([])
    result = await run_analysis(settings=settings, client=second_client)
    assert second_client.call_count == 0, "a fresh analysis is reused for ANALYSIS_REUSE_HOURS"
    assert result.analysed == 0


@pytest.mark.asyncio
async def test_run_analysis_without_a_key_reports_instead_of_calling(sqlite_db, item_factory):
    from app.core.config import Settings

    bare = Settings(
        deepseek_api_key="",
        database_url="",
        tikhub_api_key="",
    )
    result = await run_analysis(settings=bare, client=StubDeepSeekClient([]))
    assert result.errors and "DEEPSEEK" in result.errors[0]


@pytest.mark.asyncio
async def test_merge_semantic_topics_regroups_across_platforms(sqlite_db, settings, item_factory):
    """Two platforms, different wordings, one event — the lexical layer cannot merge these."""
    await _seed(
        sqlite_db,
        settings,
        item_factory,
        [
            item_factory("douyin", "d1", "中国海警正告菲方停止侵权挑衅", hot_value=900),
            item_factory("weibo", "w1", "中国海警回应菲船只碰撞我海警艇", hot_value=800),
        ],
    )
    async with session_scope(settings) as session:
        rows = await recent_rows(session)
        assert len(rows) == 2
        assert {row.topic_group_id for row in rows} == {None}, "no lexical group exists"

        client = StubDeepSeekClient([{"groups": [[0, 1]]}])
        stats = await merge_semantic_topics(session, client, rows)

        assert stats["groups"] == 1
        assert stats["members_assigned"] == 2
        assert client.call_count == 1

        from sqlalchemy import select

        from app.models.hot_content import HotContentRecord

        updated = (await session.execute(select(HotContentRecord))).scalars().all()
        assert len({row.topic_group_id for row in updated}) == 1
        assert {row.platform for row in updated} == {"douyin", "weibo"}

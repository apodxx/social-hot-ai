"""Rewriter tests: the anti-copy check, the status chain, and persistence.

A stub client replaces DeepSeek, so the whole Phase 4 path runs without spending
a token.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.db.database import session_scope
from app.db.repository import database_stats, list_rewrites, recent_rows
from app.models.ai_rewrite import RewriteStatus
from app.services.ai.account_profile import AccountProfile
from app.services.ai.analyzer import run_analysis
from app.services.ai.deepseek import ChatResult, ChatUsage, DeepSeekJSONError
from app.services.ai.rewriter import (
    MIN_LENGTHS,
    RewriteResult,
    build_rewrite_prompt,
    check_mechanical_copy,
    compute_status,
    rewrite_one,
    run_rewriting,
)


class StubDeepSeekClient:
    """A DeepSeek stand-in with a queue of canned answers."""

    def __init__(
        self,
        responses: list[Any],
        *,
        model: str = "deepseek-flash",
        finish_reason: str = "",
    ) -> None:
        self._responses = list(responses)
        self.model = model
        self.finish_reason = finish_reason
        self.prompts: list[dict[str, Any]] = []
        self.total_usage = ChatUsage()
        self.call_count = 0

    async def complete_json(self, *, system: str, user: str, **kwargs: Any):
        self.prompts.append({"system": system, "user": user, **kwargs})
        self.call_count += 1
        payload = self._responses.pop(0)
        usage = ChatUsage(prompt_tokens=500, completion_tokens=800, total_tokens=1300)
        self.total_usage.add(usage)
        return payload, ChatResult(
            content=json.dumps(payload, ensure_ascii=False),
            model=self.model,
            usage=usage,
            finish_reason=self.finish_reason,
        )

    async def aclose(self) -> None:  # pragma: no cover
        return None


PROFILE = AccountProfile(field="AI / 科技", style="通俗", preferred_topics=["AI"])


def _analysis_payload(index: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "index": index,
        "topic": f"话题{index}",
        "summary": f"摘要{index}",
        "why_hot": "讨论多",
        "discussion_points": ["点一"],
        "account_fit": "适合",
        "content_angle": "技术原理切入",
        "is_duplicate": False,
        "recommended": True,
        "confidence": 0.95,
        "needs_verification": True,
    }
    base.update(overrides)
    return base


def _rewrite_payload(
    *,
    needs_verification: bool = True,
    risk_flags: list[str] | None = None,
    xhs_title: str = "换个角度聊聊这件事",
    xhs_body: str | None = None,
    weibo_body: str | None = None,
    douyin_script: str | None = None,
) -> dict[str, Any]:
    """A well-formed rewrite; the bodies are long enough to pass the length checks."""
    return {
        "summary": "原内容声称发生了某件事。",
        "why_hot": "因为大家在讨论它。",
        "angle": "从普通人的实际影响切入。",
        "xiaohongshu": {
            "title": xhs_title,
            "content": xhs_body
            or "第一段：先说结论。" * 12,
            "ending": "你怎么看？评论区聊聊。",
            "hashtags": ["#科技#", "#热点#"],
        },
        "weibo": {
            "opening": "一句话概括。",
            "title": "",
            "content": weibo_body or "短句表达观点。" * 12,
            "hashtags": ["#科技#"],
        },
        "douyin": {
            "hook": "前3秒钩子",
            "script": douyin_script or "口播脚本内容。" * 16,
            "scenes": ["画面一", "画面二"],
            "subtitles": "字幕要点",
            "cta": "关注我",
        },
        "risk_flags": risk_flags or [],
        "needs_verification": needs_verification,
        "verification_note": "原内容声称…；目前可确认…",
    }


# ------------------------------------------------------------- anti-copy check
def test_check_passes_a_genuinely_new_rewrite():
    result = RewriteResult.model_validate(_rewrite_payload())
    check = check_mechanical_copy(result, "某条与生成标题完全不同的原始标题")
    assert check.flags == []
    assert check.mechanical is False


def test_check_flags_a_title_that_was_not_rewritten():
    source = "GEO终于有标准了"
    result = RewriteResult.model_validate(_rewrite_payload(xhs_title=source))
    check = check_mechanical_copy(result, source)
    assert "title_not_rewritten" in check.flags
    assert check.title_similarity >= 0.95


def test_check_flags_identical_platform_versions():
    shared = "同一段话被复制到两个平台。" * 20
    result = RewriteResult.model_validate(_rewrite_payload(xhs_body=shared, weibo_body=shared))
    check = check_mechanical_copy(result, "原始标题")
    assert "platform_versions_identical" in check.flags


def test_check_flags_stub_lengths():
    result = RewriteResult.model_validate(_rewrite_payload(xhs_body="太短", weibo_body="短", douyin_script="也短"))
    check = check_mechanical_copy(result, "原始标题")
    assert sum(1 for flag in check.flags if flag.startswith("too_short:")) == 3
    for name, minimum in MIN_LENGTHS.items():
        assert minimum > 0


# ---------------------------------------------------------------- status chain
def test_status_is_ready_only_when_nothing_needs_a_human():
    result = RewriteResult.model_validate(_rewrite_payload(needs_verification=False))
    check = check_mechanical_copy(result, "原始标题")
    status, flags = compute_status(result, check, analysis_needs_verification=False)
    assert status is RewriteStatus.READY_TO_PUBLISH
    assert flags == []


def test_status_stays_needs_review_when_the_analysis_needed_verification():
    """The load-bearing rule: a rewrite inherits its analysis's uncertainty."""
    result = RewriteResult.model_validate(_rewrite_payload(needs_verification=False))
    check = check_mechanical_copy(result, "原始标题")
    status, _flags = compute_status(result, check, analysis_needs_verification=True)
    assert status is RewriteStatus.NEEDS_REVIEW


def test_status_stays_needs_review_on_risk_flags():
    result = RewriteResult.model_validate(
        _rewrite_payload(needs_verification=False, risk_flags=["造谣"])
    )
    check = check_mechanical_copy(result, "原始标题")
    status, flags = compute_status(result, check, analysis_needs_verification=False)
    assert status is RewriteStatus.NEEDS_REVIEW
    assert "造谣" in flags


def test_status_merges_copy_flags_into_risk_flags():
    shared = "重复内容。" * 30
    result = RewriteResult.model_validate(
        _rewrite_payload(needs_verification=False, xhs_body=shared, weibo_body=shared)
    )
    check = check_mechanical_copy(result, "原始标题")
    status, flags = compute_status(result, check, analysis_needs_verification=False)
    assert status is RewriteStatus.NEEDS_REVIEW
    assert "platform_versions_identical" in flags


# -------------------------------------------------------------------- prompt
def test_prompt_forbids_inventing_facts_without_a_body(item_factory):
    item = item_factory("weibo", "w1", "某热点标题")
    prompt = build_rewrite_prompt(PROFILE, item, _analysis_stub(), detail_text=None)
    assert "某热点标题" in prompt
    assert "AI / 科技" in prompt
    assert "没有抓到正文" in prompt
    assert "不要编造任何具体事实" in prompt
    assert "禁止机械改写" in prompt
    assert "风险控制" in prompt


def test_prompt_includes_detail_material_when_available(item_factory):
    item = item_factory("xiaohongshu", "x1", "某热点标题")
    prompt = build_rewrite_prompt(PROFILE, item, _analysis_stub(), detail_text="这是真实正文内容")
    assert "这是真实正文内容" in prompt
    assert "没有抓到正文" not in prompt


class _AnalysisStub:
    topic = "话题"
    summary = "摘要"
    why_hot = "原因"
    content_angle = "角度"
    discussion_points = ["点"]
    needs_verification = True


def _analysis_stub() -> _AnalysisStub:
    return _AnalysisStub()


# ------------------------------------------------------------------ rewrite_one
@pytest.mark.asyncio
async def test_rewrite_one_happy_path(settings, item_factory):
    client = StubDeepSeekClient([_rewrite_payload(needs_verification=False)])
    result, check, attempts, problems = await rewrite_one(
        client,
        item_factory("weibo", "w1", "原始标题"),
        _analysis_stub(),
        profile=PROFILE,
        settings=settings,
    )
    assert attempts == 1
    assert check.flags == []
    assert problems == []
    assert result.xiaohongshu.ending, "§17 requires a closing interaction"


@pytest.mark.asyncio
async def test_rewrite_one_retries_a_copy_and_keeps_the_better_attempt(settings, item_factory):
    source_title = "GEO终于有标准了"
    bad = _rewrite_payload(needs_verification=False, xhs_title=source_title, xhs_body="短")
    good = _rewrite_payload(needs_verification=False)
    client = StubDeepSeekClient([bad, good])

    result, check, attempts, problems = await rewrite_one(
        client, item_factory("weibo", "w1", source_title), _analysis_stub(),
        profile=PROFILE, settings=settings,
    )
    assert attempts == 2
    assert check.flags == [], "the retry replaced the flagged attempt"
    assert result.xiaohongshu.title == good["xiaohongshu"]["title"]
    assert any("first attempt" in problem for problem in problems)
    # The stricter instruction must have been appended to the retry prompt.
    assert "完全重写" in client.prompts[1]["user"]
    # Both calls were paid for, so the row's recorded spend covers both. Attributing
    # only the kept attempt would understate the real cost of this row.
    assert (result.prompt_tokens, result.completion_tokens) == (1000, 1600)


@pytest.mark.asyncio
async def test_rewrite_one_records_this_rows_token_usage(settings, item_factory):
    """Per-row spend for the most expensive call in the pipeline.

    A real run showed ``tokens {'prompt': 0, 'completion': 0}`` on every rewrite row
    while the run-level total was correct — the cost was unauditable exactly where it
    mattered most. The analyser apportions a batch's usage because several items share
    one call; a rewrite is one call per item, so this is the exact figure.
    """
    client = StubDeepSeekClient([_rewrite_payload(needs_verification=False)])
    result, _check, attempts, _problems = await rewrite_one(
        client, item_factory("weibo", "w1", "某个话题"), _analysis_stub(),
        profile=PROFILE, settings=settings,
    )
    assert attempts == 1
    assert result.prompt_tokens == 500
    assert result.completion_tokens == 800


@pytest.mark.asyncio
async def test_run_rewriting_persists_the_per_row_tokens(sqlite_db, settings, item_factory):
    """The columns must reach the database, not just the in-memory result."""
    await _seed(sqlite_db, settings, item_factory, [item_factory("weibo", "w1", "某热点事件丙")])
    await run_analysis(
        settings=settings,
        client=StubDeepSeekClient([{"results": [_analysis_payload(0)]}]),
    )
    await run_rewriting(
        settings=settings,
        client=StubDeepSeekClient([_rewrite_payload(needs_verification=False)]),
    )

    async with session_scope(settings) as session:
        rows, _total = await list_rewrites(session)
    rewrite, _item = rows[0]
    assert (rewrite.prompt_tokens, rewrite.completion_tokens) == (500, 800)


@pytest.mark.asyncio
async def test_rewrite_one_keeps_the_first_when_the_retry_is_worse(settings, item_factory):
    source_title = "GEO终于有标准了"
    slightly_bad = _rewrite_payload(needs_verification=False, xhs_title=source_title)
    worse = _rewrite_payload(needs_verification=False, xhs_title=source_title, xhs_body="短", weibo_body="短")
    client = StubDeepSeekClient([slightly_bad, worse])

    _result, check, attempts, _problems = await rewrite_one(
        client, item_factory("weibo", "w1", source_title), _analysis_stub(),
        profile=PROFILE, settings=settings,
    )
    assert attempts == 2
    assert check.flags == ["title_not_rewritten"], "the fewer-flag attempt is kept"


@pytest.mark.asyncio
async def test_rewrite_one_reports_truncation_instead_of_a_json_error(settings, item_factory):
    """A real run hit REWRITE_MAX_TOKENS and looked like a parsing bug.

    ``finish_reason == "length"`` must be surfaced as truncation, because the fix
    is a bigger ceiling — retrying would truncate identically.
    """
    client = StubDeepSeekClient([_rewrite_payload()], finish_reason="length")
    with pytest.raises(DeepSeekJSONError) as excinfo:
        await rewrite_one(
            client,
            item_factory("weibo", "w1", "原始标题"),
            _analysis_stub(),
            profile=PROFILE,
            settings=settings,
        )
    message = str(excinfo.value)
    assert "truncated" in message and "REWRITE_MAX_TOKENS" in message


@pytest.mark.asyncio
async def test_run_rewriting_records_a_truncated_item_as_failed(
    sqlite_db, settings, item_factory
):
    await _seed(sqlite_db, settings, item_factory, [item_factory("weibo", "w1", "某热点事件")])
    analysis_client = StubDeepSeekClient([{"results": [_analysis_payload(0)]}])
    await run_analysis(settings=settings, client=analysis_client)

    client = StubDeepSeekClient([_rewrite_payload()], finish_reason="length")
    result = await run_rewriting(settings=settings, client=client)
    assert result.failed == 1 and result.rewritten == 0
    assert any("truncated" in error for error in result.errors)


# ------------------------------------------------------------------- full run
async def _seed(sqlite_db, settings, item_factory, items):
    from app.services.pipeline.hot_pipeline import store_items

    async with session_scope(settings) as session:
        await store_items(items, settings=settings, session=session)


@pytest.mark.asyncio
async def test_run_rewriting_persists_and_propagates_verification(
    sqlite_db, settings, item_factory
):
    await _seed(
        sqlite_db,
        settings,
        item_factory,
        [
            item_factory("weibo", "w1", "某热点事件甲", hot_value=900),
            item_factory("douyin", "d1", "某热点事件乙", hot_value=800),
        ],
    )
    analysis_client = StubDeepSeekClient(
        [{"results": [_analysis_payload(0), _analysis_payload(1)]}]
    )
    await run_analysis(settings=settings, client=analysis_client)

    rewrite_client = StubDeepSeekClient(
        [_rewrite_payload(needs_verification=False), _rewrite_payload(needs_verification=False)]
    )
    result = await run_rewriting(settings=settings, client=rewrite_client)

    assert result.considered == 2
    assert result.rewritten == 2 and result.failed == 0
    assert result.stored["inserted"] == 2
    # Both analyses said needs_verification=true, so neither rewrite may ship.
    assert result.ready_to_publish == 0
    assert result.needs_review == 2
    assert result.tokens["total_tokens"] == 2600
    assert result.estimated_cny > 0

    async with session_scope(settings) as session:
        rows, total = await list_rewrites(session)
        assert total == 2
        rewrite, item = rows[0]
        assert rewrite.status == RewriteStatus.NEEDS_REVIEW.value
        assert rewrite.needs_verification is True
        assert rewrite.verification_note
        # Attribution is not duplicated onto the rewrite row: it is resolved
        # through this join, which is why the item comes back with the row.
        assert item.platform in {"weibo", "douyin"}
        assert item.title in {"某热点事件甲", "某热点事件乙"}
        assert rewrite.xiaohongshu_hashtags
        assert rewrite.douyin_scene_suggestions
        stats = await database_stats(session)
        assert stats["ai_rewrites"] == 2
        assert stats["ready_to_publish"] == 0


@pytest.mark.asyncio
async def test_run_rewriting_is_free_to_repeat(sqlite_db, settings, item_factory):
    await _seed(sqlite_db, settings, item_factory, [item_factory("weibo", "w1", "某热点事件")])
    analysis_client = StubDeepSeekClient([{"results": [_analysis_payload(0)]}])
    await run_analysis(settings=settings, client=analysis_client)

    first = StubDeepSeekClient([_rewrite_payload()])
    await run_rewriting(settings=settings, client=first)
    assert first.call_count == 1

    second = StubDeepSeekClient([])
    result = await run_rewriting(settings=settings, client=second)
    assert second.call_count == 0, "REWRITE_REUSE_HOURS must prevent a second bill"
    assert result.considered == 0


@pytest.mark.asyncio
async def test_run_rewriting_only_touches_selected_items(sqlite_db, settings, item_factory):
    await _seed(
        sqlite_db,
        settings,
        item_factory,
        [item_factory("weibo", "w1", "会被选中的事件"), item_factory("douyin", "d1", "不会被选中的事件")],
    )
    analysis_client = StubDeepSeekClient(
        [
            {
                "results": [
                    _analysis_payload(0, recommended=True, confidence=0.9),
                    _analysis_payload(1, recommended=False, confidence=0.1),
                ]
            }
        ]
    )
    await run_analysis(settings=settings, client=analysis_client)

    client = StubDeepSeekClient([_rewrite_payload()])
    result = await run_rewriting(settings=settings, client=client)
    assert result.considered == 1, "only Phase 3's selection reaches rewriting"
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_run_rewriting_without_a_key_reports_instead_of_calling(sqlite_db):
    from app.core.config import Settings

    bare = Settings(deepseek_api_key="", deepseek_model="", database_url="", tikhub_api_key="")
    result = await run_rewriting(settings=bare, client=StubDeepSeekClient([]))
    assert result.errors and "DEEPSEEK" in result.errors[0]

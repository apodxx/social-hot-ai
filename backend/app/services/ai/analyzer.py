"""Phase 3: DeepSeek hot-topic analysis and selection.

Flow (spec sections 十二, 十四, 十五, 三十五):

1. take items that have no fresh analysis;
2. **rule filter** them for free (advertising, marketing, empty, low quality,
   abnormal counters) — no tokens spent on junk;
3. cap the survivors and pick them platform-balanced (``select_candidates``);
4. ask DeepSeek, **in batches**, for the structured judgement the spec lists:
   what the topic is, why it is hot, what people discuss, whether it fits the
   account, whether there is room for a rewrite, whether it duplicates another
   item, and whether a human must verify the facts;
5. select the 5-10 that go on to Phase 4;
6. persist every analysis and fill ``topic_groups.summary``.

Cost decisions worth naming:

* **Batching.** The spec's cost rule is explicit; ten items per request means
  three requests for thirty items instead of thirty requests.
* **No ``raw_data`` in the prompt.** Only index/platform/title/hot_value/rank are
  sent. The provider JSON is what we store for debugging, not what we pay tokens
  to re-read.
* **Rule filtering before the model**, never after.
* **One analysis per item, reused for ``ANALYSIS_REUSE_HOURS``**, so a repeated
  run is not a repeated bill.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.db.repository import (
    AnalysisUpsertStats,
    apply_topic_clusters,
    refresh_topic_summaries,
    rows_needing_analysis,
    upsert_analyses,
)
from app.services.ai.account_profile import AccountProfile, load_account_profile
from app.services.ai.deepseek import (
    ChatUsage,
    DeepSeekClient,
    DeepSeekJSONError,
)
from app.services.pipeline.dedup import normalize_title
from app.services.pipeline.interest import (
    InterestProfile,
    annotate,
    relevance_summary,
    select_relevant_candidates,
)
from app.services.pipeline.rule_filter import rule_filter, select_candidates
from app.services.pipeline.topic_grouping import TopicCluster, platform_name

logger = logging.getLogger(__name__)

ANALYSIS_SYSTEM_PROMPT = """你是资深内容策划与热点分析师，为一个社媒内容团队服务。
你只输出严格合法的 JSON：不要 Markdown 代码块、不要解释性文字、不要注释、不要多余标点。
你不编造未提供的事实；无法确认的信息必须标记 needs_verification，并用"原内容声称"这类措辞。"""

SEMANTIC_SYSTEM_PROMPT = """你是话题归并助手。你只输出严格合法的 JSON，不要任何解释文字。
你的任务是判断哪些条目在讲同一件真实事件（允许措辞不同、平台不同），并据此分组。"""


class AnalysisResult(BaseModel):
    """One validated analysis. Anything unparseable never becomes an instance."""

    index: int
    topic: str = ""
    summary: str = ""
    why_hot: str = ""
    discussion_points: list[str] = Field(default_factory=list)
    account_fit: str = ""
    content_angle: str = ""
    is_duplicate: bool = False
    recommended: bool = False
    confidence: float = 0.0
    needs_verification: bool = False

    #: Internal bookkeeping (batch usage apportioned across the batch's items,
    #: and the model's raw object for this index). Not part of the provider
    #: contract, but stored so a wrong judgement can be debugged.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalise_confidence(cls, value: Any) -> float:
        """Accept 0-1 or a percentage, clamp to ``[0, 1]``."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if number > 1.0:
            number = number / 100.0
        return max(0.0, min(1.0, number))

    @field_validator("discussion_points", mode="before")
    @classmethod
    def _normalise_points(cls, value: Any) -> list[str]:
        """Accept a list, or one string with the points separated by newlines."""
        if value is None:
            return []
        if isinstance(value, str):
            parts = [part.strip(" -•\t") for part in value.replace("；", "\n").splitlines()]
            return [part for part in parts if part]
        if isinstance(value, (list, tuple)):
            return [str(part).strip() for part in value if str(part).strip()]
        return []

    @field_validator("topic", "summary", "why_hot", "account_fit", "content_angle", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


@dataclass
class AnalysisRunResult:
    """Everything one analysis pass did."""

    rule_filter: dict[str, int] = field(default_factory=dict)
    #: How the paid candidates were chosen (Phase 10). Reported so a run shows *why*
    #: these items were analysed, not just how many.
    interest: dict[str, Any] = field(default_factory=dict)
    batches: int = 0
    analysed: int = 0
    failed_batches: int = 0
    selected: int = 0
    stored: dict[str, int] = field(default_factory=dict)
    topic_summaries_updated: int = 0
    semantic_merge: dict[str, int] | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    estimated_cny: float = 0.0
    model: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_filter": self.rule_filter,
            "interest": self.interest,
            "batches": self.batches,
            "analysed": self.analysed,
            "failed_batches": self.failed_batches,
            "selected": self.selected,
            "stored": self.stored,
            "topic_summaries_updated": self.topic_summaries_updated,
            "semantic_merge": self.semantic_merge,
            "tokens": self.tokens,
            "estimated_cny": round(self.estimated_cny, 4),
            "model": self.model,
            "errors": self.errors,
        }


def describe_item(index: int, item: Any) -> dict[str, Any]:
    """The minimal, cheap projection of an item that is sent to the model."""
    return {
        "index": index,
        "platform": platform_name(item),
        "title": (getattr(item, "title", "") or "")[:120],
        "hot_value": getattr(item, "hot_value", None),
        "rank": getattr(item, "rank", None),
    }


def build_batch_prompt(profile: AccountProfile, items: Sequence[Any], today: date) -> str:
    """The user message for one batch."""
    described = [describe_item(index, item) for index, item in enumerate(items)]
    return f"""{profile.to_prompt_block()}

今天是 {today.isoformat()}。下面是 {len(described)} 条待分析的热点：

{json.dumps(described, ensure_ascii=False, indent=2)}

请逐条分析，并且只输出下面这个 JSON 结构（不要任何其他文字）：
{{"results": [{{
  "index": 0,
  "topic": "这条讲的是什么话题，不超过30字",
  "summary": "一句话概括事件，不超过60字",
  "why_hot": "为什么受到关注，不超过80字",
  "discussion_points": ["用户主要讨论点1", "用户主要讨论点2"],
  "account_fit": "是否适合当前账号，并给出理由，不超过60字",
  "content_angle": "可用的二创角度，不超过60字",
  "is_duplicate": false,
  "recommended": true,
  "confidence": 0.0,
  "needs_verification": false
}}]}}

规则：
1. results 必须恰好包含全部 {len(described)} 条，index 与输入一一对应，不得增加、删除或重复。
2. confidence 是 0 到 1 之间的数字，表示你对"这条值得做二创"的信心。
3. 涉及具体人物、数字、新闻事件、政策、医疗、金融、科技参数或社会事件时，若无法确认，把 needs_verification 设为 true，
   并在 summary 中区分"原内容声称"与"目前可确认"。
4. is_duplicate 表示这条与本批其他条目讲的是同一件事。
5. 不要复制原文，不要编造输入中没有提供的事实。"""


def build_semantic_prompt(items: Sequence[Any]) -> str:
    """The user message that asks which of these items are the same event."""
    described = [describe_item(index, item) for index, item in enumerate(items)]
    return f"""下面是 {len(described)} 条已经筛过的热点：

{json.dumps(described, ensure_ascii=False, indent=2)}

请判断哪些条目在讲**同一件真实事件**（措辞可以不同、平台可以不同）。只输出：
{{"groups": [[0, 3], [1], [2]]}}

规则：
1. 每个数组是一组"同一事件"的 index，必须覆盖全部 {len(described)} 条且不重复。
2. 只把确实讲同一件事的放在一起；仅仅是同一领域、同一关键词但不同事件，不要合并。
3. 单独成组的条目就用单元素数组表示。"""


def parse_batch(payload: Any, expected_indices: Iterable[int]) -> tuple[list[AnalysisResult], list[str]]:
    """Validate a batch answer; returns ``(results, problems)``.

    The spec forbids unparseable output, so anything that does not match is
    reported as a problem and dropped — never stored as prose.
    """
    problems: list[str] = []
    expected = set(expected_indices)
    raw_results: Any
    if isinstance(payload, dict):
        raw_results = payload.get("results")
        if raw_results is None:
            # Tolerate a single object, or an index-keyed mapping.
            if "index" in payload:
                raw_results = [payload]
            elif payload and all(str(key).isdigit() for key in payload):
                raw_results = [
                    {**value, "index": int(key)} if isinstance(value, dict) else value
                    for key, value in payload.items()
                ]
    elif isinstance(payload, list):
        raw_results = payload
    else:
        raw_results = None

    if not isinstance(raw_results, list):
        return [], [f"response had no 'results' array (got {type(payload).__name__})"]

    results: list[AnalysisResult] = []
    seen: set[int] = set()
    for entry in raw_results:
        if not isinstance(entry, dict):
            problems.append("a result entry was not an object")
            continue
        try:
            result = AnalysisResult.model_validate(entry)
        except Exception as exc:  # noqa: BLE001 - validation detail is the message
            problems.append(f"result could not be validated: {exc}")
            continue
        if result.index not in expected:
            problems.append(f"result index {result.index} was not requested")
            continue
        if result.index in seen:
            problems.append(f"result index {result.index} appeared twice")
            continue
        seen.add(result.index)
        results.append(result)

    missing = sorted(expected - seen)
    if missing:
        problems.append(f"no result for indices {missing}")
    return results, problems


def parse_semantic_groups(payload: Any, expected_indices: Iterable[int]) -> list[list[int]]:
    """Validate the semantic-merge answer into clean index groups."""
    expected = set(expected_indices)
    raw_groups = payload.get("groups") if isinstance(payload, dict) else payload
    if not isinstance(raw_groups, list):
        return []
    seen: set[int] = set()
    groups: list[list[int]] = []
    for group in raw_groups:
        if not isinstance(group, list):
            continue
        members = [
            int(value)
            for value in group
            if isinstance(value, (int, float)) and int(value) in expected and int(value) not in seen
        ]
        if not members:
            continue
        seen.update(members)
        groups.append(members)
    leftover = sorted(expected - seen)
    if leftover:
        groups.extend([[index] for index in leftover])
    return groups


def _apportion(usage: ChatUsage, count: int) -> list[tuple[int, int]]:
    """Split a batch's token usage across its items so row costs sum correctly."""
    if count <= 0:
        return []
    prompt_each, prompt_rest = divmod(usage.prompt_tokens, count)
    completion_each, completion_rest = divmod(usage.completion_tokens, count)
    shares = [(prompt_each, completion_each) for _ in range(count)]
    if shares:
        shares[0] = (prompt_each + prompt_rest, completion_each + completion_rest)
    return shares


async def analyze_items(
    client: DeepSeekClient,
    items: Sequence[Any],
    *,
    profile: AccountProfile,
    batch_size: int,
    today: date | None = None,
) -> tuple[list[tuple[Any, AnalysisResult]], int, int, list[str]]:
    """Analyse ``items`` in batches.

    :returns: ``(pairs, batch_count, failed_batches, problems)`` where ``pairs``
        is ``(item, result)``.
    """
    resolved_day = today or datetime.now(timezone.utc).date()
    pairs: list[tuple[Any, AnalysisResult]] = []
    problems: list[str] = []
    batches = failed = 0

    size = max(1, batch_size)
    for start in range(0, len(items), size):
        chunk = list(items[start : start + size])
        batches += 1
        prompt = build_batch_prompt(profile, chunk, resolved_day)
        try:
            payload, result = await client.complete_json(
                system=ANALYSIS_SYSTEM_PROMPT, user=prompt
            )
            parsed, batch_problems = parse_batch(payload, range(len(chunk)))
            if not parsed:
                # One stricter retry before giving up on this batch.
                logger.warning("batch %d produced no usable results; retrying once", batches)
                payload, result = await client.complete_json(
                    system=ANALYSIS_SYSTEM_PROMPT,
                    user=prompt + "\n\n注意：必须只输出 JSON，且 results 数量与 index 必须完全匹配。",
                )
                parsed, batch_problems = parse_batch(payload, range(len(chunk)))
        except DeepSeekJSONError as exc:
            failed += 1
            problems.append(f"batch {batches}: unparseable JSON ({exc})")
            logger.error("batch %d: unparseable JSON", batches)
            continue

        if not parsed:
            failed += 1
            problems.extend(f"batch {batches}: {problem}" for problem in batch_problems)
            continue

        shares = _apportion(result.usage, len(parsed))
        for (item, analysis), (prompt_tokens, completion_tokens) in zip(
            ((chunk[entry.index], entry) for entry in parsed), shares
        ):
            analysis.prompt_tokens = prompt_tokens
            analysis.completion_tokens = completion_tokens
            # Keep the model's own object for this index (minus the copy of
            # itself) so a wrong judgement can be debugged later.
            analysis.raw = analysis.model_dump(mode="json", exclude={"raw"})
            pairs.append((item, analysis))
        problems.extend(f"batch {batches}: {problem}" for problem in batch_problems)

    return pairs, batches, failed, problems


def select_top(
    pairs: Sequence[tuple[Any, AnalysisResult]],
    *,
    max_selected: int,
    min_confidence: float,
) -> list[Any]:
    """Pick the items that advance to Phase 4.

    Only recommended, confident, non-duplicate items qualify; ties break toward
    the hotter item so the selection is reproducible from the data.
    """
    eligible = [
        (item, analysis)
        for item, analysis in pairs
        if analysis.recommended
        and not analysis.is_duplicate
        and analysis.confidence >= min_confidence
    ]
    eligible.sort(
        key=lambda pair: (
            -pair[1].confidence,
            -(getattr(pair[0], "hot_value", None) or 0),
        )
    )
    return [item for item, _analysis in eligible[:max_selected]]


async def merge_semantic_topics(
    session: AsyncSession,
    client: DeepSeekClient,
    rows: Sequence[Any],
) -> dict[str, int]:
    """One extra call that regroups items the lexical layer could not merge.

    Phase 2's layer 4 matches characters, so two platforms covering one event in
    different words stay apart (measured: the best real cross-platform pair scored
    0.35 against a 0.82 threshold). This asks the model which of the selected
    items describe the same event, then reuses the Phase 2 persistence path.
    """
    if len(rows) < 2:
        return {"calls": 0, "groups": 0, "members_assigned": 0}
    payload, _result = await client.complete_json(
        system=SEMANTIC_SYSTEM_PROMPT, user=build_semantic_prompt(rows)
    )
    groups = parse_semantic_groups(payload, range(len(rows)))
    clusters: list[TopicCluster] = []
    for members in groups:
        if len(members) < 2:
            continue
        member_rows = [rows[index] for index in members]
        representative = max(
            member_rows, key=lambda row: (getattr(row, "hot_value", None) or 0, len(row.title or ""))
        )
        topic = (representative.title or "").strip()
        clusters.append(
            TopicCluster(
                topic=topic,
                topic_normalized=normalize_title(topic),
                platforms=sorted({platform_name(row) for row in member_rows}),
                members=list(member_rows),
            )
        )
    stats = await apply_topic_clusters(session, clusters)
    return {"calls": 1, "groups": len(clusters), **stats.as_dict()}


async def run_analysis(
    *,
    settings: Settings | None = None,
    session: AsyncSession | None = None,
    client: DeepSeekClient | None = None,
    profile: AccountProfile | None = None,
    max_candidates: int | None = None,
    only_ids: Sequence[int] | None = None,
) -> AnalysisRunResult:
    """Run the Phase 3 analysis pass.

    **This spends DeepSeek tokens** — bounded by ``ANALYSIS_MAX_CANDIDATES``,
    ``ANALYSIS_BATCH_SIZE`` and ``ANALYSIS_REUSE_HOURS``.

    ``only_ids`` analyses exactly those items (the per-item "AI 分析" button). The
    interest filter is then bypassed on purpose: the operator picked the item, so the
    filter has already had its say and must not overrule an explicit choice.
    """
    resolved = settings or get_settings()
    if session is not None:
        return await _run(resolved, session, client, profile, max_candidates, only_ids)
    async with session_scope(resolved) as own_session:
        return await _run(resolved, own_session, client, profile, max_candidates, only_ids)


async def _run(
    settings: Settings,
    session: AsyncSession,
    client: DeepSeekClient | None,
    profile: AccountProfile | None,
    max_candidates: int | None,
    only_ids: Sequence[int] | None = None,
) -> AnalysisRunResult:
    resolved_profile = profile or load_account_profile(settings.account_profile_file)
    result = AnalysisRunResult()
    if not settings.deepseek_configured:
        result.errors.append("DEEPSEEK_API_KEY or DEEPSEEK_MODEL is not configured")
        return result

    rows = await rows_needing_analysis(
        session,
        hours=settings.dedup_window_hours,
        limit=settings.dedup_max_compare,
        reuse_hours=settings.analysis_reuse_hours,
        only_ids=only_ids,
    )
    if not rows:
        logger.info("analysis: nothing to do (every item already has a fresh analysis)")
        return result

    filtered = rule_filter(rows)
    result.rule_filter = filtered.summary()
    candidate_limit = max_candidates or settings.analysis_max_candidates
    # Relevance first, heat second. This is the step that decides what the paid
    # analysis actually sees — before Phase 10 it was pure heat order, so an account
    # about technology and programming paid to analyse whatever was trending.
    interest = InterestProfile.from_settings(settings)
    scores = annotate(filtered.kept, interest)
    if only_ids is not None:
        # An explicit per-item request outranks the interest filter and the candidate
        # cap: the operator pointed at this item, so it is analysed.
        candidates = list(filtered.kept)
        result.interest = {
            "enabled": interest.configured,
            "only": interest.only,
            "bypassed": True,
            "reason": "explicit per-item request",
            "candidates": len(candidates),
        }
        result.interest = {
            **result.interest,
            **relevance_summary(filtered.kept, interest),
        }
    else:
        candidates = select_relevant_candidates(
            filtered.kept, candidate_limit, profile=interest, scores=scores
        )
        result.interest = {
            "enabled": interest.configured,
            "only": interest.only,
            **relevance_summary(filtered.kept, interest),
            "candidates": len(candidates),
            "candidate_scores": [
                {
                    "title": str(getattr(item, "title", ""))[:40],
                    "score": scores[id(item)].score,
                    "matched": scores[id(item)].matched[:5],
                }
                for item in candidates[:10]
            ],
        }
        if not candidates and interest.configured and interest.only:
            # Silence here would look like a broken pipeline. Say what happened, and say
            # what the fix is, rather than paying to analyse entertainment instead.
            result.interest["skipped_reason"] = (
                "INTEREST_ONLY is on and no stored item matched an interest keyword; "
                "nothing was analysed. Add WATCH_KEYWORDS or widen INTEREST_KEYWORDS."
            )
            logger.warning("analysis: %s", result.interest["skipped_reason"])
    logger.info(
        "analysis: %d rows -> %d after rules -> %d candidates (%d relevant by interest)",
        len(rows),
        len(filtered.kept),
        len(candidates),
        result.interest["relevant"],
    )

    owns_client = client is None
    active_client = client or DeepSeekClient(settings)
    result.model = active_client.model
    try:
        pairs, batches, failed, problems = await analyze_items(
            active_client,
            candidates,
            profile=resolved_profile,
            batch_size=settings.analysis_batch_size,
        )
        result.batches = batches
        result.failed_batches = failed
        result.analysed = len(pairs)
        result.errors.extend(problems)

        selected_items = select_top(
            pairs,
            max_selected=settings.analysis_max_selected,
            min_confidence=settings.analysis_min_confidence,
        )
        selected_ids = {getattr(item, "id", None) for item in selected_items}
        stats: AnalysisUpsertStats = await upsert_analyses(
            session,
            [
                (item, analysis, getattr(item, "id", None) in selected_ids)
                for item, analysis in pairs
            ],
            model=active_client.model,
        )
        result.stored = stats.as_dict()
        result.selected = stats.selected
        result.topic_summaries_updated = await refresh_topic_summaries(session)

        if settings.analysis_semantic_merge and selected_items:
            try:
                result.semantic_merge = await merge_semantic_topics(
                    session, active_client, selected_items
                )
            except Exception as exc:  # noqa: BLE001 - never fail the run over this
                logger.error("semantic merge failed: %s", exc)
                result.errors.append(f"semantic merge failed: {exc}")

        usage = active_client.total_usage
        result.tokens = usage.as_dict()
        result.estimated_cny = usage.estimated_cny()
    finally:
        if owns_client:
            await active_client.aclose()

    logger.info("analysis run complete: %s", result.as_dict())
    return result

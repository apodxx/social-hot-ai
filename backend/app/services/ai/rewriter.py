"""Phase 4: the rewriting agent (spec sections 十七 to 二十).

One call per selected item, producing all three platform versions. Batching is
deliberately **not** used here, unlike Phase 3: the output is long-form creative
text, and packing several items into one request risks truncation and
cross-contamination between topics. The cost is bounded instead by
``ANALYSIS_MAX_SELECTED`` (at most ten items reach this stage).

Two rules from the spec are enforced by *code*, not only by the prompt, because
a prompt is a request and code is a guarantee:

* **§18 — no mechanical rewriting.** :func:`check_mechanical_copy` measures what is
  measurable without a source body (see its docstring), and a flagged attempt is
  regenerated once with the problems spelled out; if it still fails, the row is
  marked ``NEEDS_REVIEW`` with a ``mechanical_rewrite`` flag rather than shipped.
* **§19/§20 — verification and risk.** The model reports risk flags and whether a
  human must verify; :func:`compute_status` then decides ``READY_TO_PUBLISH``
  versus ``NEEDS_REVIEW``. The model never sets the status itself, and a rewrite
  **cannot** be publish-ready when its Phase 3 analysis needed verification.

The input a rewrite gets is the analysis plus whatever the source exposes — for
these platforms that is a hot *word* and no body, because section 十六's detail
fetching is not implemented yet. The prompt therefore forbids inventing specifics
and every result carries ``needs_verification`` until a human confirms the facts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Sequence

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.db.repository import (
    RewriteUpsertStats,
    rows_needing_rewrite,
    upsert_rewrites,
)
from app.models.ai_rewrite import RewriteStatus
from app.services.ai.account_profile import AccountProfile
from app.services.ai.analyzer import describe_item
from app.services.ai.deepseek import DeepSeekClient, DeepSeekJSONError
from app.services.pipeline.dedup import normalize_title, title_similarity
from app.services.pipeline.topic_grouping import platform_name

logger = logging.getLogger(__name__)

REWRITE_SYSTEM_PROMPT = """你是一位资深社媒内容创作者，为一个账号做热点二次创作。
你只输出严格合法的 JSON：不要 Markdown 代码块、不要解释文字、不要多余标点。
你必须原创表达：理解原内容后重新组织语言，禁止复制原文、禁止只替换同义词、禁止只改标题。
你不知道的事实绝不编造；无法确认的信息必须用"原内容声称"表述，并把 needs_verification 设为 true。"""

#: §20's risk list. Any hit forces NEEDS_REVIEW.
RISK_CATEGORIES: tuple[str, ...] = (
    "明显虚假信息",
    "造谣",
    "个人隐私",
    "恶意攻击",
    "违法内容",
    "明显侵权风险",
)

#: Minimum body lengths that distinguish a real rewrite from a stub.
MIN_LENGTHS: dict[str, int] = {
    "xiaohongshu_content": 80,
    "weibo_content": 60,
    "douyin_script": 100,
}


class XiaohongshuVersion(BaseModel):
    """§17.4 — title, body, closing interaction, hashtags."""

    title: str = ""
    content: str = ""
    ending: str = ""
    hashtags: list[str] = Field(default_factory=list)


class WeiboVersion(BaseModel):
    """§17.5 — opening, body, hashtags."""

    opening: str = ""
    title: str = ""
    content: str = ""
    hashtags: list[str] = Field(default_factory=list)


class DouyinVersion(BaseModel):
    """§17.6 — hook, script, scene suggestions, subtitles, CTA."""

    hook: str = ""
    script: str = ""
    scenes: list[str] = Field(default_factory=list)
    subtitles: str = ""
    cta: str = ""


class ImageSlot(BaseModel):
    """One picture's place in the 图文 layout."""

    index: int = 0
    #: What this picture is for, in the post's flow ("提出问题" / "给出对比").
    role: str = ""
    #: Text to put **on** the image (Xiaohongshu cover text is the click driver).
    overlay: str = ""
    #: One line of body copy this image sits next to.
    caption: str = ""


class ImagePlan(BaseModel):
    """The 图文 (image-text) layout plan.

    What this can and cannot be is worth stating plainly: the model is **text-only**,
    so it has never seen the pictures. It does not know what is *in* them. What it can
    plan is structure — which picture leads, what text to overlay, how the body maps
    onto the available frames — and that is genuinely useful, because the order and the
    cover text are what decide whether a 图文 post gets opened.

    ``planned_blind`` records that boundary so nobody later mistakes the captions for a
    description of the image content.
    """

    cover_index: int = 0
    cover_text: str = ""
    caption_strategy: str = ""
    slots: list[ImageSlot] = Field(default_factory=list)
    planned_blind: bool = True
    note: str = ""


class RewriteResult(BaseModel):
    """One validated rewrite. Prose that cannot be parsed never becomes this."""

    summary: str = ""
    why_hot: str = ""
    angle: str = ""
    xiaohongshu: XiaohongshuVersion = Field(default_factory=XiaohongshuVersion)
    weibo: WeiboVersion = Field(default_factory=WeiboVersion)
    douyin: DouyinVersion = Field(default_factory=DouyinVersion)
    risk_flags: list[str] = Field(default_factory=list)
    needs_verification: bool = True
    verification_note: str = ""

    #: The 图文 layout plan (Phase 9). Present only when the item actually has images.
    image_plan: ImagePlan | None = None

    #: Internal bookkeeping. One rewrite is one API call, so this is the exact spend
    #: for this row (both attempts summed when §18 forced a retry) — not an estimate.
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @field_validator("risk_flags", mode="before")
    @classmethod
    def _normalise_flags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.replace("；", "\n").splitlines() if part.strip()]
        if isinstance(value, (list, tuple)):
            return [str(part).strip() for part in value if str(part).strip()]
        return []


@dataclass
class CopyCheck:
    """The result of the §18 anti-copy measurement."""

    title_similarity: float = 0.0
    cross_platform_similarity: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def mechanical(self) -> bool:
        """True when at least one anti-copy rule fired."""
        return bool(self.flags)

    @property
    def similarity(self) -> float:
        """Worst (highest) similarity measured, for the record."""
        return max(self.title_similarity, self.cross_platform_similarity)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title_similarity": round(self.title_similarity, 4),
            "cross_platform_similarity": round(self.cross_platform_similarity, 4),
            "flags": list(self.flags),
        }


def check_mechanical_copy(
    result: RewriteResult,
    source_title: str,
    *,
    title_similarity_limit: float = 0.95,
    cross_platform_limit: float = 0.9,
) -> CopyCheck:
    """Measure the part of §18 that is checkable without a source body.

    What this can and cannot do, stated plainly: the source for these platforms is
    a hot word with **no body**, so a true plagiarism comparison is impossible.
    What is measurable is whether the work is actually a *rewrite*:

    * the generated titles are compared with the source title — a near-identical
      title means the title was not rewritten (§18's "只修改标题" / "复制原文");
    * the Xiaohongshu and Weibo bodies are compared with each other — near-identical
      bodies mean one platform format was pasted into the other, which §17.4/17.5
      require to differ;
    * each required body must clear a minimum length, so a stub cannot pass.

    Once section 十六 supplies real bodies, the same function gains a genuine
    text-overlap comparison.
    """
    check = CopyCheck()

    titles = [result.xiaohongshu.title, result.weibo.title]
    if source_title:
        source_key = normalize_title(source_title)
        check.title_similarity = max(
            (title_similarity(normalize_title(title), source_key) for title in titles if title),
            default=0.0,
        )
    if check.title_similarity >= title_similarity_limit:
        check.flags.append("title_not_rewritten")

    bodies = [result.xiaohongshu.content, result.weibo.content]
    if all(bodies):
        check.cross_platform_similarity = SequenceMatcher(
            None, normalize_title(bodies[0]), normalize_title(bodies[1])
        ).ratio()
        if check.cross_platform_similarity >= cross_platform_limit:
            check.flags.append("platform_versions_identical")

    for name, minimum in MIN_LENGTHS.items():
        value = {
            "xiaohongshu_content": result.xiaohongshu.content,
            "weibo_content": result.weibo.content,
            "douyin_script": result.douyin.script,
        }[name]
        if len((value or "").strip()) < minimum:
            check.flags.append(f"too_short:{name}")

    return check


def compute_status(
    result: RewriteResult,
    check: CopyCheck,
    *,
    analysis_needs_verification: bool,
) -> tuple[RewriteStatus, list[str]]:
    """Decide publication readiness (§20). Code decides; the model does not.

    A rewrite stays ``NEEDS_REVIEW`` when the model reported a risk, when it said a
    human must verify the facts, when the §18 check failed, or when **its Phase 3
    analysis** needed verification — because a human has not confirmed the facts
    that the rewrite is built on.
    """
    flags = list(dict.fromkeys([*result.risk_flags, *check.flags]))
    unknown = [flag for flag in flags if flag not in RISK_CATEGORIES and ":" not in flag]
    if unknown:
        # Keep the provider's own wording; it is evidence, not noise.
        logger.info("rewrite reported risk flags: %s", unknown)
    must_review = bool(flags) or result.needs_verification or analysis_needs_verification
    return (
        RewriteStatus.NEEDS_REVIEW if must_review else RewriteStatus.READY_TO_PUBLISH,
        flags,
    )


def build_rewrite_prompt(
    profile: AccountProfile,
    source: Any,
    analysis: Any,
    *,
    detail_text: str | None = None,
) -> str:
    """The user message for one rewrite."""
    source_block = json.dumps(describe_item(0, source), ensure_ascii=False, indent=2)
    analysis_block = json.dumps(
        {
            "topic": getattr(analysis, "topic", ""),
            "summary": getattr(analysis, "summary", ""),
            "why_hot": getattr(analysis, "why_hot", ""),
            "content_angle": getattr(analysis, "content_angle", ""),
            "discussion_points": getattr(analysis, "discussion_points", []),
            "needs_verification": getattr(analysis, "needs_verification", True),
        },
        ensure_ascii=False,
        indent=2,
    )
    if detail_text:
        material = f"下面是抓取到的详细内容（可信度未知，需按 §19 处理）：\n{detail_text[:4000]}"
    else:
        material = (
            "注意：本次**没有抓到正文**，你只有标题与热度。因此不要编造任何具体事实"
            "（不要写具体数字、时间、人名、机构名、政策条文），只做框架与角度层面的创作，"
            "并把 needs_verification 设为 true。"
        )

    media_block, image_rules = _media_plan_block(source)
    if media_block:
        # Only ask for the plan when there is something to plan around; asking for an
        # image layout with no images invites the model to invent them.
        image_plan_field = (
            ',\n  "image_plan": {"cover_index": 0, '
            '"cover_text": "封面大字（不超过12字）", '
            '"caption_strategy": "整体配图思路，不超过60字", '
            '"slots": [{"index": 0, "role": "这张图承担的作用", '
            '"overlay": "图上文字（可空）", "caption": "配文一句话"}], '
            '"note": "给运营的排版提醒"}'
        )
        image_plan_rules = (
            "\n7. **image_plan 必填**：该条目有图片素材，必须给出 image_plan，"
            "slots 要覆盖每一张图，index 与素材索引一一对应，cover_index 指向你选的封面。"
        )
    else:
        image_plan_field = ""
        image_plan_rules = ""

    return f"""{profile.to_prompt_block()}

【原始热点】
{source_block}

【AI 分析结论】
{analysis_block}

{material}
{media_block}
请完成二次创作，并且只输出下面这个 JSON 结构（不要任何其他文字）：
{{
  "summary": "发生了什么，不超过80字",
  "why_hot": "为什么受到关注，不超过100字",
  "angle": "你的二创方向，不超过80字",
  "xiaohongshu": {{"title": "标题（不超过20字，要有吸引力）", "content": "正文（200-400字，分段）", "ending": "结尾互动引导", "hashtags": ["#话题1#", "#话题2#"]}},
  "weibo": {{"opening": "开头一句话", "title": "标题（可空）", "content": "正文（100-200字）", "hashtags": ["#话题#"]}},
  "douyin": {{"hook": "前3秒钩子", "script": "30秒口播脚本（150-250字）", "scenes": ["画面建议1", "画面建议2"], "subtitles": "字幕要点", "cta": "结尾行动号召"}},
  "risk_flags": [],
  "needs_verification": true,
  "verification_note": "区分'原内容声称'与'目前可确认'"{image_plan_field}
}}
{image_plan_rules}
硬性规则：
1. **禁止机械改写**：不许复制原文、不许只替换同义词、不许只改标题。要先理解、再换角度重新组织语言。
2. **三个平台必须是不同的表达**，不是同一段话复制三次：小红书口语化分段、微博短句直接、抖音是口播脚本。
3. **事实核验**：涉及具体人物、数字、新闻事件、政策、医疗、金融、科技参数、社会事件而无法确认时，
   needs_verification 必须为 true，并在 verification_note 中区分"原内容声称"与"目前可确认"。
4. **风险控制**：如果内容存在以下问题，请把它们写进 risk_flags（没有就给空数组）：
   {'、'.join(RISK_CATEGORIES)}。若存在任何一项，绝对不要把它写成确定的正面结论。
5. 遵守上面账号定位里列出的禁止项。{image_rules}"""


def _media_plan_block(source: Any) -> tuple[str, str]:
    """Describe the available media and the rules for planning around it.

    The item may be a stored row (``media`` is a JSON dict) or a domain object. Both
    are handled because the rewriter is called with either during tests.
    """
    raw = getattr(source, "media", None)
    if hasattr(raw, "model_dump"):
        media = raw.model_dump(mode="json")
    elif isinstance(raw, dict):
        media = raw
    else:
        media = {}
    images = [image for image in (media.get("images") or []) if isinstance(image, dict)]
    video = media.get("video") if isinstance(media.get("video"), dict) else None
    if not images:
        return "", ""

    shapes = []
    for position, image in enumerate(images):
        width, height = image.get("width"), image.get("height")
        if width and height:
            ratio = "竖图" if height > width else ("横图" if width > height else "方图")
            shapes.append(f"第{position}张 {width}x{height}（{ratio}）")
        else:
            shapes.append(f"第{position}张（尺寸未知）")
    block = f"""
【可用图片素材】共 {len(images)} 张（索引从 0 开始）：
{chr(10).join('  ' + item for item in shapes)}"""
    if video and video.get("url"):
        block += "\n该条目还包含一个视频（本轮不处理视频，只做图文排版）。"

    rules = (
        "\n6. **图文排版**：你**看不到图片内容**（你是纯文本模型），"
        "所以只做结构安排：选哪张当封面、图上写什么字、每段配第几张。"
        "**严禁描述图片里有什么**（不要写'图中显示了…'），也不要写你没看到的东西。"
    )
    return block, rules


async def rewrite_one(
    client: DeepSeekClient,
    source: Any,
    analysis: Any,
    *,
    profile: AccountProfile,
    settings: Settings,
    detail_text: str | None = None,
) -> tuple[RewriteResult, CopyCheck, int, list[str]]:
    """Rewrite one item, with the §18 retry.

    :returns: ``(result, copy_check, attempts, problems)``
    """
    problems: list[str] = []
    prompt = build_rewrite_prompt(profile, source, analysis, detail_text=detail_text)
    attempts = 0
    #: Everything spent on this row, including an attempt that gets thrown away.
    spent = {"prompt": 0, "completion": 0}

    async def _attempt(extra: str = "") -> RewriteResult:
        nonlocal attempts
        attempts += 1
        payload, completion = await client.complete_json(
            system=REWRITE_SYSTEM_PROMPT,
            user=prompt + extra,
            temperature=settings.rewrite_temperature,
            max_tokens=settings.rewrite_max_tokens,
        )
        # Count the call before inspecting it: a truncated response was still paid for.
        spent["prompt"] += completion.usage.prompt_tokens
        spent["completion"] += completion.usage.completion_tokens
        if completion.finish_reason == "length":
            # A real run hit this: the JSON was cut off mid-structure, and the
            # resulting error looked like a parsing bug instead of what it was.
            raise DeepSeekJSONError(
                "output was truncated at REWRITE_MAX_TOKENS="
                f"{settings.rewrite_max_tokens} (finish_reason=length); raise the limit — "
                "retrying with the same ceiling would truncate again"
            )
        if not isinstance(payload, dict):
            raise DeepSeekJSONError("rewrite response was not a JSON object")
        return RewriteResult.model_validate(payload)

    result = await _attempt()
    check = check_mechanical_copy(
        result,
        getattr(source, "title", "") or "",
        title_similarity_limit=0.95,
    )
    if check.mechanical and settings.rewrite_retry_on_copy:
        logger.warning(
            "rewrite for %s flagged %s; retrying once with stricter instructions",
            getattr(source, "id", "?"),
            check.flags,
        )
        problems.extend(f"first attempt: {flag}" for flag in check.flags)
        strict = (
            "\n\n上一次的回答存在这些问题："
            + "、".join(check.flags)
            + "。请完全重写：换一个切入角度、换一套表达和句式，"
            "并确保三个平台的文本明显不同；不要复用上一次的任何句子。"
        )
        try:
            retry_result = await _attempt(strict)
        except DeepSeekJSONError as exc:
            problems.append(f"retry was unparseable: {exc}")
        else:
            retry_check = check_mechanical_copy(
                retry_result,
                getattr(source, "title", "") or "",
                title_similarity_limit=0.95,
            )
            if len(retry_check.flags) <= len(check.flags):
                result, check = retry_result, retry_check
            problems.extend(f"retry: {flag}" for flag in retry_check.flags)

    # The row's cost is everything spent on it, whichever attempt was kept. Taking
    # only the kept attempt's usage would understate a discarded §18 retry.
    result.prompt_tokens = spent["prompt"]
    result.completion_tokens = spent["completion"]
    return result, check, attempts, problems


@dataclass
class RewriteRunResult:
    """Everything one rewriting pass did."""

    considered: int = 0
    rewritten: int = 0
    failed: int = 0
    ready_to_publish: int = 0
    needs_review: int = 0
    mechanical_retries: int = 0
    stored: dict[str, int] = field(default_factory=dict)
    #: Content ids rewritten in this run — what the notification stage reports on.
    rewritten_ids: list[int] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    estimated_cny: float = 0.0
    model: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "rewritten": self.rewritten,
            "failed": self.failed,
            "ready_to_publish": self.ready_to_publish,
            "needs_review": self.needs_review,
            "mechanical_retries": self.mechanical_retries,
            "stored": self.stored,
            "rewritten_ids": list(self.rewritten_ids),
            "tokens": self.tokens,
            "estimated_cny": round(self.estimated_cny, 4),
            "model": self.model,
            "errors": self.errors,
        }


async def run_rewriting(
    *,
    settings: Settings | None = None,
    session: AsyncSession | None = None,
    client: DeepSeekClient | None = None,
    profile: AccountProfile | None = None,
    max_items: int | None = None,
    only_ids: Sequence[int] | None = None,
) -> RewriteRunResult:
    """Rewrite everything Phase 3 selected. **Spends DeepSeek tokens.**

    ``only_ids`` rewrites exactly those items (the per-item "二创" button), ignoring the
    "must be selected" rule that bounds a batch run. An analysis is still required —
    the prompt is built from it — so an item without one does not come back.
    """
    resolved = settings or get_settings()
    if session is not None:
        return await _run(resolved, session, client, profile, max_items, only_ids)
    async with session_scope(resolved) as own_session:
        return await _run(resolved, own_session, client, profile, max_items, only_ids)


async def _run(
    settings: Settings,
    session: AsyncSession,
    client: DeepSeekClient | None,
    profile: AccountProfile | None,
    max_items: int | None,
    only_ids: Sequence[int] | None = None,
) -> RewriteRunResult:
    from app.services.ai.account_profile import load_account_profile

    result = RewriteRunResult()
    if not settings.deepseek_configured:
        result.errors.append("DEEPSEEK_API_KEY or DEEPSEEK_MODEL is not configured")
        return result

    resolved_profile = profile or load_account_profile(settings.account_profile_file)
    limit = max_items or settings.rewrite_max_items or settings.analysis_max_selected
    rows = await rows_needing_rewrite(
        session, limit=limit, reuse_hours=settings.rewrite_reuse_hours, only_ids=only_ids
    )
    result.considered = len(rows)
    if not rows:
        message = (
            "this item has no analysis yet; run AI 分析 first"
            if only_ids
            else "every selected item already has a rewrite"
        )
        logger.info("rewriting: nothing to do (%s)", message)
        if only_ids:
            result.errors.append(message)
        return result

    owns_client = client is None
    active_client = client or DeepSeekClient(settings)
    result.model = active_client.model
    entries: list[tuple[Any, RewriteResult, RewriteStatus, list[str], CopyCheck, int]] = []
    try:
        for row, analysis in rows:
            source = row
            # A fetched body (section 十六) lives in ``description``; when the
            # detail stage has not run, this is empty and the prompt forbids
            # inventing specifics.
            detail_text = (getattr(row, "description", "") or "").strip() or None
            try:
                rewrite, check, attempts, problems = await rewrite_one(
                    active_client,
                    source,
                    analysis,
                    profile=resolved_profile,
                    settings=settings,
                    detail_text=detail_text,
                )
            except DeepSeekJSONError as exc:
                result.failed += 1
                result.errors.append(f"{getattr(row, 'id', '?')}: unparseable JSON ({exc})")
                logger.error("rewrite failed for %s: %s", getattr(row, "id", "?"), exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one item must not stop the run
                result.failed += 1
                result.errors.append(f"{getattr(row, 'id', '?')}: {exc}")
                logger.error("rewrite failed for %s: %s", getattr(row, "id", "?"), exc)
                continue

            status, flags = compute_status(
                rewrite,
                check,
                analysis_needs_verification=bool(getattr(analysis, "needs_verification", False)),
            )
            if bool(getattr(analysis, "needs_verification", False)) and not rewrite.needs_verification:
                # The stored flag means "a human must verify this before publishing",
                # so the analysis's uncertainty is folded in here. Leaving the
                # model's own "false" would contradict the status column and make
                # the Phase 6 notification claim the content is verified.
                rewrite.needs_verification = True
                if not rewrite.verification_note:
                    rewrite.verification_note = (
                        "分析阶段已标记需人工核实：发布前请确认原文来源与关键事实。"
                    )
            if attempts > 1:
                result.mechanical_retries += 1
            result.errors.extend(
                f"{getattr(row, 'id', '?')}: {problem}" for problem in problems
            )
            entries.append((row, rewrite, status, flags, check, attempts))
            result.rewritten += 1
            result.rewritten_ids.append(row.id)
            if status is RewriteStatus.READY_TO_PUBLISH:
                result.ready_to_publish += 1
            else:
                result.needs_review += 1

        stats: RewriteUpsertStats = await upsert_rewrites(
            session, entries, model=active_client.model
        )
        result.stored = stats.as_dict()
        usage = active_client.total_usage
        result.tokens = usage.as_dict()
        result.estimated_cny = usage.estimated_cny()
    finally:
        if owns_client:
            await active_client.aclose()

    logger.info("rewrite run complete: %s", result.as_dict())
    return result

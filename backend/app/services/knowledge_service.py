"""知识科普模块的编排（Phase 13）。

三个入口，计费方式不同、失败互不牵连：

| 入口 | 付费 | 说明 |
|---|---|---|
| :func:`ensure_tag_vocabulary` | 否（已存库时） | 读标签词表。**打开页面不花钱。** |
| :func:`refresh_tag_vocabulary` | 1 次 DeepSeek | 重新生成 30-50 个标签（运营方点的「刷新标签」） |
| :func:`generate_article` | 1-2 次 DeepSeek + 可选 1 次搜图 | 文章 + 三平台文案（+ 配图） |

标签**存库、免费复用**是运营方明确的选择：打开标签球不应该产生任何费用，只有显式刷新
或点击某个标签生成文案时才花钱。

每一步的 token 与失败都记进 ``steps`` 字段，所以一次生成花了多少、哪一步失败，事后可查，
不需要从总账反推。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.models.knowledge import KnowledgeArticleRecord, KnowledgeTagRecord
from app.services.ai.knowledge import (
    ARTICLE_SYSTEM_PROMPT,
    PLATFORM_SYSTEM_PROMPT,
    TAG_SYSTEM_PROMPT,
    ArticleDraft,
    TagDraft,
    build_article_prompt,
    build_platform_prompt,
    parse_article,
    parse_platforms,
    parse_tags,
)

logger = logging.getLogger(__name__)

#: 标签数下限：低于这个数说明模型没按提示词输出，值得记一条警告。
MIN_TAGS = 20

#: 进度回调：``(step, status, detail)``。用于把长任务的分步进展推给界面。
#: ``status`` 取 ``started`` / ``done`` / ``failed``。
ProgressCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]

#: 分步标签，界面直接显示这些中文名。
STEP_LABELS = {
    "article": "生成科普文章",
    "platforms": "改写小红书/微博/抖音",
    "images": "搜图并下载配图",
    "save": "保存",
}


async def _emit(
    on_progress: ProgressCallback | None, step: str, status: str, **detail: Any
) -> None:
    """报告一步进展。

    **回调失败绝不影响主流程**：进度只是给人看的，不能因为它出错就让一次已经付费的生成
    失败。所以这里吞掉异常并只记 debug 日志。
    """
    if on_progress is None:
        return
    try:
        await on_progress(step, status, detail)
    except Exception:  # noqa: BLE001 - 进度是旁路，不影响业务
        logger.debug("progress callback failed for %s/%s", step, status, exc_info=True)


@dataclass
class TagVocabulary:
    """标签词表（球体的数据源）。"""

    tags: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    created: int = 0
    updated: int = 0
    generated: bool = False
    warnings: list[str] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    estimated_cny: float = 0.0
    model: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tags": self.tags,
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "generated": self.generated,
            "warnings": self.warnings,
            "tokens": self.tokens,
            "estimated_cny": self.estimated_cny,
            "model": self.model,
            "error": self.error or None,
        }


@dataclass
class ArticleResult:
    """一次文章生成的结果。"""

    ok: bool = False
    record_id: int | None = None
    article: dict[str, Any] = field(default_factory=dict)
    images: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    steps: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    estimated_cny: float = 0.0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "record_id": self.record_id,
            "article": self.article,
            "images": self.images,
            "tags": self.tags,
            "steps": self.steps,
            "tokens": self.tokens,
            "estimated_cny": self.estimated_cny,
            "error": self.error or None,
        }


def tag_to_dict(record: KnowledgeTagRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "kind": record.kind,
        "difficulty": record.difficulty,
        "blurb": record.blurb,
        "weight": record.weight,
        "article_count": record.article_count,
        "source": record.source,
    }


def article_to_dict(record: KnowledgeArticleRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "topic": record.topic,
        "tag_id": record.tag_id,
        "title": record.title,
        "hook": record.hook,
        "audience": record.audience,
        "difficulty": record.difficulty,
        "sections": record.sections or [],
        "glossary": record.glossary or [],
        "takeaways": record.takeaways or [],
        "further_reading": record.further_reading or [],
        "platforms": record.platforms or {},
        "images": record.images or [],
        "tags": record.tags or [],
        "model": record.model,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "estimated_cny": record.estimated_cny,
        "steps": record.steps or {},
        "error": record.error,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


# --------------------------------------------------------------- 标签词表
async def list_tags(
    settings: Settings | None = None,
    *,
    kind: str | None = None,
    limit: int = 200,
) -> list[KnowledgeTagRecord]:
    """读标签词表。**免费。**"""
    resolved = settings or get_settings()
    async with session_scope(resolved) as session:
        statement = select(KnowledgeTagRecord)
        if kind:
            statement = statement.where(KnowledgeTagRecord.kind == kind)
        rows = (
            await session.execute(
                statement.order_by(
                    KnowledgeTagRecord.weight.desc(), KnowledgeTagRecord.name.asc()
                ).limit(limit)
            )
        ).scalars().all()
    return list(rows)


async def ensure_tag_vocabulary(
    settings: Settings | None = None, *, minimum: int = MIN_TAGS
) -> TagVocabulary:
    """返回词表；**只在完全为空时生成一次**，否则一分钱不花。

    这里有个刻意的取舍：只要库里**有任何**标签就直接复用，哪怕数量少于 ``minimum``。
    早期版本写成"少于 20 个就重新生成"，结果是——只要模型某次输出偏少，**每次打开页面
    都会再调一次模型**，变成一笔静默的、反复发生的费用。这与"先免费复用、手动才重新生成"
    的约定直接冲突。

    所以数量不足时**只用 ``warnings`` 告诉运营方该点刷新**，绝不自己花钱。
    """
    resolved = settings or get_settings()
    existing = await list_tags(resolved)
    if existing:
        vocabulary = TagVocabulary(
            tags=[tag_to_dict(tag) for tag in existing],
            total=len(existing),
            generated=False,
        )
        if len(existing) < minimum:
            vocabulary.warnings.append(
                f"词表只有 {len(existing)} 个标签（建议 {minimum} 个以上）。"
                "可以点「刷新标签」重新生成——那会消耗一次 DeepSeek 调用，"
                "所以不会自动触发。"
            )
        return vocabulary

    logger.info("tag vocabulary is empty, generating once")
    return await refresh_tag_vocabulary(resolved)


async def refresh_tag_vocabulary(
    settings: Settings | None = None, *, extra: str = ""
) -> TagVocabulary:
    """**重新生成标签词表 —— 消耗 1 次 DeepSeek 调用。**"""
    from app.services.ai.deepseek import DeepSeekClient, DeepSeekJSONError, DeepSeekTruncatedError

    resolved = settings or get_settings()
    result = TagVocabulary(generated=True)
    if not resolved.deepseek_configured:
        result.error = "DEEPSEEK_API_KEY 或 DEEPSEEK_MODEL 未配置"
        return result

    client = DeepSeekClient(resolved)
    result.model = client.model
    user = "请生成计算机大类学生的知识标签词表。"
    if extra.strip():
        user += f"\n额外要求：{extra.strip()}"
    try:
        payload, completion = await client.complete_json(
            system=TAG_SYSTEM_PROMPT,
            user=user,
            temperature=0.8,
            max_tokens=resolved.rewrite_max_tokens,
        )
        result.tokens = completion.usage.as_dict()
        result.estimated_cny = completion.usage.estimated_cny()
    except Exception as exc:  # noqa: BLE001 - 供应商异常一律如实返回
        result.error = f"标签生成失败：{type(exc).__name__}: {exc}"
        return result
    finally:
        await client.aclose()

    drafts: list[TagDraft] = parse_tags(payload)
    if len(drafts) < MIN_TAGS:
        result.warnings.append(
            f"模型只给出 {len(drafts)} 个标签，少于预期 {MIN_TAGS} 个；"
            "可能被输出长度截断，可重试或降低要求"
        )
    if not drafts:
        result.error = "模型没有返回任何标签"
        return result

    async with session_scope(resolved) as session:
        for draft in drafts:
            existing = (
                await session.execute(
                    select(KnowledgeTagRecord).where(
                        func.lower(KnowledgeTagRecord.name) == draft.name.casefold()
                    )
                )
            ).scalars().first()
            if existing is None:
                session.add(
                    KnowledgeTagRecord(
                        name=draft.name,
                        kind=draft.kind,
                        difficulty=draft.difficulty,
                        blurb=draft.blurb,
                        weight=draft.weight,
                        source="model",
                    )
                )
                result.created += 1
            else:
                # 保留人工调整过的权重与分类，只补齐空缺的字段。
                existing.kind = existing.kind or draft.kind
                existing.difficulty = existing.difficulty or draft.difficulty
                existing.blurb = existing.blurb or draft.blurb
                if existing.source != "manual":
                    existing.weight = draft.weight
                result.updated += 1

    rows = await list_tags(resolved)
    result.tags = [tag_to_dict(tag) for tag in rows]
    result.total = len(rows)
    logger.warning(
        "tag vocabulary refreshed: +%d new, %d updated, %d total (¥%.4f)",
        result.created,
        result.updated,
        result.total,
        result.estimated_cny,
    )
    return result


# --------------------------------------------------------------- 文章生成
async def generate_article(
    topic: str,
    *,
    settings: Settings | None = None,
    extra: str = "",
    with_platforms: bool = True,
    with_images: bool = True,
    image_count: int = 6,
    search_keyword: str = "",
    tag_id: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> ArticleResult:
    """为一篇科普文章生成正文、三平台文案、配图与标签。

    **计费**：文章 1 次 DeepSeek；三平台文案 1 次 DeepSeek；配图默认 1 次搜图
    （$0.0078，比生图便宜 4 倍且一次拿多张）。

    失败分步记录：文章生成失败就直接返回（后面两步没有意义）；三平台或搜图失败只影响
    自己那一步，文章本身仍然保存——**已经付过钱的部分不该被后面的失败作废**。
    """
    from app.services.ai.deepseek import DeepSeekClient, DeepSeekJSONError, DeepSeekTruncatedError

    resolved = settings or get_settings()
    result = ArticleResult()
    topic = topic.strip()
    if not topic:
        result.error = "主题不能为空"
        return result
    if not resolved.deepseek_configured:
        result.error = "DEEPSEEK_API_KEY 或 DEEPSEEK_MODEL 未配置"
        return result

    client = DeepSeekClient(resolved)
    total_cny = 0.0
    total_tokens: dict[str, int] = {}

    def _accumulate(usage: Any) -> dict[str, int]:
        nonlocal total_cny
        tokens = usage.as_dict()
        for key, value in tokens.items():
            if isinstance(value, int):
                total_tokens[key] = total_tokens.get(key, 0) + value
        total_cny += usage.estimated_cny()
        return tokens

    # ---- 第一步：文章本体 ----
    await _emit(on_progress, "article", "started", note="这一步最长，通常 15-30 秒")

    async def _article_call(*, compact: bool, strict_json: bool = False) -> tuple[Any, Any]:
        prompt = build_article_prompt(topic, extra=extra, compact=compact)
        if strict_json:
            # 第二次要求更严格的 JSON。中文长文本最常见两种坏法：字符串里出现真实换行、
            # 或未转义的双引号。
            prompt += (
                "\n\n**上一版 JSON 不合法导致无法解析。这次请特别注意**："
                "字符串内部不要出现真实换行（需要分段写 \\n），"
                "不要出现未转义的双引号（要强调用「」）。"
            )
        return await client.complete_json(
            system=ARTICLE_SYSTEM_PROMPT,
            user=prompt,
            temperature=0.7,
            max_tokens=resolved.article_max_tokens,
        )

    try:
        payload, completion = await _article_call(compact=False)
    except DeepSeekTruncatedError as first:
        # **截断是可恢复的**：同样的提示词再来一次大概率还是会被截断，所以第二次明确
        # 要求更短的篇幅。不这样做的话，这一次已经付费的调用就白费了。
        logger.warning(
            "article for %r was truncated at %d tokens — retrying with a shorter ask",
            topic,
            first.completion_tokens,
        )
        await _emit(
            on_progress,
            "article",
            "started",
            note=f"上次在 {first.completion_tokens} tokens 处被截断，正在用更短的篇幅重试",
        )
        try:
            payload, completion = await _article_call(compact=True)
            result.steps["article_retry"] = {"reason": "truncated", "tokens": first.completion_tokens}
        except DeepSeekTruncatedError as second:
            result.error = (
                f"文章输出两次都被截断（第一次 {first.completion_tokens}、"
                f"第二次 {second.completion_tokens} tokens，上限 ARTICLE_MAX_TOKENS="
                f"{resolved.article_max_tokens}）。请调高该上限，或在「额外要求」里写明"
                "「只写 3 节、每节 150 字以内」。"
            )
            result.steps["article"] = {"ok": False, "error": result.error, "truncated": True}
            await _emit(on_progress, "article", "failed", error="两次都被截断")
            await client.aclose()
            return result
        except DeepSeekJSONError as exc:
            result.error = f"重试后仍不是合法 JSON：{exc}"
            result.steps["article"] = {"ok": False, "error": result.error}
            await _emit(on_progress, "article", "failed", error="重试后 JSON 仍不合法")
            await client.aclose()
            return result
    except DeepSeekJSONError as first:
        # **JSON 不合法也值得重试一次**：模型是随机采样的（temperature 0.7），实测同一个
        # 主题重来一次就能正常解析。第一版判断"语法错误重试没意义"是错的——那是把它当成
        # 确定性问题了。
        logger.warning("article for %r had invalid JSON — retrying once: %s", topic, first)
        await _emit(
            on_progress,
            "article",
            "started",
            note="上次输出不是合法 JSON，正在重试并要求更严格的格式",
        )
        try:
            payload, completion = await _article_call(compact=False, strict_json=True)
            result.steps["article_retry"] = {"reason": "invalid_json"}
        except (DeepSeekJSONError, DeepSeekTruncatedError) as second:
            result.error = (
                "文章输出两次都不是合法 JSON。"
                f"第一次：{first}；第二次：{second}"
            )
            result.steps["article"] = {"ok": False, "error": result.error}
            await _emit(on_progress, "article", "failed", error="两次 JSON 都不合法")
            await client.aclose()
            return result
    except Exception as exc:  # noqa: BLE001
        result.error = f"文章生成失败：{type(exc).__name__}: {exc}"
        result.steps["article"] = {"ok": False, "error": result.error}
        await _emit(on_progress, "article", "failed", error=result.error)
        await client.aclose()
        return result

    draft: ArticleDraft = parse_article(payload)
    result.steps["article"] = {
        "ok": True,
        "tokens": _accumulate(completion.usage),
        "truncated": completion.finish_reason == "length",
        "warnings": draft.warnings,
        "sections": len(draft.sections),
    }
    if not draft.title or not draft.sections:
        result.error = (
            "模型返回的文章缺少标题或正文分节，无法保存。"
            f"（{'; '.join(draft.warnings) or '无更多信息'}）"
        )
        await client.aclose()
        return result
    if completion.finish_reason == "length":
        draft.warnings.append(
            f"输出在 REWRITE_MAX_TOKENS={resolved.rewrite_max_tokens} 处被截断，建议提高上限"
        )
    await _emit(
        on_progress,
        "article",
        "done",
        sections=len(draft.sections),
        title=draft.title,
        tokens=result.steps["article"].get("tokens", {}),
    )

    # ---- 第二步：三平台文案（失败不影响文章） ----
    platforms: dict[str, Any] = {}
    if with_platforms:
        await _emit(on_progress, "platforms", "started")
        try:
            platform_payload, platform_completion = await client.complete_json(
                system=PLATFORM_SYSTEM_PROMPT,
                user=build_platform_prompt(draft, topic),
                temperature=0.7,
                # 三平台文案比文章短，但抖音脚本 + 微博 + 小红书三份加起来也不小。
                max_tokens=resolved.article_max_tokens,
            )
            platforms = parse_platforms(platform_payload)
            result.steps["platforms"] = {
                "ok": bool(platforms),
                "tokens": _accumulate(platform_completion.usage),
                "got": sorted(platforms),
            }
            await _emit(on_progress, "platforms", "done", platforms=sorted(platforms))
        except DeepSeekJSONError as exc:
            result.steps["platforms"] = {
                "ok": False,
                "error": f"三平台文案输出不完整（可能被截断）：{exc}",
            }
            await _emit(on_progress, "platforms", "failed", error="输出不完整（可能被截断）")
            logger.warning("platform rendering produced incomplete JSON for %r", topic)
        except Exception as exc:  # noqa: BLE001
            result.steps["platforms"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            await _emit(on_progress, "platforms", "failed", error=f"{type(exc).__name__}: {exc}")
            logger.warning("platform rendering failed for %r: %s", topic, exc)

    images: list[dict[str, Any]] = []
    if with_images:
        await _emit(on_progress, "images", "started", keyword=(search_keyword or topic))
        try:
            from app.services.tikhub.image_search import search_and_download

            keyword = (search_keyword or topic).strip()
            found, summary = await search_and_download(
                keyword, limit=image_count, settings=resolved
            )
            images = [image.as_dict() for image in found if image.local_path]
            result.steps["images"] = {"ok": bool(images), "keyword": keyword, **summary}
            await _emit(
                on_progress,
                "images",
                "done" if images else "failed",
                downloaded=len(images),
                error="" if images else summary.get("errors", ["没有搜到可用图片"])[:1],
            )
        except Exception as exc:  # noqa: BLE001
            result.steps["images"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            await _emit(on_progress, "images", "failed", error=f"{type(exc).__name__}: {exc}")
            logger.warning("image search failed for %r: %s", topic, exc)

    await client.aclose()

    # ---- 文章自带的知识标签（从正文与术语表里取，不额外调模型） ----
    tags = _derive_tags(draft, topic)
    result.tags = tags

    record = KnowledgeArticleRecord(        topic=topic,
        tag_id=tag_id,
        title=draft.title,
        hook=draft.hook,
        audience=draft.audience,
        difficulty=draft.difficulty,
        sections=draft.sections,
        glossary=draft.glossary,
        takeaways=draft.takeaways,
        further_reading=draft.further_reading,
        platforms=platforms,
        images=images,
        tags=tags,
        model=client.model,
        prompt_tokens=total_tokens.get("prompt_tokens", 0),
        completion_tokens=total_tokens.get("completion_tokens", 0),
        estimated_cny=round(total_cny, 4),
        steps=result.steps,
    )
    await _emit(on_progress, "save", "started")
    async with session_scope(resolved) as session:
        session.add(record)
        await session.flush()
        result.record_id = record.id
        # 标签被用过一次：计数 +1，球体上的权重随之上升。
        if tag_id is not None:
            tag = await session.get(KnowledgeTagRecord, tag_id)
            if tag is not None:
                tag.article_count += 1
                tag.weight = min(10.0, (tag.weight or 1.0) + 0.2)
        stored = article_to_dict(record)

    result.ok = True
    result.article = stored
    result.images = images
    result.tokens = total_tokens
    result.estimated_cny = round(total_cny, 4)
    await _emit(on_progress, "save", "done", article_id=result.record_id)
    logger.warning(
        "knowledge article for %r: id=%s sections=%d platforms=%s images=%d ¥%.4f",
        topic,
        result.record_id,
        len(draft.sections),
        sorted(platforms),
        len(images),
        result.estimated_cny,
    )
    return result


def _derive_tags(draft: ArticleDraft, topic: str) -> list[str]:
    """文章自带的知识标签：主题 + 术语表 + 分节小标题里的关键概念。

    **不额外调模型**——术语表和分节标题本来就是文章的结构，再从里面抽标签是免费的，
    而单独为每篇文章跑一次标签抽取会让点击一次标签的成本翻倍。
    """
    tags: list[str] = [topic]
    for entry in draft.glossary:
        term = str(entry.get("term") or "").strip()
        if term and term not in tags:
            tags.append(term)
    for section in draft.sections:
        heading = str(section.get("heading") or "").strip()
        # 小标题常带修饰语（"为什么需要它"），过长的不适合当标签。
        if heading and len(heading) <= 16 and heading not in tags:
            tags.append(heading)
    return tags[:24]


async def list_articles(
    settings: Settings | None = None, *, topic: str | None = None, limit: int = 20, offset: int = 0
) -> tuple[list[KnowledgeArticleRecord], int]:
    """已生成的文章，最新在前。**免费。**"""
    resolved = settings or get_settings()
    async with session_scope(resolved) as session:
        conditions = []
        if topic:
            conditions.append(KnowledgeArticleRecord.topic == topic)
        total = (
            await session.execute(
                select(func.count(KnowledgeArticleRecord.id)).where(*conditions)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(KnowledgeArticleRecord)
                .where(*conditions)
                .order_by(KnowledgeArticleRecord.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    return list(rows), int(total)


async def get_article(
    article_id: int, settings: Settings | None = None
) -> KnowledgeArticleRecord | None:
    resolved = settings or get_settings()
    async with session_scope(resolved) as session:
        return (
            await session.execute(
                select(KnowledgeArticleRecord).where(KnowledgeArticleRecord.id == article_id)
            )
        ).scalars().first()


__all__ = [
    "MIN_TAGS",
    "ArticleResult",
    "TagVocabulary",
    "article_to_dict",
    "ensure_tag_vocabulary",
    "generate_article",
    "get_article",
    "list_articles",
    "list_tags",
    "refresh_tag_vocabulary",
    "tag_to_dict",
]

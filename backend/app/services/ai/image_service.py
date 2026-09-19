"""The image-generation service: build the instruction, generate, save, record.

Sits between the API/UI and :mod:`app.services.ai.image_gen`. Three jobs:

* pick the reference images (the item's downloaded material, preferring the ones the
   layout plan chose as the cover),
* build an instruction that is an **edit**, informed by the rewritten copy so the picture
  matches the post rather than the original,
* download the result immediately and write one audit row.

Every path here is paid, so the service refuses to guess: no reference image, no prompt,
or an unconfigured key each produce a clear error *before* the call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.models.image_generation import ImageGenerationRecord
from app.services.ai.image_gen import (
    QwenImageClient,
    build_edit_prompt,
    download_generated,
)

logger = logging.getLogger(__name__)


@dataclass
class GenerateOutcome:
    """What one generation attempt did."""

    ok: bool = False
    record_id: int | None = None
    local_paths: list[str] = field(default_factory=list)
    result_urls: list[str] = field(default_factory=list)
    reference_paths: list[str] = field(default_factory=list)
    prompt: str = ""
    estimated_usd: float = 0.0
    estimated_cny: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "record_id": self.record_id,
            "local_paths": self.local_paths,
            "result_urls": self.result_urls,
            "reference_paths": self.reference_paths,
            "prompt": self.prompt,
            "estimated_usd": round(self.estimated_usd, 6),
            "estimated_cny": self.estimated_cny,
            "usage": self.usage,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


def _absolute(settings: Settings, relative: str) -> Path:
    """``media/ab/cd.png`` -> an absolute path inside the project."""
    return settings.media_root_path.parent / relative


def pick_references(
    media: dict[str, Any] | None,
    *,
    settings: Settings,
    limit: int = 2,
    layout_cover_index: int | None = None,
) -> list[Path]:
    """Choose which downloaded pictures to send as references.

    Only images **already downloaded** are eligible: their provider URLs have expired, so
    an undownloaded one cannot be sent at all. The layout plan's cover choice comes first
    when it is available, because that is the frame the operator decided should lead.
    """
    images = [image for image in ((media or {}).get("images") or []) if isinstance(image, dict)]
    with_local = [image for image in images if image.get("local_path")]

    ordered: list[dict[str, Any]] = []
    if layout_cover_index is not None:
        for index, image in enumerate(images):
            if index == layout_cover_index and image.get("local_path"):
                ordered.append(image)
                break
    ordered.extend(image for image in with_local if image not in ordered)

    picked: list[Path] = []
    for image in ordered[:limit]:
        path = _absolute(settings, str(image["local_path"]))
        if path.is_file():
            picked.append(path)
        else:
            # The row says there is a local copy but the file is gone (media/ cleaned).
            logger.warning("reference image missing on disk: %s", path)
    return picked


async def generate_for_content(
    hot_content_id: int,
    *,
    settings: Settings | None = None,
    goal: str = "",
    caption: str = "",
    overlay_text: str = "",
    keep_subject: bool = True,
    extra: str = "",
    client: QwenImageClient | None = None,
    store: bool = True,
) -> GenerateOutcome:
    """Generate one image for a stored item, using its downloaded material.

    **This spends money per image.** Nothing calls it automatically.
    """
    resolved = settings or get_settings()
    outcome = GenerateOutcome()

    if not resolved.image_gen_enabled:
        outcome.error = "图片二创已关闭（IMAGE_GEN_ENABLED=false）"
        return outcome
    if not resolved.dashscope_configured:
        outcome.error = "DASHSCOPE_API_KEY 或 DASHSCOPE_BASE_URL 未配置"
        return outcome

    async with session_scope(resolved) as session:
        from app.models.ai_rewrite import AiRewriteRecord
        from app.models.hot_content import HotContentRecord

        row = (
            await session.execute(
                select(HotContentRecord).where(HotContentRecord.id == hot_content_id)
            )
        ).scalars().first()
        if row is None:
            outcome.error = f"找不到条目 {hot_content_id}"
            return outcome
        rewrite = (
            await session.execute(
                select(AiRewriteRecord).where(AiRewriteRecord.hot_content_id == hot_content_id)
            )
        ).scalars().first()
        media = row.media or {}
        title = row.title or ""
        layout_cover = None
        if rewrite is not None and isinstance(rewrite.layout, dict):
            cover = rewrite.layout.get("cover_index")
            layout_cover = int(cover) if isinstance(cover, (int, float)) else None
        rewrite_title = rewrite.xiaohongshu_title if rewrite is not None else ""

    references = pick_references(media, settings=resolved, layout_cover_index=layout_cover)
    if not references:
        outcome.error = (
            "这条没有可用的本地图片素材（需要先搜索采集并下载图片）。"
            "图片模型只能用已下载到素材库的图——平台原链接会过期。"
        )
        return outcome
    outcome.reference_paths = [
        path.relative_to(resolved.media_root_path.parent).as_posix() for path in references
    ]

    resolved_goal = goal.strip() or (
        f"为一条关于「{title[:40]}」的社媒帖子制作配图，"
        "画面要能吸引 18-30 岁的中文读者点开。"
    )
    resolved_caption = caption.strip() or rewrite_title or title
    prompt = build_edit_prompt(
        goal=resolved_goal,
        caption=resolved_caption,
        overlay_text=overlay_text,
        keep_subject=keep_subject,
        extra=extra,
    )
    outcome.prompt = prompt

    owns_client = client is None
    active = client or QwenImageClient(resolved)
    try:
        result = await active.edit_image(prompt=prompt, reference_paths=references)
    finally:
        if owns_client:
            await active.aclose()

    outcome.result_urls = result.urls
    outcome.estimated_usd = result.estimated_usd
    outcome.estimated_cny = result.estimated_cny
    outcome.usage = result.usage
    outcome.elapsed_ms = result.elapsed_ms
    if not result.ok:
        outcome.error = result.error
        if store:
            outcome.record_id = await _store(
                resolved, hot_content_id=hot_content_id, outcome=outcome,
                model=resolved.qwen_image_model, status="failed", error=result.error,
            )
        return outcome

    # Download before the 24-hour URL expires; a paid image that is not saved is lost.
    local_paths, errors = await download_generated(result.urls, settings=resolved)
    outcome.local_paths = local_paths
    outcome.ok = bool(local_paths)
    if errors:
        outcome.error = "部分图片未能保存：" + "；".join(errors[:3])
        if not local_paths:
            outcome.ok = False

    if store:
        outcome.record_id = await _store(
            resolved,
            hot_content_id=hot_content_id,
            outcome=outcome,
            model=resolved.qwen_image_model,
            status="success" if outcome.ok else "failed",
            error=outcome.error,
        )
    logger.warning(
        "image generation for item %s: ok=%s ~$%.4f %s",
        hot_content_id,
        outcome.ok,
        outcome.estimated_usd,
        outcome.local_paths,
    )
    return outcome


async def _store(
    settings: Settings,
    *,
    hot_content_id: int | None,
    outcome: GenerateOutcome,
    model: str,
    status: str,
    error: str,
) -> int | None:
    """Write the audit row. A storage failure must not lose the image itself."""
    try:
        async with session_scope(settings) as session:
            record = ImageGenerationRecord(
                hot_content_id=hot_content_id,
                prompt=outcome.prompt,
                reference_paths=list(outcome.reference_paths),
                result_urls=list(outcome.result_urls),
                local_paths=list(outcome.local_paths),
                model=model,
                size=settings.image_gen_size,
                requested_n=settings.image_gen_n,
                usage=outcome.usage,
                estimated_usd=outcome.estimated_usd,
                estimated_cny=outcome.estimated_cny,
                status=status,
                error=error,
                elapsed_ms=outcome.elapsed_ms,
            )
            session.add(record)
            await session.flush()
            return record.id
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not undo a paid result
        logger.error("could not record the image generation: %s", exc)
        return None


async def list_generations(
    settings: Settings | None = None,
    *,
    hot_content_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ImageGenerationRecord], int]:
    """Stored generations, newest first."""
    resolved = settings or get_settings()
    from sqlalchemy import func

    async with session_scope(resolved) as session:
        conditions = []
        if hot_content_id is not None:
            conditions.append(ImageGenerationRecord.hot_content_id == hot_content_id)
        total = (
            await session.execute(
                select(func.count(ImageGenerationRecord.id)).where(*conditions)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(ImageGenerationRecord)
                .where(*conditions)
                .order_by(ImageGenerationRecord.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    return list(rows), int(total)


def generation_to_dict(record: ImageGenerationRecord) -> dict[str, Any]:
    """Serialise one generation for the API and the MCP tool."""
    return {
        "id": record.id,
        "hot_content_id": record.hot_content_id,
        "status": record.status,
        "prompt": record.prompt,
        "reference_paths": record.reference_paths or [],
        "local_paths": record.local_paths or [],
        "result_urls": record.result_urls or [],
        "model": record.model,
        "size": record.size,
        "usage": record.usage or {},
        "estimated_usd": record.estimated_usd,
        "estimated_cny": record.estimated_cny,
        "error": record.error,
        "elapsed_ms": record.elapsed_ms,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


__all__ = [
    "GenerateOutcome",
    "generate_for_content",
    "generation_to_dict",
    "list_generations",
    "pick_references",
]

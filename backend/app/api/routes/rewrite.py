"""``/api/rewrite`` — Phase 4: the three platform versions of a selected topic.

``POST /api/rewrite/run`` **spends DeepSeek tokens**; the reads are free. Every
response carries the source attribution the spec's section 十八 requires
(``source_url`` / ``source_platform`` / ``source_author``), resolved from the
linked ``hot_contents`` row rather than duplicated.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.config import get_settings
from app.db.database import session_scope
from app.db.repository import list_rewrites, rewrite_for_content
from app.models.ai_rewrite import AiRewriteRecord, RewriteStatus
from app.models.hot_content import HotContentRecord
from app.services.ai.account_profile import load_account_profile
from app.services.ai.rewriter import run_rewriting

logger = logging.getLogger(__name__)
router = APIRouter()


def _source_block(item: HotContentRecord | None) -> dict[str, Any] | None:
    """§18's required attribution, plus what a reviewer needs to judge the topic."""
    if item is None:
        return None
    media = item.media if isinstance(item.media, dict) else {}
    images = [entry for entry in (media.get("images") or []) if isinstance(entry, dict)]
    video = media.get("video") if isinstance(media.get("video"), dict) else None
    return {
        "platform": item.platform,
        "title": item.title,
        "url": item.url,
        "author": item.author,
        "author_id": item.author_id,
        "hot_value": item.hot_value,
        "rank": item.rank,
        "platform_content_id": item.platform_content_id,
        # The layout plan refers to pictures by index, so the reviewer needs the same
        # list the plan was written against — hence images here rather than a second call.
        "image_count": item.image_count,
        "images": [
            {
                "url": entry.get("url"),
                "local_path": entry.get("local_path"),
                "width": entry.get("width"),
                "height": entry.get("height"),
            }
            for entry in images
        ],
        "video": (
            {"url": video.get("url"), "cover_url": video.get("cover_url")} if video else None
        ),
    }


def _rewrite_to_dict(rewrite: AiRewriteRecord, item: HotContentRecord | None) -> dict[str, Any]:
    return {
        "hot_content_id": rewrite.hot_content_id,
        "topic_group_id": rewrite.topic_group_id,
        "status": rewrite.status,
        "risk_flags": rewrite.risk_flags or [],
        "needs_verification": rewrite.needs_verification,
        "verification_note": rewrite.verification_note,
        "summary": rewrite.summary,
        "why_hot": rewrite.why_hot,
        "angle": rewrite.angle,
        "xiaohongshu": {
            "title": rewrite.xiaohongshu_title,
            "content": rewrite.xiaohongshu_content,
            "ending": rewrite.xiaohongshu_ending,
            "hashtags": rewrite.xiaohongshu_hashtags or [],
        },
        "weibo": {
            "opening": rewrite.weibo_opening,
            "title": rewrite.weibo_title,
            "content": rewrite.weibo_content,
            "hashtags": rewrite.weibo_hashtags or [],
        },
        "douyin": {
            "hook": rewrite.douyin_hook,
            "script": rewrite.douyin_script,
            "scenes": rewrite.douyin_scene_suggestions or [],
            "subtitles": rewrite.douyin_subtitles,
            "cta": rewrite.douyin_cta,
        },
        "model": rewrite.model,
        "attempts": rewrite.attempts,
        "copy_similarity": rewrite.copy_similarity,
        # The 图文 layout plan, when the item had images (Phase 9).
        "layout": rewrite.layout or {},
        "tokens": {
            "prompt": rewrite.prompt_tokens,
            "completion": rewrite.completion_tokens,
        },
        "created_at": rewrite.created_at.isoformat() if rewrite.created_at else None,
        "updated_at": rewrite.updated_at.isoformat() if rewrite.updated_at else None,
        "source": _source_block(item),
    }


@router.post(
    "/rewrite/run",
    summary="Rewrite the selected hot topics into Xiaohongshu / Weibo / Douyin versions",
    description=(
        "**This endpoint spends DeepSeek tokens** — one request per selected item, "
        "bounded by `ANALYSIS_MAX_SELECTED`. A §18 anti-copy check runs in code; a "
        "flagged attempt is regenerated once, and a rewrite can only be "
        "`READY_TO_PUBLISH` when neither the model nor its Phase 3 analysis asked "
        "for verification. The response reports the exact token usage."
    ),
)
async def run_rewrite_endpoint(
    max_items: int | None = Query(
        default=None, ge=1, le=50, description="Override how many items to rewrite."
    ),
    hot_content_id: int | None = Query(
        default=None,
        description=(
            "只二创**指定的一条**（界面详情抽屉里的「二创」按钮）。"
            "指定时不要求该条已被入选；但它必须已有 AI 分析，否则会明确报错。"
        ),
    ),
) -> dict[str, Any]:
    """Rewrite the selected items and persist the results."""
    settings = get_settings()
    if not settings.deepseek_configured:
        raise HTTPException(
            status_code=503, detail="DEEPSEEK_API_KEY or DEEPSEEK_MODEL is not configured."
        )
    try:
        load_account_profile(settings.account_profile_file)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    only_ids = [hot_content_id] if hot_content_id is not None else None
    logger.warning(
        "POST /api/rewrite/run — spends DeepSeek tokens (scope: %s)",
        f"item {hot_content_id}" if only_ids else "batch",
    )
    result = await run_rewriting(settings=settings, max_items=max_items, only_ids=only_ids)
    if only_ids and result.considered == 0:
        # The rewrite prompt is built from the analysis, so an unanalysed item cannot be
        # rewritten. Say that plainly instead of returning an empty success.
        raise HTTPException(
            status_code=409,
            detail="该条还没有 AI 分析，无法二创。请先点「AI 分析」。",
        )
    async with session_scope(settings) as session:
        if only_ids:
            found = await rewrite_for_content(session, hot_content_id)
            return {
                "success": True,
                "run": result.as_dict(),
                "rewrites": [_rewrite_to_dict(*found)] if found else [],
            }
        rows, _total = await list_rewrites(session, limit=50)
    return {
        "success": True,
        "run": result.as_dict(),
        "rewrites": [_rewrite_to_dict(rewrite, item) for rewrite, item in rows],
    }


@router.get("/rewrite", summary="Stored rewrites (free)")
async def get_rewrites(
    status: str | None = Query(
        default=None,
        description=f"Filter by status: {RewriteStatus.READY_TO_PUBLISH.value} | {RewriteStatus.NEEDS_REVIEW.value}",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Read stored rewrites with their source items."""
    if status and status not in {item.value for item in RewriteStatus}:
        raise HTTPException(
            status_code=422,
            detail=f"unknown status {status!r}; expected {[item.value for item in RewriteStatus]}",
        )
    settings = get_settings()
    async with session_scope(settings) as session:
        rows, total = await list_rewrites(session, status=status, limit=limit, offset=offset)
    return {
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_rewrite_to_dict(rewrite, item) for rewrite, item in rows],
    }


@router.get("/rewrite/{hot_content_id}", summary="One item's rewrite (free)")
async def get_rewrite(hot_content_id: int) -> dict[str, Any]:
    """Read the rewrite of one hot item."""
    settings = get_settings()
    async with session_scope(settings) as session:
        found = await rewrite_for_content(session, hot_content_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no rewrite for hot content {hot_content_id}")
    rewrite, item = found
    return {"success": True, "item": _rewrite_to_dict(rewrite, item)}

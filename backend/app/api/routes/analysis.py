"""``/api/analysis`` — Phase 3: DeepSeek analysis, selection, and topic summaries.

``POST /api/analysis/run`` **spends DeepSeek tokens**; everything else here is a
free read of what is already stored.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.config import get_settings
from app.db.database import session_scope
from app.db.repository import analysis_for_content, list_analyses
from app.models.ai_analysis import AiAnalysisRecord
from app.models.hot_content import HotContentRecord
from app.services.ai.account_profile import load_account_profile
from app.services.ai.analyzer import run_analysis

logger = logging.getLogger(__name__)
router = APIRouter()


def _analysis_to_dict(analysis: AiAnalysisRecord, item: HotContentRecord | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hot_content_id": analysis.hot_content_id,
        "topic_group_id": analysis.topic_group_id,
        "topic": analysis.topic,
        "summary": analysis.summary,
        "why_hot": analysis.why_hot,
        "content_angle": analysis.content_angle,
        "discussion_points": analysis.discussion_points or [],
        "account_fit": analysis.account_fit,
        "recommended": analysis.recommended,
        "confidence": analysis.confidence,
        "needs_verification": analysis.needs_verification,
        "is_duplicate": analysis.is_duplicate,
        "selected": analysis.selected,
        "model": analysis.model,
        "tokens": {
            "prompt": analysis.prompt_tokens,
            "completion": analysis.completion_tokens,
        },
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
        "updated_at": analysis.updated_at.isoformat() if analysis.updated_at else None,
    }
    if item is not None:
        payload["source"] = {
            "platform": item.platform,
            "title": item.title,
            "url": item.url,
            "rank": item.rank,
            "hot_value": item.hot_value,
            "platform_content_id": item.platform_content_id,
        }
    return payload


@router.post(
    "/analysis/run",
    summary="Analyse stored hot content with DeepSeek and select the best items",
    description=(
        "**This endpoint spends DeepSeek tokens.** Cost is bounded by "
        "`ANALYSIS_MAX_CANDIDATES` (items sent), `ANALYSIS_BATCH_SIZE` (items per "
        "request) and `ANALYSIS_REUSE_HOURS` (how long an analysis is reused). "
        "The free rule filter runs first, and the response reports the exact token "
        "usage."
    ),
)
async def run_analysis_endpoint(
    max_candidates: int | None = Query(
        default=None,
        ge=1,
        le=200,
        description="Override ANALYSIS_MAX_CANDIDATES for this run.",
    ),
    hot_content_id: int | None = Query(
        default=None,
        description=(
            "分析**指定的一条**（界面详情抽屉里的「AI 分析」按钮）。"
            "指定时会绕过领域过滤与候选上限——是你点的，就分析它。"
        ),
    ),
) -> dict[str, Any]:
    """Analyse, select, and persist. Billed in tokens, not in TikHub calls."""
    settings = get_settings()
    if not settings.deepseek_configured:
        raise HTTPException(
            status_code=503,
            detail="DEEPSEEK_API_KEY or DEEPSEEK_MODEL is not configured.",
        )
    try:
        load_account_profile(settings.account_profile_file)
    except ValueError as exc:
        # A malformed profile must be visible, not silently ignored.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    only_ids = [hot_content_id] if hot_content_id is not None else None
    logger.warning(
        "POST /api/analysis/run — spends DeepSeek tokens (scope: %s)",
        f"item {hot_content_id}" if only_ids else "batch",
    )
    result = await run_analysis(
        settings=settings, max_candidates=max_candidates, only_ids=only_ids
    )
    async with session_scope(settings) as session:
        if only_ids:
            # Read the item back so the caller sees what it just paid for.
            # ``analysis_for_content`` returns ``(analysis, item)`` — a tuple, which is
            # what the first version of this got wrong (it unpacked the tuple as if it
            # were the analysis and 500'd *after* the tokens had been spent).
            found = await analysis_for_content(session, hot_content_id)
            return {
                "success": True,
                "run": result.as_dict(),
                "item": _analysis_to_dict(*found) if found else None,
                "selected": [],
            }
        stored = await list_analyses(session, selected_only=True, limit=20)
    return {
        "success": True,
        "run": result.as_dict(),
        "selected": [_analysis_to_dict(analysis, item) for analysis, item in stored[0]],
    }


@router.get("/analysis", summary="Stored analyses (free)")
async def get_analyses(
    selected_only: bool = Query(default=False, description="Only the items chosen for rewriting."),
    recommended_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Read stored analyses with their source item."""
    settings = get_settings()
    async with session_scope(settings) as session:
        rows, total = await list_analyses(
            session,
            selected_only=selected_only,
            recommended_only=recommended_only,
            limit=limit,
            offset=offset,
        )
    return {
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_analysis_to_dict(analysis, item) for analysis, item in rows],
    }


@router.get("/analysis/{hot_content_id}", summary="One item's analysis (free)")
async def get_analysis(hot_content_id: int) -> dict[str, Any]:
    """Read the analysis of one hot item."""
    settings = get_settings()
    async with session_scope(settings) as session:
        found = await analysis_for_content(session, hot_content_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no analysis for hot content {hot_content_id}")
    analysis, item = found
    return {"success": True, "item": _analysis_to_dict(analysis, item)}


@router.get("/analysis/profile/current", summary="The account profile in effect (free)")
async def get_profile() -> dict[str, Any]:
    """What the analyser is writing for — the file's content, validated."""
    settings = get_settings()
    try:
        profile = load_account_profile(settings.account_profile_file)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "success": True,
        "path": str(settings.account_profile_file),
        "is_empty": profile.is_empty,
        "profile": profile.model_dump(mode="json"),
        "prompt_block": profile.to_prompt_block(),
    }

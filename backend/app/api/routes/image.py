"""``/api/image`` — 图片二创 (Phase 11).

The most expensive endpoint in the project. Generation is billed **per image**
(1K output $0.03438, 2K $0.068761, plus $0.00275 per input image), so roughly 15x an
entire text rewrite. Everything here is therefore shaped around making the price visible
and the action deliberate:

* ``GET /api/image/estimate`` is free and states the exact price for the configured size,
  so the UI never shows a guessed number;
* ``POST /api/image/generate`` requires an explicit item and is never called by any
  pipeline stage;
* one call is one image unless the caller raises ``n``, which multiplies the bill.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.ai.image_service import (
    generate_for_content,
    generation_to_dict,
    list_generations,
    pick_references,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class GenerateRequest(BaseModel):
    """One image generation request."""

    hot_content_id: int = Field(description="为哪一条热点生成配图")
    goal: str = Field(default="", description="画面要表达什么（留空则按标题自动生成）")
    caption: str = Field(default="", description="这张图要配合的文案（默认用二创标题）")
    overlay_text: str = Field(default="", description="要排在画面上的文字（可空）")
    keep_subject: bool = Field(
        default=True, description="是否保留参考图的主体类型与氛围（false 则只沿用色调质感）"
    )
    extra: str = Field(default="", description="额外要求")
    # NOTE: there is deliberately no ``n`` field. Every image is billed separately
    # ($0.03438 at 1K), so "how many" is a policy decision (IMAGE_GEN_N, locked to 1),
    # not something a request should be able to raise.


@router.get(
    "/image/estimate",
    summary="What one image costs, and whether the inputs are ready (free)",
    description=(
        "Reports the price for the configured size, how many reference images the item "
        "has available, and whether the provider is configured — so the UI can show the "
        "real cost instead of an estimate, and refuse early instead of failing after paying."
    ),
)
async def image_estimate(
    hot_content_id: int | None = Query(default=None, description="检查这一条有没有可用素材"),
) -> dict[str, Any]:
    """Free: reads config and the database only."""
    settings = get_settings()
    available = None
    if hot_content_id is not None:
        from sqlalchemy import select

        from app.db.database import session_scope
        from app.models.hot_content import HotContentRecord

        async with session_scope(settings) as session:
            row = (
                await session.execute(
                    select(HotContentRecord).where(HotContentRecord.id == hot_content_id)
                )
            ).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"找不到条目 {hot_content_id}")
        available = len(pick_references(row.media or {}, settings=settings))

    # The real charge for one generation is the output image **plus every input image**.
    # Quoting only the output price understated a measured call by 16% ($0.03438 quoted,
    # $0.03988 charged with two references), which is exactly the kind of estimate this
    # project keeps getting wrong — so the total is computed here and shown instead.
    input_price = 0.00275
    references = available if available is not None else 0
    total_usd = settings.image_gen_cost_usd_per_image + references * input_price

    return {
        "success": True,
        "configured": settings.dashscope_configured,
        "enabled": settings.image_gen_enabled,
        "model": settings.qwen_image_model,
        "size": settings.image_gen_size,
        "price_usd_per_image": settings.image_gen_cost_usd_per_image,
        "price_cny_per_image": settings.image_gen_cost_cny_per_image,
        "price_usd_per_input_image": input_price,
        "estimated_total_usd": round(total_usd, 5),
        "estimated_total_cny": round(total_usd * 7.3, 4),
        "max_per_run": settings.image_gen_max_per_run,
        "reference_images_available": available,
        "note": (
            "计费按输出图片张数与像素档位，不按 token。"
            f"当前尺寸 {settings.image_gen_size} 属 1K 档：输出 ${settings.image_gen_cost_usd_per_image:.5f}"
            f"（≈¥{settings.image_gen_cost_cny_per_image}），每张输入图另计 ${input_price}。"
            + (
                f"本条会用 {references} 张参考图，预计合计 ${total_usd:.5f}（≈¥{round(total_usd * 7.3, 4)}）。"
                if available is not None
                else ""
            )
        ),
    }


@router.post(
    "/image/generate",
    summary="Generate one image for an item from its material (billed per image)",
    description=(
        "**Billed per generated image**, roughly 15x a text rewrite. Uses the item's "
        "already-downloaded pictures as references (provider URLs expire, and the "
        "provider cannot reach this machine). The result is downloaded immediately into "
        "`media/generated/` because the provider's URL lives only 24 hours."
    ),
)
async def generate_image(payload: GenerateRequest) -> dict[str, Any]:
    """Generate one image and record what it cost."""
    settings = get_settings()
    if not settings.image_gen_enabled:
        raise HTTPException(
            status_code=503, detail="图片二创已关闭：请设置 IMAGE_GEN_ENABLED=true"
        )
    if not settings.dashscope_configured:
        raise HTTPException(
            status_code=503,
            detail="DASHSCOPE_API_KEY 或 DASHSCOPE_BASE_URL 未配置，无法生成图片",
        )

    logger.warning(
        "POST /api/image/generate item=%s n=1 size=%s — billed about $%.4f",
        payload.hot_content_id,
        settings.image_gen_size,
        settings.image_gen_cost_usd_per_image,
    )

    outcome = await generate_for_content(
        payload.hot_content_id,
        settings=settings,
        goal=payload.goal,
        caption=payload.caption,
        overlay_text=payload.overlay_text,
        keep_subject=payload.keep_subject,
        extra=payload.extra,
    )
    if not outcome.ok:
        # 422 for "your input cannot work" (no material, bad config), 502 for a provider
        # failure — the same split the promo endpoint uses.
        status = 502 if outcome.estimated_usd else 422
        raise HTTPException(status_code=status, detail=outcome.error)

    return {"success": True, **outcome.as_dict()}


@router.get("/image/generations", summary="Stored image generations (free)")
async def get_generations(
    hot_content_id: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Previously generated images, newest first."""
    settings = get_settings()
    rows, total = await list_generations(
        settings, hot_content_id=hot_content_id, limit=limit, offset=offset
    )
    spent = sum(record.estimated_usd for record in rows)
    return {
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "estimated_usd_in_page": round(spent, 4),
        "items": [generation_to_dict(record) for record in rows],
    }

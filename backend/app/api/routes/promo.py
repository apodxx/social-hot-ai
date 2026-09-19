"""``/api/promo`` — README to promotion copy (Phase 10).

The one endpoint here **spends DeepSeek tokens** (one call per generation), so it says
so in its summary and the request carries the source explicitly: exactly one of
``text``, ``path`` or ``url``. Sending none, or all three, is a 422 rather than a guess.

Reading a local path is offered because that is how a developer actually has a README
checked out. It is restricted to document-like files — a whole file on disk must not be
pullable into a model prompt by naming it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.ai.promo_service import (
    MAX_README_BYTES,
    generate_promo,
    get_promo,
    list_promos,
    promo_to_dict,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class PromoRequest(BaseModel):
    """One generation request. Exactly one source field must be set."""

    text: str | None = Field(default=None, description="README 内容（直接粘贴）")
    path: str | None = Field(default=None, description="本地 README 路径（.md/.txt/.rst）")
    url: str | None = Field(default=None, description="README 的 http(s) 地址")
    project_name: str = Field(default="", description="项目名（README 里没有明确标题时使用）")
    extra_note: str = Field(default="", description="额外要求，例如目标读者或发布场合")
    style_reference_url: str | None = Field(
        default=None,
        description=(
            "小红书笔记分享链接，用作**风格参考**（学标题长度/分段/语气/标签用法）。"
            "解析链接免费；读取笔记内容需要 1 次 TikHub 计费调用。"
        ),
    )


@router.post(
    "/promo/readme",
    summary="Generate Xiaohongshu / Weibo / Douyin promotion copy from a README (billed)",
    description=(
        "**This endpoint spends DeepSeek tokens** — one call per generation, bounded by "
        "a structural digest of the README (`MAX_DIGEST_CHARS`). The model never sees "
        "the whole file, is told exactly what was cut, and is forbidden from inventing "
        "numbers, benchmarks or personal anecdotes that the README does not contain."
    ),
)
async def create_promo(payload: PromoRequest) -> dict[str, Any]:
    """Digest the README, generate three platform versions, and store the result."""
    settings = get_settings()
    supplied = [value for value in (payload.text, payload.path, payload.url) if value]
    if len(supplied) != 1:
        raise HTTPException(
            status_code=422,
            detail="provide exactly one of: text, path, url",
        )
    if payload.text and len(payload.text.encode("utf-8")) > MAX_README_BYTES:
        raise HTTPException(status_code=413, detail=f"README exceeds {MAX_README_BYTES} bytes")

    logger.warning(
        "POST /api/promo/readme source=%s style_reference=%s — billed: %s",
        "text" if payload.text else ("path" if payload.path else "url"),
        "yes" if payload.style_reference_url else "no",
        "1 TikHub call + 1 DeepSeek call" if payload.style_reference_url else "1 DeepSeek call",
    )
    run = await generate_promo(
        text=payload.text,
        path=payload.path,
        url=payload.url,
        project_name=payload.project_name,
        extra_note=payload.extra_note,
        settings=settings,
        style_url=payload.style_reference_url,
    )
    if not run.ok and not run.result:
        # The result carries the classification: 422 for a bad input (unreadable source,
        # web page instead of a README, nothing to promote), 502 for a provider problem.
        # Matching on message substrings used to report an input problem as a server error.
        raise HTTPException(status_code=run.status, detail=run.error)

    body = run.as_dict()
    if run.record_id:
        record = await get_promo(settings, run.record_id)
        body["promo"] = promo_to_dict(record) if record is not None else None
    return {"success": run.ok, **body}


@router.get("/promo/readme", summary="Stored promotions (free)")
async def list_readme_promos(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Previously generated promotions, newest first."""
    settings = get_settings()
    rows, total = await list_promos(settings, limit=limit, offset=offset)
    return {
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [promo_to_dict(row) for row in rows],
    }


@router.get("/promo/readme/{promo_id}", summary="One stored promotion (free)")
async def get_readme_promo(promo_id: int) -> dict[str, Any]:
    settings = get_settings()
    record = await get_promo(settings, promo_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"promotion {promo_id} not found")
    return {"success": True, "item": promo_to_dict(record)}

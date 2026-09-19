"""``/api/knowledge`` — 知识科普文章 + 标签球（Phase 13）。

计费边界写在这里，因为界面上的每个按钮都对应这里的某一条：

| 接口 | 计费 |
|---|---|
| ``GET  /knowledge/tags`` | **免费**（词表存库；首次为空时自动生成一次） |
| ``POST /knowledge/tags/refresh`` | 1 次 DeepSeek（「刷新标签」按钮） |
| ``POST /knowledge/articles`` | 1-2 次 DeepSeek + 默认 1 次搜图 |
| ``GET  /knowledge/articles`` | 免费 |
| ``POST /knowledge/images/search`` | 1 次 TikHub（$0.0078，一次多张） |

搜图是默认的配图方式：一次调用拿多张真实相关图片，成本是生成一张图的四分之一。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.knowledge_service import (
    STEP_LABELS,
    article_to_dict,
    ensure_tag_vocabulary,
    generate_article,
    get_article,
    list_articles,
    list_tags,
    refresh_tag_vocabulary,
    tag_to_dict,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class GenerateArticleRequest(BaseModel):
    """生成一篇科普文章。"""

    topic: str = Field(min_length=1, max_length=128, description="主题/标签，如「B+树」")
    tag_id: int | None = Field(default=None, description="来自哪个标签（点击球体时带上）")
    extra: str = Field(default="", max_length=500, description="额外要求，如「多给代码示例」")
    with_platforms: bool = Field(default=True, description="同时生成小红书/微博/抖音文案")
    with_images: bool = Field(default=True, description="同时搜配图（1 次 TikHub 调用）")
    image_count: int = Field(default=6, ge=1, le=20, description="配图张数上限")
    search_keyword: str = Field(default="", description="搜图关键词，留空用主题")


class RefreshTagsRequest(BaseModel):
    """刷新标签词表。"""

    extra: str = Field(default="", max_length=300, description="例如「多来点 AI 方向的」")


class SearchImagesRequest(BaseModel):
    """搜图（不生成文章，只找图并下载）。"""

    keyword: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=8, ge=1, le=20)


@router.get(
    "/knowledge/tags",
    summary="标签词表（免费；为空时自动生成一次）",
    description=(
        "3D 标签球的数据源。词表**存库**，所以重复打开页面不花钱；只有显式调用 "
        "`/knowledge/tags/refresh` 才消耗一次 DeepSeek 调用。"
    ),
)
async def get_tags(
    kind: str | None = Query(default=None, description="按分类过滤，如「系统网络」"),
) -> dict[str, Any]:
    """读词表；若库里标签太少则自动补一次（会说明是否发生了生成）。"""
    settings = get_settings()
    if kind:
        rows = await list_tags(settings, kind=kind)
        return {
            "success": True,
            "total": len(rows),
            "generated": False,
            "tags": [tag_to_dict(tag) for tag in rows],
        }
    vocabulary = await ensure_tag_vocabulary(settings)
    payload = vocabulary.as_dict()
    if payload.get("error"):
        raise HTTPException(status_code=502, detail=payload["error"])
    return {"success": True, **payload}


@router.post(
    "/knowledge/tags/refresh",
    summary="重新生成标签词表（消耗 1 次 DeepSeek 调用）",
    description=(
        "运营方点的「刷新标签」。**会产生费用**：一次 DeepSeek 调用，约 ¥0.01-0.03。"
        "已有人工调整过的标签会保留其分类与权重。"
    ),
)
async def refresh_tags(payload: RefreshTagsRequest) -> dict[str, Any]:
    settings = get_settings()
    if not settings.deepseek_configured:
        raise HTTPException(status_code=503, detail="DEEPSEEK_API_KEY 或 DEEPSEEK_MODEL 未配置")
    logger.warning("POST /api/knowledge/tags/refresh — spends one DeepSeek call")
    result = await refresh_tag_vocabulary(settings, extra=payload.extra)
    if result.error:
        raise HTTPException(status_code=502, detail=result.error)
    return {"success": True, **result.as_dict()}


@router.post(
    "/knowledge/articles",
    summary="为某个标签生成一篇科普文章 + 三平台文案 + 配图（计费）",
    description=(
        "**会产生费用**：文章 1 次 DeepSeek、三平台文案 1 次 DeepSeek、配图默认 1 次 "
        "TikHub 搜图（$0.0078，一次多张；比生成图片便宜 4 倍）。\n\n"
        "分步容错：文章生成失败直接返回；三平台或搜图失败只影响各自那一步，"
        "**已经生成的文章仍然保存**。"
    ),
)
async def create_article(payload: GenerateArticleRequest) -> dict[str, Any]:
    settings = get_settings()
    if not settings.deepseek_configured:
        raise HTTPException(status_code=503, detail="DEEPSEEK_API_KEY 或 DEEPSEEK_MODEL 未配置")
    logger.warning(
        "POST /api/knowledge/articles topic=%r platforms=%s images=%s — billed",
        payload.topic,
        payload.with_platforms,
        payload.with_images,
    )
    result = await generate_article(
        payload.topic,
        settings=settings,
        extra=payload.extra,
        with_platforms=payload.with_platforms,
        with_images=payload.with_images,
        image_count=payload.image_count,
        search_keyword=payload.search_keyword,
        tag_id=payload.tag_id,
    )
    if not result.ok:
        # 422 = 输入/模型输出不可用；502 = 供应商失败。
        status = 502 if "失败" in (result.error or "") else 422
        raise HTTPException(status_code=status, detail=result.error or "生成失败")
    return {"success": True, **result.as_dict()}


@router.post(
    "/knowledge/articles/stream",
    summary="同上，但**逐步推送进度**（NDJSON 流）",
    description=(
        "文章生成要 30-60 秒，一个只会转的圈等于没信息。这个接口把每一步的开始/完成/失败"
        "作为一行 JSON 推给调用方，最后一行是完整结果。\n\n"
        "`application/x-ndjson`，每行形如：\n"
        '`{"event":"step","step":"article","status":"started","label":"生成科普文章"}`\n'
        '`{"event":"step","step":"platforms","status":"done","platforms":["douyin","weibo"]}`\n'
        '`{"event":"result","ok":true,"article":{...}}`\n'
        '`{"event":"error","error":"..."}`\n\n'
        "计费与 `/knowledge/articles` 完全相同。"
    ),
)
async def create_article_stream(payload: GenerateArticleRequest) -> StreamingResponse:
    """Generate an article while streaming per-step progress."""
    settings = get_settings()
    if not settings.deepseek_configured:
        raise HTTPException(status_code=503, detail="DEEPSEEK_API_KEY 或 DEEPSEEK_MODEL 未配置")
    logger.warning(
        "POST /api/knowledge/articles/stream topic=%r — billed (streaming progress)",
        payload.topic,
    )

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_progress(step: str, status: str, detail: dict[str, Any]) -> None:
            await queue.put(
                {
                    "event": "step",
                    "step": step,
                    "status": status,
                    "label": STEP_LABELS.get(step, step),
                    **detail,
                }
            )

        async def run() -> None:
            try:
                outcome = await generate_article(
                    payload.topic,
                    settings=settings,
                    extra=payload.extra,
                    with_platforms=payload.with_platforms,
                    with_images=payload.with_images,
                    image_count=payload.image_count,
                    search_keyword=payload.search_keyword,
                    tag_id=payload.tag_id,
                    on_progress=on_progress,
                )
                if outcome.ok:
                    await queue.put({"event": "result", **outcome.as_dict()})
                else:
                    await queue.put({"event": "error", "error": outcome.error})
            except Exception as exc:  # noqa: BLE001 - 必须让客户端收到结束信号
                logger.error("streamed article generation crashed: %s", exc, exc_info=True)
                await queue.put({"event": "error", "error": f"{type(exc).__name__}: {exc}"})

        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # 心跳：让前端知道连接还活着，也避免中间代理掐掉空闲连接。
                    yield json.dumps({"event": "ping"}, ensure_ascii=False) + "\n"
                    continue
                yield json.dumps(item, ensure_ascii=False) + "\n"
                if item.get("event") in {"result", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/knowledge/articles", summary="已生成的文章列表（免费）")
async def get_articles(
    topic: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    settings = get_settings()
    rows, total = await list_articles(settings, topic=topic, limit=limit, offset=offset)
    return {
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        # 列表只给摘要字段：正文可能很长，列表页不需要。
        "items": [
            {
                "id": record.id,
                "topic": record.topic,
                "title": record.title,
                "hook": record.hook,
                "difficulty": record.difficulty,
                "tags": record.tags or [],
                "image_count": len(record.images or []),
                "platforms": sorted((record.platforms or {}).keys()),
                "estimated_cny": record.estimated_cny,
                "created_at": record.created_at.isoformat() if record.created_at else None,
            }
            for record in rows
        ],
    }


@router.get("/knowledge/articles/{article_id}", summary="一篇文章的完整内容（免费）")
async def get_article_detail(article_id: int) -> dict[str, Any]:
    settings = get_settings()
    record = await get_article(article_id, settings)
    if record is None:
        raise HTTPException(status_code=404, detail=f"找不到文章 {article_id}")
    return {"success": True, "article": article_to_dict(record)}


@router.post(
    "/knowledge/images/search",
    summary="搜图并下载到素材库（1 次 TikHub 调用）",
    description=(
        "**计费**：每次约 $0.0078，一次返回多张。图片会立即下载到本地素材库"
        "（平台链接会过期，不下载就用不了）。每条都返回作者与原文链接，用于署名。"
    ),
)
async def search_images_endpoint(payload: SearchImagesRequest) -> dict[str, Any]:
    settings = get_settings()
    if not settings.tikhub_configured:
        raise HTTPException(status_code=503, detail="TIKHUB_API_KEY 未配置")
    from app.services.tikhub.image_search import search_and_download

    logger.warning(
        "POST /api/knowledge/images/search keyword=%r — 1 billed TikHub call", payload.keyword
    )
    images, summary = await search_and_download(
        payload.keyword, limit=payload.limit, settings=settings
    )
    return {
        "success": bool(images),
        "keyword": payload.keyword,
        "images": [image.as_dict() for image in images],
        **summary,
    }

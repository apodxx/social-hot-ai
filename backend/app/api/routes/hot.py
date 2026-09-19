"""``GET /api/hot`` — the Phase 1 deliverable.

Returns hot content for every supported platform in one unified structure. A
platform that fails is reported in ``errors`` while the others still return
data, because one dead platform must not hide the other two.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.db.database import session_scope
from app.db.repository import (
    database_stats,
    list_admin_hot_contents,
    list_topic_groups,
    topic_members,
)
from app.models.hot_content import HotContent, Platform
from app.schemas.hot import HotResponse
from app.services.pipeline.hot_pipeline import collect_and_store
from app.services.pipeline.search_pipeline import collect_by_keyword
from app.services.tikhub import ADAPTER_CLASSES, TikHubClient, fetch_all_hot
from app.services.tikhub.search import SEARCH_ADAPTERS

logger = logging.getLogger(__name__)
router = APIRouter()

PLATFORM_ORDER: tuple[Platform, ...] = (
    Platform.XIAOHONGSHU,
    Platform.WEIBO,
    Platform.DOUYIN,
)


def _resolve_adapters(platforms: str | None) -> list[type]:
    """Pick adapters from a ``platforms=xiaohongshu,weibo`` filter."""
    if not platforms:
        return list(ADAPTER_CLASSES)
    wanted = {part.strip().lower() for part in platforms.split(",") if part.strip()}
    unknown = wanted - {cls.platform.value for cls in ADAPTER_CLASSES}
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported platform(s): {', '.join(sorted(unknown))}; "
            f"supported: {', '.join(cls.platform.value for cls in ADAPTER_CLASSES)}",
        )
    return [cls for cls in ADAPTER_CLASSES if cls.platform.value in wanted]


def _dump(item: HotContent, *, include_raw: bool) -> dict[str, Any]:
    """Serialise one item, dropping ``raw_data`` unless it was asked for."""
    exclude = None if include_raw else {"raw_data"}
    return item.model_dump(mode="json", exclude=exclude)


@router.get(
    "/hot",
    summary="Hot content across all Phase 1 platforms",
    responses={200: {"model": HotResponse, "description": "Unified hot content"}},
)
async def get_hot(
    limit: int | None = Query(
        default=None,
        ge=1,
        le=200,
        description="Max items per platform; defaults to HOT_LIMIT_PER_PLATFORM.",
    ),
    platforms: str | None = Query(
        default=None,
        description="Comma-separated subset, e.g. `weibo,douyin` (default: all).",
    ),
    include_raw: bool = Query(
        default=False,
        description="Include each provider's original item under `raw_data`.",
    ),
) -> dict[str, Any]:
    """Fetch and return unified hot content."""
    settings = get_settings()
    if not settings.tikhub_configured:
        raise HTTPException(
            status_code=503,
            detail="TIKHUB_API_KEY is not configured. Copy .env.example to .env and set it.",
        )

    effective_limit = limit or settings.hot_limit_per_platform
    adapters = _resolve_adapters(platforms)
    logger.info("GET /api/hot limit=%d platforms=%s", effective_limit, [c.platform.value for c in adapters])

    async with TikHubClient(settings) as client:
        items_by_platform, errors = await fetch_all_hot(
            client, effective_limit, adapter_classes=adapters
        )

    data = {
        platform.value: [
            _dump(item, include_raw=include_raw)
            for item in items_by_platform.get(platform, [])
        ]
        for platform in PLATFORM_ORDER
        if platform in {cls.platform for cls in adapters}
    }
    counts = {name: len(items) for name, items in data.items()}

    if not data or all(count == 0 for count in counts.values()):
        # Nothing usable at all: report the failures as a gateway error rather
        # than a cheerful 200 with three empty lists.
        logger.error("every requested platform failed: %s", errors)
        raise HTTPException(
            status_code=502,
            detail={"message": "all requested platforms failed", "errors": errors},
        )

    logger.info("GET /api/hot -> %s", counts)
    return {
        "success": True,
        "data": data,
        "errors": errors,
        "counts": counts,
        "limit_per_platform": effective_limit,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# =============================================================================
# Phase 2: persistence and cross-platform topic aggregation
# =============================================================================


def _row_to_dict(row: Any, *, include_raw: bool) -> dict[str, Any]:
    """Serialise a stored row (``raw_data`` is opt-in, as in the live endpoint)."""
    payload: dict[str, Any] = {
        "id": row.id,
        "platform": row.platform,
        "platform_content_id": row.platform_content_id,
        "title": row.title,
        "description": row.description,
        "author": row.author,
        "author_id": row.author_id,
        "url": row.url,
        "publish_time": row.publish_time.isoformat() if row.publish_time else None,
        "rank": row.rank,
        "hot_value": row.hot_value,
        "likes": row.likes,
        "comments": row.comments,
        "shares": row.shares,
        "collects": row.collects,
        "content_type": row.content_type,
        "cover_url": row.cover_url,
        "video_url": row.video_url,
        "topic_group_id": row.topic_group_id,
        "origin": row.origin,
        "source_keyword": row.source_keyword,
        "image_count": row.image_count,
        "media": _media_brief(row.media),
        "detail_fetched": bool(row.detail_fetched_at),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_raw:
        payload["raw_data"] = row.raw_data
    return payload


def _media_brief(media: Any) -> dict[str, Any]:
    """The media fields the admin UI needs, without the download bookkeeping.

    ``bytes``/``sha256`` are deliberately left out of the API surface: they describe
    the local file, not the content, and the list view has no use for them.
    """
    payload = media if isinstance(media, dict) else {}
    images = payload.get("images") or []
    video = payload.get("video") or None
    return {
        "images": [
            {
                "url": image.get("url"),
                "local_path": image.get("local_path"),
                "width": image.get("width"),
                "height": image.get("height"),
            }
            for image in images
            if isinstance(image, dict)
        ],
        "video": (
            {
                "url": video.get("url"),
                "cover_url": video.get("cover_url"),
                "duration_ms": video.get("duration_ms"),
            }
            if isinstance(video, dict)
            else None
        ),
        "has_image": bool(images),
        "has_video": bool(isinstance(video, dict) and video.get("url")),
    }


def _group_to_dict(group: Any, member_count: int) -> dict[str, Any]:
    return {
        "id": group.id,
        "topic": group.topic,
        "summary": group.summary,
        "platforms": group.platforms,
        # The spec's "related_contents": derived from members, never stored.
        "related_contents": member_count,
        "is_cross_platform": len(group.platforms or []) > 1,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
    }


@router.post(
    "/hot/collect",
    summary="Fetch from TikHub, deduplicate, store, and aggregate topics",
    description=(
        "**This endpoint spends money.** Every platform costs at least one billed "
        "TikHub endpoint call (Xiaohongshu may cost two, because its `num` is "
        "capped at 40). For a zero-cost equivalent, load the captured fixtures "
        "with `scripts/load_fixtures_into_db.py`."
    ),
)
async def collect_hot(
    limit: int | None = Query(
        default=None, ge=1, le=200, description="Max items per platform."
    ),
    platforms: str | None = Query(
        default=None, description="Comma-separated subset, e.g. `weibo`."
    ),
) -> dict[str, Any]:
    """Run the Phase 2 pipeline once and report what it did."""
    settings = get_settings()
    if not settings.tikhub_configured:
        raise HTTPException(
            status_code=503,
            detail="TIKHUB_API_KEY is not configured; nothing to collect.",
        )
    effective_limit = limit or settings.hot_limit_per_platform
    logger.warning("POST /api/hot/collect limit=%d — this run makes billed calls", effective_limit)

    result = await collect_and_store(
        effective_limit, settings=settings, platforms=platforms
    )
    async with session_scope(settings) as session:
        stats = await database_stats(session)
    return {"success": True, "run": result.as_dict(), "database": stats}


def _analysis_brief(analysis: Any) -> dict[str, Any] | None:
    """The analysis fields the admin list shows (§30's "AI推荐" column)."""
    if analysis is None:
        return None
    return {
        "topic": analysis.topic,
        "summary": analysis.summary,
        "why_hot": analysis.why_hot,
        "content_angle": analysis.content_angle,
        "recommended": analysis.recommended,
        "confidence": analysis.confidence,
        "needs_verification": analysis.needs_verification,
        "selected": analysis.selected,
    }


def _rewrite_brief(rewrite: Any) -> dict[str, Any] | None:
    """The rewrite status the admin list shows (§30's "状态" column)."""
    if rewrite is None:
        return None
    return {
        "status": rewrite.status,
        "needs_verification": rewrite.needs_verification,
        "risk_flags": rewrite.risk_flags or [],
        "attempts": rewrite.attempts,
        "has_layout": bool(rewrite.layout),
        "updated_at": rewrite.updated_at.isoformat() if rewrite.updated_at else None,
    }


@router.get("/hot/stored", summary="Query stored hot content (admin list)")
async def get_stored_hot(
    platform: str | None = Query(default=None, description="weibo | douyin | xiaohongshu"),
    q: str | None = Query(default=None, description="Keyword search on the title."),
    since: datetime | None = Query(default=None, description="ISO timestamp, inclusive"),
    until: datetime | None = Query(default=None, description="ISO timestamp, inclusive"),
    recommended: bool | None = Query(
        default=None, description="Only AI-recommended items (implies an analysis exists)."
    ),
    selected: bool | None = Query(
        default=None, description="Only items selected for rewriting."
    ),
    origin: str | None = Query(
        default=None, description="hot = ranking/feed entry, search = keyword hit."
    ),
    with_images: bool | None = Query(
        default=None, description="Only items that carry pictures (or only those that do not)."
    ),
    source_keyword: str | None = Query(
        default=None,
        description="Exact match on the keyword a search result was collected under.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_raw: bool = Query(default=False),
) -> dict[str, Any]:
    """Read what is already in the database. Costs nothing.

    Each item carries its analysis, rewrite state and media, so the list
    renders in one request instead of one request per row.
    """
    settings = get_settings()
    if origin and origin not in {"hot", "search"}:
        raise HTTPException(
            status_code=422, detail=f"unknown origin {origin!r}; expected 'hot' or 'search'"
        )
    async with session_scope(settings) as session:
        rows, total = await list_admin_hot_contents(
            session,
            platform=platform,
            keyword=q,
            since=since,
            until=until,
            recommended=recommended,
            selected=selected,
            origin=origin,
            with_images=with_images,
            source_keyword=source_keyword,
            limit=limit,
            offset=offset,
        )
    items = []
    for item, analysis, rewrite in rows:
        payload = _row_to_dict(item, include_raw=include_raw)
        payload["analysis"] = _analysis_brief(analysis)
        payload["rewrite"] = _rewrite_brief(rewrite)
        items.append(payload)
    return {
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/hot/topics", summary="Cross-platform topic groups")
async def get_topic_groups(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    cross_platform_only: bool = Query(
        default=False, description="Only topics present on two or more platforms."
    ),
) -> dict[str, Any]:
    """Deduplication layer 4: topics aggregated across platforms."""
    settings = get_settings()
    async with session_scope(settings) as session:
        groups, total = await list_topic_groups(
            session, limit=limit, offset=offset, cross_platform_only=cross_platform_only
        )
    return {
        "success": True,
        "total": total,
        "items": [_group_to_dict(group, members) for group, members in groups],
    }


@router.get("/hot/topics/{group_id}", summary="One topic group with its members")
async def get_topic_group(group_id: int, include_raw: bool = Query(default=False)) -> dict[str, Any]:
    """Members of one topic, best-ranked first."""
    settings = get_settings()
    async with session_scope(settings) as session:
        groups, _ = await list_topic_groups(session, limit=200, offset=0)
        match = next((group for group, _ in groups if group.id == group_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail=f"topic group {group_id} not found")
        members = await topic_members(session, group_id)
    return {
        "success": True,
        "topic": _group_to_dict(match, len(members)),
        "members": [_row_to_dict(row, include_raw=include_raw) for row in members],
    }


@router.get(
    "/hot/pending",
    summary="What is waiting to be analysed, and what a run would cost (free)",
    description=(
        "Counts stored rows with no analysis and how many of them the interest filter "
        "would actually send. Lets the UI offer the next step with a real number — the "
        "gap between 'collected' and 'analysed' was invisible before."
    ),
)
async def get_pending_analysis() -> dict[str, Any]:
    """Free: reads the database only."""
    from app.services.pipeline.analysis_gap import pending_analysis

    pending = await pending_analysis(get_settings())
    return {"success": True, "pending": pending.as_dict()}


@router.post(
    "/hot/watch",
    summary="Search every configured domain keyword (billed)",
    description=(
        "**One billed TikHub call per keyword per platform.** Runs the same ``watch`` "
        "stage the scheduled pipeline runs, so the domain content can be refreshed on "
        "demand. The number of calls is keywords x platforms and is returned in the report."
    ),
)
async def run_watch_now() -> dict[str, Any]:
    """Run the domain-keyword searches immediately."""
    settings = get_settings()
    if not settings.tikhub_configured:
        raise HTTPException(status_code=503, detail="TIKHUB_API_KEY is not configured.")
    if not settings.watch_search_enabled:
        raise HTTPException(
            status_code=503,
            detail="the watch stage is disabled: set WATCH_SEARCH_ENABLED=true",
        )
    planned = settings.watch_billed_calls_per_run
    logger.warning(
        "POST /api/hot/watch keywords=%s platforms=%s — %d billed call(s)",
        settings.watch_keyword_list,
        settings.watch_platform_list,
        planned,
    )
    from app.services.pipeline.task_recorder import run_as_task

    # Recorded like any pipeline run: this spends money, so it must show up on the
    # 任务记录 page instead of being inferable only from the balance.
    recorded = await run_as_task(
        task_type="watch",
        step_name="watch",
        work=lambda: _watch_work(settings),
        settings=settings,
    )
    return {
        "success": True,
        "planned_calls": planned,
        "run": recorded.result,
        "task": recorded.as_dict(),
    }


async def _watch_work(settings: Any) -> dict[str, Any]:
    """The watch stage's report, as the task recorder expects it."""
    from app.services.pipeline.watch_stage import run_watch_stage

    return (await run_watch_stage(settings)).as_dict()


async def _search_work(
    keyword: str,
    platforms: list[str] | None,
    limit: int | None,
    download_media: bool | None,
    settings: Any,
) -> dict[str, Any]:
    """One keyword search's report, as the task recorder expects it."""
    from app.services.pipeline.search_pipeline import collect_by_keyword

    result = await collect_by_keyword(
        keyword,
        platforms=platforms,
        limit=limit,
        download_media=download_media,
        settings=settings,
    )
    return result.as_dict()


class SearchRequest(BaseModel):
    """A keyword search request (§12).

    ``keyword`` is required because an empty search would still cost one billed call
    per platform and return the platform's generic feed — money spent on nothing.
    """

    keyword: str = Field(min_length=1, max_length=64, description="要搜索的话题/关键词")
    platforms: list[str] | None = Field(
        default=None, description="默认三个平台；可只选其中一部分以省钱"
    )
    limit: int | None = Field(default=None, ge=1, le=50, description="每平台保留条数")
    download_media: bool | None = Field(
        default=None, description="是否把图片下载到本地素材库（默认按 MEDIA_DOWNLOAD_ENABLED）"
    )
    analyze_after: bool = Field(
        default=False,
        description=(
            "采集后立刻跑一次 AI 分析（**额外消耗 DeepSeek token**）。"
            "默认关闭：采集与分析是两笔不同的费用，不该由一次点击同时触发。"
        ),
    )


@router.post(
    "/hot/search",
    summary="Search a topic by keyword across platforms (billed)",
    description=(
        "**This endpoint makes billed TikHub calls**: one search per platform. "
        "Unlike the hot-list endpoints, the results are real posts, so they carry "
        "images and video. Images are downloaded into the local material library "
        "because the provider URLs are signed and expire."
    ),
)
async def search_hot(payload: SearchRequest) -> dict[str, Any]:
    """Search, store with ``origin=search``, and fetch the images."""
    settings = get_settings()
    if not settings.tikhub_configured:
        raise HTTPException(
            status_code=503,
            detail="TIKHUB_API_KEY is not configured; nothing to search.",
        )
    keyword = payload.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="keyword must not be blank")

    platforms = payload.platforms
    if platforms:
        unknown = set(platforms) - set(SEARCH_ADAPTERS)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported platform(s): {', '.join(sorted(unknown))}; "
                f"supported: {', '.join(sorted(SEARCH_ADAPTERS))}",
            )

    billed = len(platforms) if platforms else len(settings.search_platform_list)
    logger.warning(
        "POST /api/hot/search keyword=%r platforms=%s — %d billed TikHub call(s)",
        keyword,
        platforms or settings.search_platform_list,
        billed,
    )

    # Recorded, so this billed call is visible on the 任务记录 page rather than only
    # inferable from the TikHub balance.
    from app.services.pipeline.task_recorder import run_as_task

    recorded = await run_as_task(
        task_type="search",
        step_name="search",
        work=lambda: _search_work(
            keyword, platforms, payload.limit, payload.download_media, settings
        ),
        settings=settings,
    )
    result_dict = recorded.result

    # Collecting and analysing are separate costs, so the second one only happens when
    # the caller asked for it — but leaving the operator with 191 unanalysed rows and no
    # hint was worse, hence the flag and the pending endpoint.
    analysis: dict[str, Any] | None = None
    if payload.analyze_after:
        from app.services.pipeline.analysis_gap import run_analysis_after_collection

        logger.warning("POST /api/hot/search analyze_after=true — billed DeepSeek calls")
        analysis = await run_analysis_after_collection(settings)
    # Read back what is now stored for this keyword, so the caller sees rows (with
    # ids and local image paths) rather than only counters. Filtering is by
    # ``source_keyword`` (exact), not by title text: only a handful of the collected
    # items happen to carry the keyword in their title.
    async with session_scope(settings) as session:
        rows, total = await list_admin_hot_contents(
            session, source_keyword=keyword, limit=100
        )

    return {
        "success": True,
        "run": result_dict,
        "task": recorded.as_dict(),
        "analysis": analysis,
        "total": total,
        "items": [
            {
                **_row_to_dict(item, include_raw=False),
                "analysis": _analysis_brief(analysis),
                "rewrite": _rewrite_brief(rewrite),
            }
            for item, analysis, rewrite in rows
        ],
    }

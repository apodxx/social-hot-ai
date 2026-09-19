"""Phase 8: the MCP server (spec section 三十三).

The endpoint is mounted **inside the FastAPI process** (``/mcp/``), so one service
decides whether it is available: start the backend and the tools exist, stop it and
they are gone. That also means the tools talk to the same settings, the same database
and the same serialisers the admin UI uses — no second code path to drift.

Three decisions worth stating:

* **Read-only tools are always exposed; billed tools sit behind a switch.**
  ``MCP_ALLOW_BILLED=false`` removes the spending tools from ``tools/list`` entirely.
  Tool schemas ride along on *every* model request, so a tool that is not there costs
  nothing, and a tool nobody should call cannot be called by accident.
* **Descriptions carry the cost.** Every billed tool says what it spends, in the name
  the model reads before deciding. This is a second line of defence: the real gate is
  the human approval in the client (see the DSH billing guard), and this is what the
  approver is looking at when they decide.
* **Everything is delegated, nothing is reimplemented.** Reads call the same
  repository functions as ``/api/*``; runs call the same service entry points as the
  POST routes. The MCP surface is a second door onto one implementation, not a fork.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.core.config import Settings, get_settings
from app.db.database import session_scope

# Aliased with a `repo_` prefix so the tool functions below can keep the natural
# names the model sees (`list_rewrites`, `list_tasks`) without shadowing them.
from app.db.repository import (
    analysis_for_content,
    dashboard_stats,
    list_admin_hot_contents,
    list_rewrites,
    list_tasks,
    list_topic_groups,
    rewrite_for_content,
)

logger = logging.getLogger(__name__)

#: Names of the tools that spend money. The client-side billing guard matches these
#: exact names, so changing one here without changing the guard would open a hole.
BILLED_TOOL_NAMES = (
    "run_collection",
    "run_watch_searches",
    "search_topic",
    "run_analysis",
    "run_rewrite",
    "run_pipeline",
    "generate_readme_promo",
    "generate_post_image",
    "generate_knowledge_article",
    "refresh_knowledge_tags",
    "search_knowledge_images",
)


def _hot_row(item: Any, analysis: Any, rewrite: Any) -> dict[str, Any]:
    """One hot item as the assistant needs it: no raw payload, no empty fields."""
    payload: dict[str, Any] = {
        "id": item.id,
        "platform": item.platform,
        "title": item.title,
        "rank": item.rank,
        "hot_value": item.hot_value,
        "url": item.url,
        "origin": item.origin,
        "source_keyword": item.source_keyword,
        "image_count": item.image_count,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }
    if item.image_count:
        # Only the fact and a thumbnail path, never the whole image list: this rides in
        # the model's context on every call.
        images = (item.media or {}).get("images") or []
        if images:
            payload["first_image"] = {
                "url": images[0].get("url"),
                "local_path": images[0].get("local_path"),
            }
    if analysis is not None:
        payload["analysis"] = {
            "topic": analysis.topic,
            "recommended": analysis.recommended,
            "confidence": round(float(analysis.confidence or 0.0), 3),
            "selected": analysis.selected,
            "needs_verification": analysis.needs_verification,
        }
    if rewrite is not None:
        payload["rewrite"] = {
            "status": rewrite.status,
            "needs_verification": rewrite.needs_verification,
            "risk_flags": rewrite.risk_flags or [],
        }
    return payload


def build_mcp_server(settings: Settings | None = None) -> MCPServer:
    """Build the MCP server with its tools registered.

    Read-only tools never spend money and are always registered. Billed tools are
    omitted when ``MCP_ALLOW_BILLED`` is false.
    """
    resolved = settings or get_settings()
    server: MCPServer = MCPServer(
        name="socialhot",
        title="SocialHot AI",
        version="0.8.0",
        instructions=(
            "全网热点采集与 AI 二创工具。读取类工具免费；采集/分析/二创会真实消耗 "
            "TikHub 配额或 DeepSeek token。系统只生成草稿，**不会自动发布**。"
        ),
    )

    # ---------------------------------------------------------------- free reads
    @server.tool(
        description=(
            "热点采集与 AI 处理的统计数字：各平台条数、已分析/已入选/已生成二创、"
            "待人工审核数量。免费。"
        )
    )
    async def get_stats(today: bool = True) -> dict[str, Any]:
        """Dashboard counters. ``today`` limits the per-platform counts to today."""
        since = None
        if today:
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo

            try:
                zone = ZoneInfo(resolved.scheduler_timezone)
            except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
                zone = timezone.utc
            now = datetime.now(zone)
            since = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        async with session_scope(resolved) as session:
            stats = await dashboard_stats(session, since=since)
        stats.pop("latest_task", None)
        return stats

    @server.tool(
        description=(
            "查询已入库的热点（可按平台/关键词/AI推荐/入选筛选），每条附带 AI 分析结论与"
            "二创状态，以及图片数量和是否含视频。免费，返回本地数据。"
        )
    )
    async def list_hot(
        platform: str | None = None,
        keyword: str | None = None,
        recommended: bool | None = None,
        selected: bool | None = None,
        with_images: bool | None = None,
        origin: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Stored hot items, newest first.

        ``with_images`` selects items that carry pictures (search results do, ranking
        entries do not). ``origin`` is ``hot`` or ``search``.
        """
        limit = max(1, min(int(limit), 50))
        async with session_scope(resolved) as session:
            rows, total = await list_admin_hot_contents(
                session,
                platform=platform,
                keyword=keyword,
                recommended=recommended,
                selected=selected,
                with_images=with_images,
                origin=origin,
                limit=limit,
                offset=offset,
            )
        return {
            "total": total,
            "returned": len(rows),
            "items": [_hot_row(item, analysis, rewrite) for item, analysis, rewrite in rows],
        }

    @server.tool(
        description=(
            "跨平台话题聚合结果（去重第 4 层）。注意：该层用词面相似度判断同一话题，"
            "阈值较高，结果经常为空，这属于已知局限而非故障。免费。"
        )
    )
    async def get_topics(cross_platform_only: bool = False, limit: int = 20) -> dict[str, Any]:
        """Topic groups with their platform lists."""
        async with session_scope(resolved) as session:
            groups, total = await list_topic_groups(
                session,
                limit=max(1, min(int(limit), 50)),
                offset=0,
                cross_platform_only=cross_platform_only,
            )
        return {
            "total": total,
            "items": [
                {
                    "id": group.id,
                    "topic": group.topic,
                    "summary": group.summary,
                    "platforms": group.platforms or [],
                    "members": member_count,
                    "is_cross_platform": len(group.platforms or []) > 1,
                }
                for group, member_count in groups
            ],
        }

    @server.tool(description="读取单条热点的完整 AI 分析（话题、摘要、为什么火、切入角度、讨论点）。免费。")
    async def get_analysis(hot_content_id: int) -> dict[str, Any]:
        """The full analysis for one hot item."""
        from sqlalchemy import select

        from app.models.hot_content import HotContentRecord

        async with session_scope(resolved) as session:
            analysis = await analysis_for_content(session, int(hot_content_id))
            if analysis is None:
                return {"found": False, "hot_content_id": hot_content_id}
            item = (
                await session.execute(
                    select(HotContentRecord).where(HotContentRecord.id == int(hot_content_id))
                )
            ).scalars().first()
        return {
            "found": True,
            "hot_content_id": analysis.hot_content_id,
            "topic": analysis.topic,
            "summary": analysis.summary,
            "why_hot": analysis.why_hot,
            "content_angle": analysis.content_angle,
            "discussion_points": analysis.discussion_points or [],
            "account_fit": analysis.account_fit,
            "recommended": analysis.recommended,
            "confidence": round(float(analysis.confidence or 0.0), 3),
            "selected": analysis.selected,
            "needs_verification": analysis.needs_verification,
            "source": (
                {"platform": item.platform, "title": item.title, "url": item.url}
                if item is not None
                else None
            ),
        }

    @server.tool(
        description=(
            "读取单条热点的二创草稿（小红书/微博/抖音三平台文案）。系统不会自动发布，"
            "返回的是需要人工审核或复制的草稿。免费。"
        )
    )
    async def get_rewrite(hot_content_id: int) -> dict[str, Any]:
        async with session_scope(resolved) as session:
            found = await rewrite_for_content(session, int(hot_content_id))
            if found is None:
                return {"found": False, "hot_content_id": hot_content_id}
            rewrite, item = found
            payload = {
                "found": True,
                "hot_content_id": rewrite.hot_content_id,
                "status": rewrite.status,
                "needs_verification": rewrite.needs_verification,
                "verification_note": rewrite.verification_note,
                "risk_flags": rewrite.risk_flags or [],
                "summary": rewrite.summary,
                "angle": rewrite.angle,
                "xiaohongshu": {
                    "title": rewrite.xiaohongshu_title,
                    "content": rewrite.xiaohongshu_content,
                    "ending": rewrite.xiaohongshu_ending,
                    "hashtags": rewrite.xiaohongshu_hashtags or [],
                },
                "weibo": {
                    "opening": rewrite.weibo_opening,
                    "content": rewrite.weibo_content,
                    "hashtags": rewrite.weibo_hashtags or [],
                },
                "douyin": {
                    "hook": rewrite.douyin_hook,
                    "script": rewrite.douyin_script,
                    "scenes": rewrite.douyin_scene_suggestions or [],
                    "cta": rewrite.douyin_cta,
                },
                "source": (
                    {"platform": item.platform, "title": item.title, "url": item.url}
                    if item is not None
                    else None
                ),
            }
        return payload

    @server.tool(description="列出已生成的二创草稿及其状态（可发布 / 需人工审核）。免费。")
    async def list_rewrites(status: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Stored rewrites, newest first."""
        async with session_scope(resolved) as session:
            rows, total = await list_rewrites(
                session, status=status, limit=max(1, min(int(limit), 50)), offset=0
            )
        return {
            "total": total,
            "items": [
                {
                    "hot_content_id": rewrite.hot_content_id,
                    "status": rewrite.status,
                    "needs_verification": rewrite.needs_verification,
                    "risk_flags": rewrite.risk_flags or [],
                    "title": rewrite.xiaohongshu_title,
                    "source_title": item.title if item is not None else None,
                    "source_platform": item.platform if item is not None else None,
                    "updated_at": rewrite.updated_at.isoformat() if rewrite.updated_at else None,
                }
                for rewrite, item in rows
            ],
        }

    @server.tool(description="历次流水线运行的记录：状态、耗时、各阶段结果与错误。免费。")
    async def list_tasks(status: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Recorded pipeline runs, newest first."""
        async with session_scope(resolved) as session:
            rows, total = await list_tasks(
                session, status=status, limit=max(1, min(int(limit), 50)), offset=0
            )
        return {
            "total": total,
            "items": [
                {
                    "id": task.id,
                    "task_type": task.task_type,
                    "status": task.status,
                    "started_at": task.started_at.isoformat() if task.started_at else None,
                    "duration_ms": task.duration_ms,
                    "error_message": task.error_message,
                    "steps": task.steps or [],
                }
                for task in rows
            ],
        }

    @server.tool(description="定时任务的配置与下次运行时间，以及调度器当前是否在运行。免费。")
    async def get_schedule() -> dict[str, Any]:
        """The configured schedule and the next fire times."""
        from app.services.pipeline.runner import describe_schedule

        return describe_schedule(resolved)

    @server.tool(
        description=(
            "查看当前账号的领域偏好关键词，以及库里有多少条与这些领域相关。"
            "用来确认「推给我的内容是不是我关心的方向」。免费。"
        )
    )
    async def get_interest_profile(sample: int = 200) -> dict[str, Any]:
        """The configured domain keywords and how much stored content matches them."""
        from sqlalchemy import select

        from app.models.hot_content import HotContentRecord
        from app.services.pipeline.interest import InterestProfile, relevance_summary

        profile = InterestProfile.from_settings(resolved)
        async with session_scope(resolved) as session:
            rows = (
                await session.execute(
                    select(HotContentRecord).order_by(HotContentRecord.id.desc()).limit(sample)
                )
            ).scalars().all()
        summary = relevance_summary(list(rows), profile)
        return {
            "keywords": list(profile.keywords),
            "negative_keywords": list(profile.negatives),
            "only_relevant_are_analysed": profile.only,
            "watch_keywords": resolved.watch_keyword_list,
            "watch_platforms": resolved.watch_platform_list,
            "watch_billed_calls_per_run": resolved.watch_billed_calls_per_run,
            "sampled_rows": len(rows),
            **summary,
        }

    @server.tool(description="已生成的 README 推广文案列表（小红书/微博/抖音三平台）。免费。")
    async def list_readme_promos(limit: int = 10) -> dict[str, Any]:
        """Previously generated project promotions, newest first."""
        from app.services.ai.promo_service import list_promos, promo_to_dict

        rows, total = await list_promos(resolved, limit=max(1, min(int(limit), 50)))
        return {
            "total": total,
            "items": [
                {
                    "id": row.id,
                    "project_name": row.project_name,
                    "one_liner": row.one_liner,
                    "xiaohongshu_title": (row.versions or {}).get("xiaohongshu", {}).get("title"),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
        }

    @server.tool(description="读取一份已生成的推广文案的三平台完整内容。免费。")
    async def get_readme_promo(promo_id: int) -> dict[str, Any]:
        """One stored promotion, with all three platform versions."""
        from app.services.ai.promo_service import get_promo, promo_to_dict

        record = await get_promo(resolved, int(promo_id))
        if record is None:
            return {"found": False, "promo_id": promo_id}
        return {"found": True, **promo_to_dict(record)}

    @server.tool(
        description=(
            "查看图片二创的单价与某条素材是否就绪（免费）。"
            "生成图片按张计费：1K 档约 $0.034/张、2K 档约 $0.069/张，"
            "**约等于一次文字二创的 15 倍**，所以这是本项目最贵的操作。"
        )
    )
    async def get_image_estimate(hot_content_id: int | None = None) -> dict[str, Any]:
        """Price per image and whether an item has usable local material. Free."""
        from app.services.ai.image_service import pick_references

        available = None
        if hot_content_id is not None:
            from sqlalchemy import select

            from app.models.hot_content import HotContentRecord

            async with session_scope(resolved) as session:
                row = (
                    await session.execute(
                        select(HotContentRecord).where(
                            HotContentRecord.id == int(hot_content_id)
                        )
                    )
                ).scalars().first()
            if row is not None:
                available = len(pick_references(row.media or {}, settings=resolved))
        return {
            "configured": resolved.dashscope_configured,
            "enabled": resolved.image_gen_enabled,
            "model": resolved.qwen_image_model,
            "size": resolved.image_gen_size,
            "price_usd_per_image": resolved.image_gen_cost_usd_per_image,
            "price_cny_per_image": resolved.image_gen_cost_cny_per_image,
            "reference_images_available": available,
        }

    @server.tool(description="查看已生成的图片及其花费（免费）。")
    async def list_generated_images(hot_content_id: int | None = None, limit: int = 10) -> dict[str, Any]:
        """Previously generated images, newest first."""
        from app.services.ai.image_service import generation_to_dict, list_generations

        rows, total = await list_generations(
            resolved,
            hot_content_id=int(hot_content_id) if hot_content_id is not None else None,
            limit=max(1, min(int(limit), 50)),
        )
        return {
            "total": total,
            "estimated_usd_total": round(sum(r.estimated_usd for r in rows), 4),
            "items": [generation_to_dict(r) for r in rows],
        }

    # ------------------------------------------------------------ billed actions
    if resolved.mcp_allow_billed:

        @server.tool(
            description=(
                "【计费】调用 TikHub 采集三平台（小红书/微博/抖音）当前热点并入库、去重、"
                "聚合话题。每次约 3 次计费请求。"
            )
        )
        async def run_collection(limit: int | None = None) -> dict[str, Any]:
            """Fetch from TikHub, deduplicate and store. Spends TikHub quota."""
            from app.services.pipeline.hot_pipeline import collect_and_store

            effective = int(limit) if limit else resolved.hot_limit_per_platform
            logger.warning("MCP run_collection limit=%d — billed TikHub calls", effective)
            result = await collect_and_store(effective, settings=resolved)
            return result.as_dict()

        @server.tool(
            description=(
                "【计费】按关键词搜索三个平台（小红书/抖音/微博）的真实帖子并入库，"
                "每个平台 1 次计费请求。与热搜榜单不同，搜索结果带图片和视频信息。"
                "图片会下载到本地素材库（因为平台图片链接会过期）。"
            )
        )
        async def search_topic(
            keyword: str,
            platforms: str | None = None,
            limit: int | None = None,
            download_media: bool = True,
        ) -> dict[str, Any]:
            """Search a topic by keyword. Spends one TikHub call per platform."""
            from app.services.pipeline.task_recorder import run_as_task

            chosen = (
                [part.strip() for part in platforms.split(",") if part.strip()]
                if platforms
                else None
            )
            logger.warning(
                "MCP search_topic keyword=%r platforms=%s — billed TikHub calls",
                keyword,
                chosen or resolved.search_platform_list,
            )

            async def work() -> dict[str, Any]:
                from app.services.pipeline.search_pipeline import collect_by_keyword

                outcome = await collect_by_keyword(
                    keyword,
                    platforms=chosen,
                    limit=int(limit) if limit else None,
                    download_media=download_media,
                    settings=resolved,
                )
                return outcome.as_dict()

            # Recorded here too: an MCP-triggered spend must be as visible as a UI one.
            recorded = await run_as_task(
                task_type="search", step_name="search", work=work, settings=resolved
            )
            return {**recorded.result, "task_id": recorded.task_id}

        @server.tool(
            description=(
                "【计费】按配置的领域关键词（WATCH_KEYWORDS）批量搜索，让科技/AI/编程等"
                "领域内容进入库中。**费用 = 关键词数 × 平台数**，每次调用都会返回实际调用次数。"
            )
        )
        async def run_watch_searches() -> dict[str, Any]:
            """Search every configured domain keyword. Costs keywords x platforms."""
            from app.services.pipeline.task_recorder import run_as_task

            logger.warning(
                "MCP run_watch_searches keywords=%s platforms=%s — %d billed call(s)",
                resolved.watch_keyword_list,
                resolved.watch_platform_list,
                resolved.watch_billed_calls_per_run,
            )

            async def work() -> dict[str, Any]:
                from app.services.pipeline.watch_stage import run_watch_stage

                outcome = await run_watch_stage(resolved)
                return outcome.as_dict()

            recorded = await run_as_task(
                task_type="watch", step_name="watch", work=work, settings=resolved
            )
            return {**recorded.result, "task_id": recorded.task_id}

        @server.tool(
            description=(
                "【计费·最贵】为某条热点生成一张新配图（图片二创）：以它已下载的图片为参考，"
                "按文案方向重新生成一张图。**一次一张、1K 档，约 $0.034/张（≈¥0.25）**，"
                "约等于一次文字二创的 15 倍。结果会立即下载到 media/generated/。"
                "张数与尺寸由策略锁定，本工具不提供参数修改。"
            )
        )
        async def generate_post_image(
            hot_content_id: int,
            goal: str = "",
            caption: str = "",
            overlay_text: str = "",
            keep_subject: bool = True,
        ) -> dict[str, Any]:
            """Generate one image for an item. Billed per image."""
            from app.services.ai.image_service import generate_for_content

            logger.warning(
                "MCP generate_post_image item=%s — one image, about $%.4f",
                hot_content_id,
                resolved.image_gen_cost_usd_per_image,
            )
            outcome = await generate_for_content(
                int(hot_content_id),
                settings=resolved,
                goal=goal,
                caption=caption,
                overlay_text=overlay_text,
                keep_subject=keep_subject,
            )
            return outcome.as_dict()

        @server.tool(description="读取知识标签词表（免费）。词表存库复用，反复读取不花钱。")
        async def list_knowledge_tags(kind: str = "", limit: int = 200) -> dict[str, Any]:
            """The knowledge tag vocabulary — the data behind the 3D tag sphere."""
            from app.services.knowledge_service import list_tags, tag_to_dict

            rows = await list_tags(
                resolved, kind=kind or None, limit=max(1, min(int(limit), 300))
            )
            return {"total": len(rows), "tags": [tag_to_dict(tag) for tag in rows]}

        @server.tool(description="读取已生成的知识科普文章列表（免费，不含正文）。")
        async def list_knowledge_articles(
            topic: str = "", limit: int = 20
        ) -> dict[str, Any]:
            """Generated science articles, newest first."""
            from app.services.knowledge_service import list_articles

            rows, total = await list_articles(
                resolved,
                topic=topic or None,
                limit=max(1, min(int(limit), 50)),
            )
            return {
                "total": total,
                "items": [
                    {
                        "id": record.id,
                        "topic": record.topic,
                        "title": record.title,
                        "difficulty": record.difficulty,
                        "tags": record.tags or [],
                        "image_count": len(record.images or []),
                        "platforms": sorted((record.platforms or {}).keys()),
                        "estimated_cny": record.estimated_cny,
                    }
                    for record in rows
                ],
            }

        @server.tool(description="读取一篇知识科普文章的完整内容（免费）。")
        async def get_knowledge_article(article_id: int) -> dict[str, Any]:
            """One article with its platform drafts and images."""
            from app.services.knowledge_service import article_to_dict, get_article

            record = await get_article(int(article_id), resolved)
            if record is None:
                return {"found": False, "article_id": article_id}
            return {"found": True, "article": article_to_dict(record)}

        @server.tool(
            description=(
                "【计费】为一个知识标签生成科普文章：文章 + 三平台文案 + 配图。"
                "面向大学计算机大类学生。文章 1 次 DeepSeek、三平台 1 次 DeepSeek、"
                "配图默认 1 次 TikHub 搜图（$0.0078，一次多张，比生图便宜 4 倍）。"
            )
        )
        async def generate_knowledge_article(
            topic: str,
            extra: str = "",
            with_platforms: bool = True,
            with_images: bool = True,
            image_count: int = 6,
        ) -> dict[str, Any]:
            """Generate an article for a tag. Billed."""
            from app.services.knowledge_service import generate_article

            logger.warning(
                "MCP generate_knowledge_article topic=%r platforms=%s images=%s — billed",
                topic,
                with_platforms,
                with_images,
            )
            outcome = await generate_article(
                topic,
                settings=resolved,
                extra=extra,
                with_platforms=with_platforms,
                with_images=with_images,
                image_count=max(1, min(int(image_count), 20)),
            )
            return outcome.as_dict()

        @server.tool(
            description=(
                "【计费】重新生成知识标签词表（30-50 个标签，用于知识地图标签球）。"
                "消耗 1 次 DeepSeek 调用。平时读取词表是免费的。"
            )
        )
        async def refresh_knowledge_tags(extra: str = "") -> dict[str, Any]:
            """Regenerate the tag vocabulary. One DeepSeek call."""
            from app.services.knowledge_service import refresh_tag_vocabulary

            logger.warning("MCP refresh_knowledge_tags — one billed DeepSeek call")
            outcome = await refresh_tag_vocabulary(resolved, extra=extra)
            return outcome.as_dict()

        @server.tool(
            description=(
                "【计费】按关键词搜图并下载到素材库，用于给文章配图。"
                "1 次 TikHub 调用（约 $0.0078），一次返回多张，比生成图片便宜 4 倍。"
            )
        )
        async def search_knowledge_images(keyword: str, limit: int = 8) -> dict[str, Any]:
            """Search images and download them. One billed TikHub call."""
            from app.services.tikhub.image_search import search_and_download

            logger.warning(
                "MCP search_knowledge_images keyword=%r — 1 billed TikHub call", keyword
            )
            found, summary = await search_and_download(
                keyword, limit=max(1, min(int(limit), 20)), settings=resolved
            )
            return {
                "keyword": keyword,
                "images": [image.as_dict() for image in found],
                **summary,
            }

        @server.tool(
            description=(
                "【计费】把一份开源项目的 README 转成小红书/微博/抖音三平台推广文案"
                "（面向大学生与编程学习者）。会消耗 DeepSeek token，一次调用生成三份。"
                "只依据 README 里真实存在的内容，不许编造数据或经历。"
            )
        )
        async def generate_readme_promo(
            text: str | None = None,
            path: str | None = None,
            url: str | None = None,
            project_name: str = "",
            extra_note: str = "",
            style_reference_url: str | None = None,
        ) -> dict[str, Any]:
            """Generate promotion copy from a README. Exactly one source must be given.

            ``style_reference_url`` takes a Xiaohongshu note link and uses that note's
            *form* (title length, paragraph rhythm, tone, hashtags) as a reference. The
            link is resolved for free; reading the note costs one TikHub call.
            """
            from app.services.ai.promo_service import generate_promo, promo_to_dict, get_promo

            logger.warning(
                "MCP generate_readme_promo — billed: %s",
                "1 TikHub + 1 DeepSeek call" if style_reference_url else "1 DeepSeek call",
            )
            run = await generate_promo(
                text=text,
                path=path,
                url=url,
                project_name=project_name,
                extra_note=extra_note,
                style_url=style_reference_url,
                settings=resolved,
            )
            payload = run.as_dict()
            if run.record_id:
                record = await get_promo(resolved, run.record_id)
                if record is not None:
                    payload["promo"] = promo_to_dict(record)
            return payload

        @server.tool(
            description=(
                "【计费】用 DeepSeek 分析库中热点并筛选出最值得写的若干条。会消耗 token，"
                "消耗量取决于候选条数（受 ANALYSIS_MAX_CANDIDATES 限制）。"
                "候选**按领域相关性排序**，与账号定位无关的内容不会送去分析。"
            )
        )
        async def run_analysis(limit: int | None = None) -> dict[str, Any]:
            """Analyse and select stored hot items. Spends DeepSeek tokens."""
            from app.services.ai.analyzer import run_analysis as analyse

            logger.warning("MCP run_analysis — billed DeepSeek calls")
            result = await analyse(limit=int(limit) if limit else None, settings=resolved)
            return result.as_dict()

        @server.tool(
            description=(
                "【计费】为已入选的热点生成三平台二创草稿。每条一次 DeepSeek 请求，"
                "消耗 token。生成结果状态为「需人工审核」或「可发布」，不会自动发布。"
            )
        )
        async def run_rewrite(limit: int | None = None) -> dict[str, Any]:
            """Rewrite the selected items into three platform drafts. Spends DeepSeek tokens."""
            from app.services.ai.rewriter import run_rewriting

            logger.warning("MCP run_rewrite — billed DeepSeek calls")
            result = await run_rewriting(limit=int(limit) if limit else None, settings=resolved)
            return result.as_dict()

        @server.tool(
            description=(
                "【计费】按阶段运行完整流水线。阶段可选 fetch（计费）、analyze（计费）、"
                "detail（计费，默认关闭）、rewrite（计费）、notify（免费）。一次调用可能产生"
                "多项费用，是最贵的一个工具。"
            )
        )
        async def run_pipeline(stages: str = "fetch,analyze,rewrite", trigger: str = "mcp") -> dict[str, Any]:
            """Run the pipeline stages in order. Spends TikHub quota and DeepSeek tokens."""
            from app.services.pipeline.runner import BILLED_STAGES, resolve_stages, run_pipeline as pipe

            selected = resolve_stages(stages)
            billed = sorted(set(selected) & BILLED_STAGES)
            logger.warning("MCP run_pipeline stages=%s billed=%s", selected, billed)
            result = await pipe(trigger=trigger, stages=selected, settings=resolved)
            payload = result.as_dict()
            payload["billed_stages"] = billed
            return payload

    return server


def tool_names(settings: Settings | None = None) -> list[str]:
    """The raw tool names this configuration exposes (for diagnostics and tests)."""
    resolved = settings or get_settings()
    free = [
        "get_stats",
        "list_hot",
        "get_topics",
        "get_analysis",
        "get_rewrite",
        "list_rewrites",
        "list_tasks",
        "get_schedule",
        "get_interest_profile",
        "list_readme_promos",
        "get_readme_promo",
        "get_image_estimate",
        "list_generated_images",
        "list_knowledge_tags",
        "list_knowledge_articles",
        "get_knowledge_article",
    ]
    return free + (list(BILLED_TOOL_NAMES) if resolved.mcp_allow_billed else [])

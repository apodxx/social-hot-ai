"""Database access for hot content and topic groups.

Written with portable SQLAlchemy (no dialect-specific ``ON CONFLICT``), because
the same code must run on PostgreSQL in production and on SQLite in the test
suite. With at most a few hundred rows per run, a batched ``SELECT`` followed by
per-row write is both portable and fast enough.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_analysis import AiAnalysisRecord
from app.models.ai_rewrite import AiRewriteRecord, RewriteStatus
from app.models.hot_content import (
    ContentOrigin,
    HotContent,
    HotContentRecord,
    merge_media_downloads,
)
from app.models.task import PipelineTaskRecord, TaskStatus
from app.models.topic_group import TopicGroupRecord
from app.services.pipeline.dedup import normalize_title, normalize_url
from app.services.pipeline.topic_grouping import TopicCluster, platform_name

logger = logging.getLogger(__name__)


@dataclass
class UpsertStats:
    """How many rows were created versus refreshed."""

    inserted: int = 0
    updated: int = 0
    details: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        return {"inserted": self.inserted, "updated": self.updated, **self.details}


@dataclass
class TopicStats:
    """Topic-group bookkeeping for one pass."""

    groups_created: int = 0
    groups_updated: int = 0
    groups_deleted: int = 0
    members_assigned: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "groups_created": self.groups_created,
            "groups_updated": self.groups_updated,
            "groups_deleted": self.groups_deleted,
            "members_assigned": self.members_assigned,
        }


async def upsert_items(session: AsyncSession, items: Sequence[HotContent]) -> UpsertStats:
    """Insert new items and refresh existing ones (dedup layer 1 at the DB level).

    The unique constraint on ``(platform, platform_content_id)`` is the last line
    of defence: even if two runs interleave, the same item cannot be stored twice.
    """
    stats = UpsertStats()
    if not items:
        return stats

    platforms = {item.platform.value for item in items}
    content_ids = {item.platform_content_id for item in items}
    existing_rows = (
        await session.execute(
            select(HotContentRecord).where(
                HotContentRecord.platform.in_(platforms),
                HotContentRecord.platform_content_id.in_(content_ids),
            )
        )
    ).scalars().all()
    existing: dict[tuple[str, str], HotContentRecord] = {
        (row.platform, row.platform_content_id): row for row in existing_rows
    }

    now = datetime.now(timezone.utc)
    for item in items:
        title_normalized = normalize_title(item.title)
        url_normalized = normalize_url(item.url)
        row = existing.get((item.platform.value, item.platform_content_id))
        if row is None:
            session.add(
                HotContentRecord.from_domain(
                    item, title_normalized=title_normalized, url_normalized=url_normalized
                )
            )
            stats.inserted += 1
            continue

        row.title = item.title
        row.description = item.description
        row.author = item.author
        row.author_id = item.author_id
        row.url = item.url
        row.publish_time = item.publish_time
        row.rank = item.rank
        row.hot_value = item.hot_value
        row.likes = item.likes
        row.comments = item.comments
        row.shares = item.shares
        row.collects = item.collects
        row.content_type = item.content_type.value
        row.cover_url = item.cover_url
        row.video_url = item.video_url
        # Merged, not overwritten: a refresh carries no local download paths, and
        # writing it verbatim would forget the material library copy of every image.
        row.media = merge_media_downloads(row.media, item.media)
        row.image_count = item.media.image_count
        # ``origin`` records how the item was *first* discovered and does not flip when
        # a later search also returns it; the keyword does update, so an item can appear
        # both in the hot list and under the search that matched it.
        if item.origin is ContentOrigin.SEARCH and item.source_keyword:
            row.source_keyword = item.source_keyword
        row.raw_data = item.raw_data
        row.title_normalized = title_normalized
        row.url_normalized = url_normalized
        row.updated_at = now
        stats.updated += 1

    await session.flush()
    logger.info("upsert: %d inserted, %d updated", stats.inserted, stats.updated)
    return stats


async def recent_rows(
    session: AsyncSession,
    *,
    hours: int = 48,
    limit: int = 400,
) -> list[HotContentRecord]:
    """Rows from the dedup window, newest first, capped at ``limit``."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(HotContentRecord)
            .where(HotContentRecord.created_at >= cutoff)
            .order_by(HotContentRecord.created_at.desc(), HotContentRecord.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def window_domain_items(
    session: AsyncSession,
    *,
    hours: int = 48,
    limit: int = 400,
) -> list[HotContent]:
    """The dedup window as domain models, skipping any row we cannot map."""
    items: list[HotContent] = []
    for row in await recent_rows(session, hours=hours, limit=limit):
        try:
            items.append(row.to_domain())
        except ValueError:  # pragma: no cover - only reachable after manual edits
            logger.warning("skipping row %s: unknown platform/content_type", row.id)
    return items


async def apply_topic_clusters(
    session: AsyncSession,
    clusters: Iterable[TopicCluster],
) -> TopicStats:
    """Persist ``clusters`` as topic groups and point their members at them.

    A cluster adopts an existing group when one of its members already belongs to
    one (so ids survive across runs); otherwise it matches a group by normalized
    topic, and only then creates a new one. Groups left with no members are
    deleted, which also handles two former groups merging into one.
    """
    stats = TopicStats()
    clusters = list(clusters)
    now = datetime.now(timezone.utc)

    for cluster in clusters:
        candidate_ids = {
            member.topic_group_id
            for member in cluster.members
            if member.topic_group_id is not None
        }
        group: TopicGroupRecord | None = None
        if candidate_ids:
            group = await session.get(TopicGroupRecord, min(candidate_ids))
        if group is None and cluster.topic_normalized:
            group = (
                await session.execute(
                    select(TopicGroupRecord)
                    .where(TopicGroupRecord.topic_normalized == cluster.topic_normalized)
                    .limit(1)
                )
            ).scalars().first()

        if group is None:
            group = TopicGroupRecord(
                topic=cluster.topic or cluster.topic_normalized,
                topic_normalized=cluster.topic_normalized,
                platforms=cluster.platforms,
                created_at=now,
                updated_at=now,
            )
            session.add(group)
            await session.flush()
            stats.groups_created += 1
        else:
            group.topic = cluster.topic or group.topic
            group.topic_normalized = cluster.topic_normalized or group.topic_normalized
            group.platforms = cluster.platforms
            group.updated_at = now
            stats.groups_updated += 1

        for member in cluster.members:
            if member.topic_group_id != group.id:
                member.topic_group_id = group.id
                stats.members_assigned += 1

    await session.flush()
    stats.groups_deleted = await delete_empty_topic_groups(session)
    logger.info("topic groups: %s", stats.as_dict())
    return stats


async def delete_empty_topic_groups(session: AsyncSession) -> int:
    """Remove groups with no members; returns how many went away."""
    empty_ids = (
        await session.execute(
            select(TopicGroupRecord.id)
            .outerjoin(HotContentRecord, HotContentRecord.topic_group_id == TopicGroupRecord.id)
            .group_by(TopicGroupRecord.id)
            .having(func.count(HotContentRecord.id) == 0)
        )
    ).scalars().all()
    if not empty_ids:
        return 0
    await session.execute(delete(TopicGroupRecord).where(TopicGroupRecord.id.in_(empty_ids)))
    return len(empty_ids)


async def list_admin_hot_contents(
    session: AsyncSession,
    *,
    platform: str | None = None,
    keyword: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    recommended: bool | None = None,
    selected: bool | None = None,
    with_images: bool | None = None,
    origin: str | None = None,
    source_keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[HotContentRecord, AiAnalysisRecord | None, AiRewriteRecord | None]], int]:
    """The admin list: hot items with their analysis and rewrite state.

    Returns ``(rows, total)`` where each row is ``(item, analysis, rewrite)`` and
    the latter two may be ``None``. One join serves the whole list so the UI never
    has to issue a request per row — the filters the spec asks for (§30: platform,
    keyword, date, AI-recommendation) are all expressible here.
    """
    conditions = []
    if platform:
        conditions.append(HotContentRecord.platform == platform)
    if keyword:
        conditions.append(HotContentRecord.title.ilike(f"%{keyword}%"))
    if since:
        conditions.append(HotContentRecord.created_at >= since)
    if until:
        conditions.append(HotContentRecord.created_at <= until)
    if recommended is not None:
        conditions.append(AiAnalysisRecord.recommended.is_(recommended))
    if selected is not None:
        conditions.append(AiAnalysisRecord.selected.is_(selected))
    if with_images is not None:
        # ``image_count`` is a stored derivation of ``media``, so this stays a plain
        # indexed comparison instead of a JSON traversal.
        conditions.append(
            HotContentRecord.image_count > 0 if with_images else HotContentRecord.image_count == 0
        )
    if origin:
        conditions.append(HotContentRecord.origin == origin)
    if source_keyword:
        # Exact match, not a title substring: a search that collected 30 items must be
        # able to show all 30. Matching the title returned only the 5 whose title
        # happened to contain the keyword, which reads as "the search lost results".
        conditions.append(HotContentRecord.source_keyword == source_keyword)

    base = (
        select(HotContentRecord, AiAnalysisRecord, AiRewriteRecord)
        .outerjoin(AiAnalysisRecord, AiAnalysisRecord.hot_content_id == HotContentRecord.id)
        .outerjoin(AiRewriteRecord, AiRewriteRecord.hot_content_id == HotContentRecord.id)
        .where(*conditions)
    )
    total = (
        await session.execute(
            select(func.count())
            .select_from(HotContentRecord)
            .outerjoin(AiAnalysisRecord, AiAnalysisRecord.hot_content_id == HotContentRecord.id)
            .where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            base.order_by(
                HotContentRecord.created_at.desc(),
                HotContentRecord.platform.asc(),
                HotContentRecord.rank.asc().nulls_last(),
                HotContentRecord.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [(row[0], row[1], row[2]) for row in rows], int(total)


async def dashboard_stats(session: AsyncSession, *, since: datetime | None = None) -> dict[str, Any]:
    """Everything the dashboard shows, in one query pass.

    ``since`` limits the per-platform counts (the UI passes the start of today);
    the analysis and rewrite totals are lifetime, because "how many are waiting for
    review" is not a same-day question.
    """
    conditions = []
    if since is not None:
        conditions.append(HotContentRecord.created_at >= since)

    per_platform = dict(
        (
            await session.execute(
                select(HotContentRecord.platform, func.count(HotContentRecord.id))
                .where(*conditions)
                .group_by(HotContentRecord.platform)
            )
        ).all()
    )
    total_items = sum(per_platform.values())
    analyses = (
        await session.execute(select(func.count(AiAnalysisRecord.id)))
    ).scalar_one()
    recommended = (
        await session.execute(
            select(func.count(AiAnalysisRecord.id)).where(AiAnalysisRecord.recommended.is_(True))
        )
    ).scalar_one()
    selected = (
        await session.execute(
            select(func.count(AiAnalysisRecord.id)).where(AiAnalysisRecord.selected.is_(True))
        )
    ).scalar_one()
    needs_verification = (
        await session.execute(
            select(func.count(AiAnalysisRecord.id)).where(
                AiAnalysisRecord.needs_verification.is_(True)
            )
        )
    ).scalar_one()
    rewrites = (await session.execute(select(func.count(AiRewriteRecord.id)))).scalar_one()
    ready = (
        await session.execute(
            select(func.count(AiRewriteRecord.id)).where(
                AiRewriteRecord.status == RewriteStatus.READY_TO_PUBLISH.value
            )
        )
    ).scalar_one()
    topic_groups = (
        await session.execute(select(func.count(TopicGroupRecord.id)))
    ).scalar_one()
    latest_task = (
        await session.execute(
            select(PipelineTaskRecord).order_by(PipelineTaskRecord.id.desc()).limit(1)
        )
    ).scalars().first()

    return {
        "since": since.isoformat() if since else None,
        "by_platform": {key: int(value) for key, value in per_platform.items()},
        "total_items": int(total_items),
        "analyses": int(analyses),
        "recommended": int(recommended),
        "selected": int(selected),
        "needs_verification": int(needs_verification),
        "rewrites": int(rewrites),
        "ready_to_publish": int(ready),
        "needs_review": int(rewrites - ready),
        "topic_groups": int(topic_groups),
        "latest_task": (
            {
                "id": latest_task.id,
                "task_type": latest_task.task_type,
                "status": latest_task.status,
                "started_at": latest_task.started_at.isoformat() if latest_task.started_at else None,
                "finished_at": latest_task.finished_at.isoformat() if latest_task.finished_at else None,
                "duration_ms": latest_task.duration_ms,
                "error_message": latest_task.error_message,
            }
            if latest_task is not None
            else None
        ),
    }


async def list_topic_groups(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    cross_platform_only: bool = False,
) -> tuple[list[tuple[TopicGroupRecord, int]], int]:
    """Topic groups with their member counts; returns ``(rows_with_counts, total)``.

    ``cross_platform_only`` means *two or more distinct platforms*, which is
    filtered in Python rather than SQL: ``platforms`` is a JSON column (the
    schema is deliberately portable), so a SQL predicate would be
    dialect-specific. Phase 2 volumes make this a non-issue.
    """
    member_count = func.count(HotContentRecord.id).label("member_count")
    rows = (
        await session.execute(
            select(TopicGroupRecord, member_count)
            .join(HotContentRecord, HotContentRecord.topic_group_id == TopicGroupRecord.id)
            .group_by(TopicGroupRecord.id)
            .order_by(member_count.desc(), TopicGroupRecord.id.desc())
        )
    ).all()
    pairs = [(row[0], int(row[1])) for row in rows]
    if cross_platform_only:
        pairs = [pair for pair in pairs if len(pair[0].platforms or []) > 1]
    total = len(pairs)
    return pairs[offset : offset + limit], total


async def topic_members(session: AsyncSession, group_id: int) -> list[HotContentRecord]:
    """Members of one topic group, best-ranked first."""
    rows = (
        await session.execute(
            select(HotContentRecord)
            .where(HotContentRecord.topic_group_id == group_id)
            .order_by(
                HotContentRecord.hot_value.desc().nulls_last(),
                HotContentRecord.rank.asc().nulls_last(),
            )
        )
    ).scalars().all()
    return list(rows)


async def database_stats(session: AsyncSession) -> dict[str, Any]:
    """Counts for the status endpoint and the run report."""
    per_platform = dict(
        (
            await session.execute(
                select(HotContentRecord.platform, func.count(HotContentRecord.id)).group_by(
                    HotContentRecord.platform
                )
            )
        ).all()
    )
    total_contents = sum(per_platform.values())
    total_groups = (
        await session.execute(select(func.count(TopicGroupRecord.id)))
    ).scalar_one()
    grouped = (
        await session.execute(
            select(func.count(HotContentRecord.id)).where(
                HotContentRecord.topic_group_id.is_not(None)
            )
        )
    ).scalar_one()
    latest = (
        await session.execute(select(func.max(HotContentRecord.created_at)))
    ).scalar_one()
    analyses = (await session.execute(select(func.count(AiAnalysisRecord.id)))).scalar_one()
    selected = (
        await session.execute(
            select(func.count(AiAnalysisRecord.id)).where(AiAnalysisRecord.selected.is_(True))
        )
    ).scalar_one()
    rewrites = (await session.execute(select(func.count(AiRewriteRecord.id)))).scalar_one()
    ready = (
        await session.execute(
            select(func.count(AiRewriteRecord.id)).where(
                AiRewriteRecord.status == RewriteStatus.READY_TO_PUBLISH.value
            )
        )
    ).scalar_one()
    return {
        "hot_contents": int(total_contents),
        "by_platform": {key: int(value) for key, value in per_platform.items()},
        "topic_groups": int(total_groups),
        "grouped_contents": int(grouped),
        "ai_analyses": int(analyses),
        "selected_analyses": int(selected),
        "ai_rewrites": int(rewrites),
        "ready_to_publish": int(ready),
        "latest_created_at": latest.isoformat() if isinstance(latest, datetime) else None,
    }


# =============================================================================
# Phase 3: AI analysis rows
# =============================================================================


async def rows_needing_analysis(
    session: AsyncSession,
    *,
    hours: int = 48,
    limit: int = 400,
    reuse_hours: int = 24,
    only_ids: Sequence[int] | None = None,
) -> list[HotContentRecord]:
    """Recent rows with no analysis, or an analysis older than ``reuse_hours``.

    ``reuse_hours=0`` means "never re-analyse", which is the cheapest setting and
    the one that makes repeated runs idempotent.

    ``only_ids`` is the *explicit request* path — one item the operator clicked
    "AI 分析" on. It deliberately ignores the recency window, the reuse window and the
    "already has an analysis" rule: an explicit click means "do this one, now", and
    silently skipping it because a rule said so would make the button look broken.
    """
    if only_ids is not None:
        if not only_ids:
            return []
        rows = (
            await session.execute(
                select(HotContentRecord).where(HotContentRecord.id.in_(list(only_ids)))
            )
        ).scalars().all()
        return list(rows)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    statement = (
        select(HotContentRecord)
        .outerjoin(AiAnalysisRecord, AiAnalysisRecord.hot_content_id == HotContentRecord.id)
        .where(HotContentRecord.created_at >= cutoff)
    )
    if reuse_hours > 0:
        stale_before = datetime.now(timezone.utc) - timedelta(hours=reuse_hours)
        statement = statement.where(
            (AiAnalysisRecord.id.is_(None)) | (AiAnalysisRecord.updated_at < stale_before)
        )
    else:
        statement = statement.where(AiAnalysisRecord.id.is_(None))
    rows = (
        await session.execute(
            statement.order_by(
                HotContentRecord.hot_value.desc().nulls_last(),
                HotContentRecord.rank.asc().nulls_last(),
                HotContentRecord.id.desc(),
            ).limit(limit)
        )
    ).scalars().all()
    return list(rows)


@dataclass
class AnalysisUpsertStats:
    """How many analysis rows were created or refreshed."""

    inserted: int = 0
    updated: int = 0
    selected: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "selected": self.selected,
        }


async def upsert_analyses(
    session: AsyncSession,
    entries: Sequence[tuple[HotContentRecord, Any, bool]],
    *,
    model: str,
) -> AnalysisUpsertStats:
    """Write one analysis per hot item.

    ``entries`` is ``(row, analysis, selected)``. ``hot_content_id`` is unique, so
    re-analysing updates the existing row instead of piling up versions.
    """
    stats = AnalysisUpsertStats()
    if not entries:
        return stats

    content_ids = [row.id for row, _analysis, _selected in entries]
    existing_rows = (
        await session.execute(
            select(AiAnalysisRecord).where(AiAnalysisRecord.hot_content_id.in_(content_ids))
        )
    ).scalars().all()
    existing = {row.hot_content_id: row for row in existing_rows}

    now = datetime.now(timezone.utc)
    for row, analysis, selected in entries:
        record = existing.get(row.id)
        if record is None:
            record = AiAnalysisRecord(hot_content_id=row.id, created_at=now)
            session.add(record)
            stats.inserted += 1
        else:
            stats.updated += 1
        record.topic_group_id = row.topic_group_id
        record.topic = analysis.topic
        record.summary = analysis.summary
        record.why_hot = analysis.why_hot
        record.content_angle = analysis.content_angle
        record.discussion_points = list(analysis.discussion_points)
        record.account_fit = analysis.account_fit
        record.recommended = bool(analysis.recommended)
        record.confidence = float(analysis.confidence)
        record.needs_verification = bool(analysis.needs_verification)
        record.is_duplicate = bool(analysis.is_duplicate)
        record.selected = bool(selected)
        record.model = model
        record.prompt_tokens = int(getattr(analysis, "prompt_tokens", 0) or 0)
        record.completion_tokens = int(getattr(analysis, "completion_tokens", 0) or 0)
        record.raw_response = getattr(analysis, "raw", {}) or {}
        record.updated_at = now
        if selected:
            stats.selected += 1

    await session.flush()
    logger.info("analyses: %s", stats.as_dict())
    return stats


async def list_analyses(
    session: AsyncSession,
    *,
    selected_only: bool = False,
    recommended_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[AiAnalysisRecord, HotContentRecord]], int]:
    """Analyses joined with their hot item; returns ``(rows, total)``."""
    conditions = []
    if selected_only:
        conditions.append(AiAnalysisRecord.selected.is_(True))
    if recommended_only:
        conditions.append(AiAnalysisRecord.recommended.is_(True))

    total = (
        await session.execute(
            select(func.count(AiAnalysisRecord.id)).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(AiAnalysisRecord, HotContentRecord)
            .join(HotContentRecord, HotContentRecord.id == AiAnalysisRecord.hot_content_id)
            .where(*conditions)
            .order_by(
                AiAnalysisRecord.selected.desc(),
                AiAnalysisRecord.confidence.desc(),
                AiAnalysisRecord.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [(row[0], row[1]) for row in rows], int(total)


async def analysis_for_content(
    session: AsyncSession, hot_content_id: int
) -> tuple[AiAnalysisRecord, HotContentRecord] | None:
    """The analysis of one item, with the item itself."""
    row = (
        await session.execute(
            select(AiAnalysisRecord, HotContentRecord)
            .join(HotContentRecord, HotContentRecord.id == AiAnalysisRecord.hot_content_id)
            .where(AiAnalysisRecord.hot_content_id == hot_content_id)
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def refresh_topic_summaries(session: AsyncSession) -> int:
    """Fill ``topic_groups.summary`` from the best member analysis.

    Phase 2 deliberately left this column empty; this is the Phase 3 step that
    fills it, using the highest-confidence analysis among the group's members
    rather than a generated sentence of its own.
    """
    rows = (
        await session.execute(
            select(
                HotContentRecord.topic_group_id,
                AiAnalysisRecord.summary,
                AiAnalysisRecord.confidence,
            )
            .join(AiAnalysisRecord, AiAnalysisRecord.hot_content_id == HotContentRecord.id)
            .where(HotContentRecord.topic_group_id.is_not(None))
            .order_by(AiAnalysisRecord.confidence.desc(), AiAnalysisRecord.id.asc())
        )
    ).all()
    best: dict[int, str] = {}
    for group_id, summary, _confidence in rows:
        if group_id is not None and summary and group_id not in best:
            best[group_id] = summary

    updated = 0
    for group_id, summary in best.items():
        group = await session.get(TopicGroupRecord, group_id)
        if group is not None and group.summary != summary:
            group.summary = summary
            group.updated_at = datetime.now(timezone.utc)
            updated += 1
    await session.flush()
    logger.info("topic summaries filled: %d", updated)
    return updated


# =============================================================================
# Phase 4: AI rewrite rows
# =============================================================================


@dataclass
class RewriteUpsertStats:
    """How many rewrite rows were created or refreshed."""

    inserted: int = 0
    updated: int = 0
    ready: int = 0
    needs_review: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "ready": self.ready,
            "needs_review": self.needs_review,
        }


async def rows_needing_rewrite(
    session: AsyncSession,
    *,
    limit: int = 10,
    reuse_hours: int = 24,
    only_ids: Sequence[int] | None = None,
) -> list[tuple[HotContentRecord, AiAnalysisRecord]]:
    """Selected analyses that have no rewrite yet, or a stale one.

    Only items Phase 3 **selected** reach this stage, which is what bounds the
    rewriting cost to at most ``ANALYSIS_MAX_SELECTED`` items per run.

    ``only_ids`` is the explicit per-item path (the "二创" button). It drops the
    ``selected`` requirement and the reuse window: the operator asked for *this* item,
    so the pipeline's cost bound does not apply. An analysis is still required — the
    rewrite prompt is built from it — so an item with no analysis simply does not come
    back, and the caller reports that.
    """
    statement = (
        select(HotContentRecord, AiAnalysisRecord)
        .join(AiAnalysisRecord, AiAnalysisRecord.hot_content_id == HotContentRecord.id)
        .outerjoin(AiRewriteRecord, AiRewriteRecord.hot_content_id == HotContentRecord.id)
    )
    if only_ids is not None:
        if not only_ids:
            return []
        rows = (
            await session.execute(
                statement.where(HotContentRecord.id.in_(list(only_ids))).order_by(
                    HotContentRecord.id.asc()
                )
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    statement = statement.where(AiAnalysisRecord.selected.is_(True))
    if reuse_hours > 0:
        stale_before = datetime.now(timezone.utc) - timedelta(hours=reuse_hours)
        statement = statement.where(
            (AiRewriteRecord.id.is_(None)) | (AiRewriteRecord.updated_at < stale_before)
        )
    else:
        statement = statement.where(AiRewriteRecord.id.is_(None))
    rows = (
        await session.execute(
            statement.order_by(
                AiAnalysisRecord.confidence.desc(), HotContentRecord.id.asc()
            ).limit(limit)
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


async def upsert_rewrites(
    session: AsyncSession,
    entries: Sequence[tuple[Any, Any, Any, Any, Any, int]],
    *,
    model: str,
) -> RewriteUpsertStats:
    """Write one rewrite per hot item.

    ``entries`` is ``(row, rewrite, status, risk_flags, copy_check, attempts)``.
    ``hot_content_id`` is unique, so re-running updates rather than duplicating.
    """
    stats = RewriteUpsertStats()
    if not entries:
        return stats

    content_ids = [row.id for row, *_rest in entries]
    existing_rows = (
        await session.execute(
            select(AiRewriteRecord).where(AiRewriteRecord.hot_content_id.in_(content_ids))
        )
    ).scalars().all()
    existing = {row.hot_content_id: row for row in existing_rows}

    now = datetime.now(timezone.utc)
    for row, rewrite, status, flags, check, attempts in entries:
        record = existing.get(row.id)
        if record is None:
            record = AiRewriteRecord(hot_content_id=row.id, created_at=now)
            session.add(record)
            stats.inserted += 1
        else:
            stats.updated += 1

        record.topic_group_id = row.topic_group_id
        record.summary = rewrite.summary
        record.why_hot = rewrite.why_hot
        record.angle = rewrite.angle

        record.xiaohongshu_title = rewrite.xiaohongshu.title
        record.xiaohongshu_content = rewrite.xiaohongshu.content
        record.xiaohongshu_ending = rewrite.xiaohongshu.ending
        record.xiaohongshu_hashtags = list(rewrite.xiaohongshu.hashtags)

        record.weibo_opening = rewrite.weibo.opening
        record.weibo_title = rewrite.weibo.title
        record.weibo_content = rewrite.weibo.content
        record.weibo_hashtags = list(rewrite.weibo.hashtags)

        record.douyin_hook = rewrite.douyin.hook
        record.douyin_script = rewrite.douyin.script
        record.douyin_scene_suggestions = list(rewrite.douyin.scenes)
        record.douyin_subtitles = rewrite.douyin.subtitles
        record.douyin_cta = rewrite.douyin.cta

        record.status = status.value if hasattr(status, "value") else str(status)
        record.risk_flags = list(flags)
        record.needs_verification = bool(rewrite.needs_verification)
        record.verification_note = rewrite.verification_note

        record.model = model
        record.attempts = int(attempts)
        record.copy_similarity = float(check.similarity) if check is not None else 0.0
        # The 图文 plan travels with the drafts it belongs to.
        plan = getattr(rewrite, "image_plan", None)
        record.layout = plan.model_dump(mode="json") if plan is not None else {}
        # Per-row spend, so the most expensive call in the pipeline stays auditable.
        record.prompt_tokens = int(getattr(rewrite, "prompt_tokens", 0) or 0)
        record.completion_tokens = int(getattr(rewrite, "completion_tokens", 0) or 0)
        record.raw_response = rewrite.model_dump(mode="json")
        record.updated_at = now

        if record.status == RewriteStatus.READY_TO_PUBLISH.value:
            stats.ready += 1
        else:
            stats.needs_review += 1

    await session.flush()
    logger.info("rewrites: %s", stats.as_dict())
    return stats


async def list_rewrites(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[AiRewriteRecord, HotContentRecord]], int]:
    """Rewrites joined with their source item; returns ``(rows, total)``."""
    conditions = []
    if status:
        conditions.append(AiRewriteRecord.status == status)

    total = (
        await session.execute(select(func.count(AiRewriteRecord.id)).where(*conditions))
    ).scalar_one()
    rows = (
        await session.execute(
            select(AiRewriteRecord, HotContentRecord)
            .join(HotContentRecord, HotContentRecord.id == AiRewriteRecord.hot_content_id)
            .where(*conditions)
            .order_by(AiRewriteRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [(row[0], row[1]) for row in rows], int(total)


async def rewrite_for_content(
    session: AsyncSession, hot_content_id: int
) -> tuple[AiRewriteRecord, HotContentRecord] | None:
    """The rewrite of one item, with the item itself."""
    row = (
        await session.execute(
            select(AiRewriteRecord, HotContentRecord)
            .join(HotContentRecord, HotContentRecord.id == AiRewriteRecord.hot_content_id)
            .where(AiRewriteRecord.hot_content_id == hot_content_id)
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else None


# =============================================================================
# Phase 5: pipeline task bookkeeping
# =============================================================================


async def fail_stale_tasks(session: AsyncSession, *, stale_minutes: int) -> int:
    """Mark long-running tasks as failed.

    Without this, one crashed run would leave ``running`` behind forever and — since
    a running task blocks new runs — the scheduler would silently stop working.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    stale = (
        await session.execute(
            select(PipelineTaskRecord).where(
                PipelineTaskRecord.status == TaskStatus.RUNNING.value,
                PipelineTaskRecord.started_at < cutoff,
            )
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for task in stale:
        task.status = TaskStatus.FAILED.value
        task.finished_at = now
        task.error_message = (
            f"abandoned (stale): still marked running after {stale_minutes} minutes; "
            "treated as crashed so later runs are not blocked"
        )
        logger.warning("task %s marked failed as stale", task.id)
    if stale:
        await session.flush()
    return len(stale)


async def has_running_task(session: AsyncSession) -> PipelineTaskRecord | None:
    """The currently running task, if any (the duplicate-execution guard)."""
    return (
        await session.execute(
            select(PipelineTaskRecord)
            .where(PipelineTaskRecord.status == TaskStatus.RUNNING.value)
            .order_by(PipelineTaskRecord.id.desc())
            .limit(1)
        )
    ).scalars().first()


async def create_task(session: AsyncSession, task_type: str) -> PipelineTaskRecord:
    """Insert a task row in the ``running`` state."""
    task = PipelineTaskRecord(
        task_type=task_type,
        status=TaskStatus.RUNNING.value,
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        steps=[],
        summary={},
    )
    session.add(task)
    await session.flush()
    logger.info("task %s started (%s)", task.id, task_type)
    return task


async def finish_task(
    session: AsyncSession,
    task: PipelineTaskRecord,
    *,
    status: TaskStatus,
    steps: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    error_message: str | None = None,
) -> PipelineTaskRecord:
    """Close a task row with its per-step report and totals."""
    now = datetime.now(timezone.utc)
    task.status = status.value
    task.finished_at = now
    task.steps = list(steps)
    task.summary = dict(summary)
    task.error_message = error_message
    if task.started_at is not None:
        started = task.started_at
        if started.tzinfo is None:  # SQLite returns naive datetimes
            started = started.replace(tzinfo=timezone.utc)
        task.duration_ms = int((now - started).total_seconds() * 1000)
    await session.flush()
    logger.info(
        "task %s finished: %s in %sms", task.id, task.status, task.duration_ms
    )
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[PipelineTaskRecord], int]:
    """Task history, newest first."""
    conditions = []
    if status:
        conditions.append(PipelineTaskRecord.status == status)
    total = (
        await session.execute(
            select(func.count(PipelineTaskRecord.id)).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(PipelineTaskRecord)
            .where(*conditions)
            .order_by(PipelineTaskRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows), int(total)


# =============================================================================
# Section 十六: fetched detail bodies
# =============================================================================


async def rows_needing_detail(
    session: AsyncSession, *, limit: int = 10
) -> list[HotContentRecord]:
    """Selected items whose body has never been fetched.

    ``detail_fetched_at`` is set even when the extracted text proved too thin to
    use, so an unusable item is not re-fetched — and re-billed — every run.
    """
    statement = (
        select(HotContentRecord)
        .join(AiAnalysisRecord, AiAnalysisRecord.hot_content_id == HotContentRecord.id)
        .where(
            AiAnalysisRecord.selected.is_(True),
            HotContentRecord.detail_fetched_at.is_(None),
        )
        .order_by(AiAnalysisRecord.confidence.desc(), HotContentRecord.id.asc())
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars().all())


async def store_detail(
    session: AsyncSession, hot_content_id: int, detail: Any
) -> HotContentRecord | None:
    """Store fetched detail text and its provenance.

    The text goes into ``description`` — that column exists for the body — and the
    endpoint plus timestamp record where it came from.
    """
    row = await session.get(HotContentRecord, hot_content_id)
    if row is None:
        return None
    if getattr(detail, "usable", False):
        row.description = detail.text
    row.detail_fetched_at = datetime.now(timezone.utc)
    row.detail_endpoint = (getattr(detail, "endpoint", "") or "")[:128]
    await session.flush()
    logger.info(
        "detail stored for item %s: %d chars via %s",
        hot_content_id,
        len(getattr(detail, "text", "") or ""),
        getattr(detail, "endpoint", "?"),
    )
    return row

"""Phase 2 pipeline: fetch -> dedup -> store -> aggregate topics.

This is the module Phase 3 (analysis) and Phase 5 (scheduler) build on: it owns
the transition from "provider JSON" to "rows in PostgreSQL", and it is the only
place that decides what a duplicate is.

Order matters and is deliberate:

1. load the recent database window (bounded by ``DEDUP_WINDOW_HOURS`` and
   ``DEDUP_MAX_COMPARE``) and index it;
2. run dedup layers 1-3 for this batch *against that window and itself*;
3. upsert the survivors;
4. re-read the window (now including the new rows) and cluster it into topic
   groups (layer 4).

Step 4 re-reads rather than reusing the batch because a topic only becomes a
*cross-platform* topic when the other platform's row exists — including rows
stored by an earlier run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.db.repository import (
    UpsertStats,
    apply_topic_clusters,
    recent_rows,
    upsert_items,
    window_domain_items,
)
from app.models.hot_content import HotContent
from app.services.pipeline.dedup import DedupIndex, deduplicate
from app.services.pipeline.topic_grouping import (
    cluster_summary,
    cluster_topics,
)
from app.services.tikhub import TikHubClient, fetch_all_hot

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Everything one collect-and-store pass did."""

    fetched: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    dedup: dict[str, int] = field(default_factory=dict)
    stored: dict[str, int] = field(default_factory=dict)
    topics: dict[str, int] = field(default_factory=dict)
    ungrouped_items: int = 0
    window_items: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "errors": self.errors,
            "dedup": self.dedup,
            "stored": self.stored,
            "topics": self.topics,
            "ungrouped_items": self.ungrouped_items,
            "window_items": self.window_items,
        }


async def store_items(
    items: Sequence[HotContent],
    *,
    settings: Settings | None = None,
    session: AsyncSession | None = None,
) -> PipelineResult:
    """Deduplicate, persist, and re-aggregate ``items``.

    Pass ``session`` to participate in an existing transaction (tests do this);
    otherwise a session scope is opened and committed here.
    """
    if session is not None:
        return await _store(session, items, settings or get_settings())
    async with session_scope(settings) as own_session:
        return await _store(own_session, items, settings or get_settings())


async def _store(
    session: AsyncSession,
    items: Sequence[HotContent],
    settings: Settings,
) -> PipelineResult:
    threshold = settings.dedup_title_similarity
    window_kwargs = {
        "hours": settings.dedup_window_hours,
        "limit": settings.dedup_max_compare,
    }

    index = DedupIndex(threshold=threshold)
    known_items = await window_domain_items(session, **window_kwargs)
    index.extend(known_items)
    known_keys = {(item.platform.value, item.platform_content_id) for item in known_items}
    logger.info("dedup window holds %d known items", len(known_items))

    # Layer 1 is a *refresh*, not a drop. A hot list's rank and hot_value change
    # between runs, so an item we already store must be updated with the new
    # numbers; dropping it here would freeze those columns at first sighting.
    # Only layers 2 and 3 (same content under a different id) genuinely remove.
    refreshes: list[HotContent] = []
    fresh: list[HotContent] = []
    for item in items:
        target = (
            refreshes
            if (item.platform.value, item.platform_content_id) in known_keys
            else fresh
        )
        target.append(item)

    report = deduplicate(fresh, threshold=threshold, index=index)
    upsert: UpsertStats = await upsert_items(session, [*report.kept, *refreshes])

    rows = await recent_rows(session, **window_kwargs)
    clusters, ungrouped = cluster_topics(rows, threshold=threshold)
    topic_stats = await apply_topic_clusters(session, clusters)

    dedup_counts = report.summary()
    dedup_counts.update(
        {
            "input": len(items),
            "kept": len(report.kept) + len(refreshes),
            "removed": len(report.removed),
            "refreshed": len(refreshes),
        }
    )

    result = PipelineResult(
        dedup=dedup_counts,
        stored=upsert.as_dict(),
        topics={**cluster_summary(clusters), **topic_stats.as_dict()},
        ungrouped_items=ungrouped,
        window_items=len(rows),
    )
    logger.info("store complete: %s", result.as_dict())
    return result


async def collect_and_store(
    limit: int,
    *,
    settings: Settings | None = None,
    platforms: str | None = None,
    client: TikHubClient | None = None,
) -> PipelineResult:
    """Fetch from TikHub, then dedup and store.

    **This spends money**: every platform costs at least one billed endpoint
    call. ``errors`` records a platform that failed without hiding the others.
    """
    resolved = settings or get_settings()
    from app.services.tikhub import ADAPTER_CLASSES

    adapters = list(ADAPTER_CLASSES)
    if platforms:
        wanted = {part.strip().lower() for part in platforms.split(",") if part.strip()}
        adapters = [cls for cls in adapters if cls.platform.value in wanted]

    async def _run(active_client: TikHubClient) -> PipelineResult:
        items_by_platform, errors = await fetch_all_hot(
            active_client, limit, adapter_classes=adapters
        )
        all_items: list[HotContent] = []
        for platform_items in items_by_platform.values():
            all_items.extend(platform_items)
        result = await store_items(all_items, settings=resolved)
        result.fetched = {
            platform.value: len(platform_items)
            for platform, platform_items in items_by_platform.items()
        }
        result.errors = errors
        return result

    if client is not None:
        return await _run(client)
    async with TikHubClient(resolved) as active_client:
        return await _run(active_client)

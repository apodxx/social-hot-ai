"""Keyword-driven collection (Phase 9).

The ranking pipeline answers "what is hot right now". This answers "what is being said
about *this*", which is a different question and needed a different entry point:

``collect_by_keyword("露营装备")``
  1. one billed search per platform (Xiaohongshu notes, Douyin posts, Weibo pictures),
  2. the same four-layer dedup the hot path uses, so a search cannot resurrect a row
     the hot path already holds,
  3. storage with ``origin=search`` and the keyword attached,
  4. optional download of the images into the local material library.

The media step is why images are usable at all: provider URLs are signed and expire, so
a stored link is not a stored picture. It runs after the rows are safely committed, and
a download failure never fails the collection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.config import Settings, get_settings
from app.models.hot_content import HotContent
from app.services.media.store import DownloadReport, download_for_bundles
from app.services.pipeline.hot_pipeline import store_items
from app.services.tikhub.client import TikHubClient, TikHubError
from app.services.tikhub.search import SEARCH_ADAPTERS

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """What one keyword search did, in the shape the API and tasks report."""

    keyword: str
    platforms: list[str] = field(default_factory=list)
    fetched: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    dedup: dict[str, Any] = field(default_factory=dict)
    stored: dict[str, int] = field(default_factory=dict)
    topics: dict[str, Any] = field(default_factory=dict)
    images: dict[str, Any] = field(default_factory=dict)
    withheld: int = 0
    billed_calls: int = 0

    @property
    def total_fetched(self) -> int:
        return sum(self.fetched.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "platforms": self.platforms,
            "fetched": self.fetched,
            "total_fetched": self.total_fetched,
            "errors": self.errors,
            "dedup": self.dedup,
            "stored": self.stored,
            "topics": self.topics,
            "images": self.images,
            "withheld": self.withheld,
            "billed_calls": self.billed_calls,
        }


async def search_platforms(
    keyword: str,
    *,
    platforms: Sequence[str] | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
    client: TikHubClient | None = None,
) -> tuple[list[HotContent], SearchResult]:
    """Run the keyword against each platform and return normalised items.

    **Spends one billed TikHub call per platform.** A platform that fails is recorded
    in ``errors`` and does not stop the others.
    """
    resolved = settings or get_settings()
    chosen = [name for name in (platforms or resolved.search_platform_list)]
    unknown = [name for name in chosen if name not in SEARCH_ADAPTERS]
    if unknown:
        raise ValueError(f"unknown platform(s): {', '.join(unknown)}")
    per_platform = int(limit or resolved.search_limit_per_platform)

    result = SearchResult(keyword=keyword, platforms=list(chosen))
    items: list[HotContent] = []
    owns_client = client is None
    active = client or TikHubClient(resolved)
    try:
        for platform in chosen:
            adapter = SEARCH_ADAPTERS[platform](active)
            try:
                found = await adapter.search(keyword, per_platform)
            except TikHubError as exc:
                result.errors[platform] = str(exc)
                logger.error("search %r failed on %s: %s", keyword, platform, exc)
                continue
            result.billed_calls += 1
            result.fetched[platform] = len(found)
            logger.info("search %r on %s: %d normalised items", keyword, platform, len(found))
            items.extend(found)
    finally:
        if owns_client:
            await active.aclose()
    return items, result


async def collect_by_keyword(
    keyword: str,
    *,
    platforms: Sequence[str] | None = None,
    limit: int | None = None,
    download_media: bool | None = None,
    settings: Settings | None = None,
    client: TikHubClient | None = None,
) -> SearchResult:
    """Search, fetch images, then dedup and store — in that order, deliberately.

    Downloading **before** storing means the local paths are already inside the media
    bundle when the row is written, so there is exactly one upsert. The alternative
    (store, then download, then store again) would need to know which items survived
    dedup, and re-upserting the pre-dedup list would re-insert rows dedup had just
    removed. The price is that an image may be fetched for an item dedup then drops;
    the batch budget bounds that, and correctness is worth more than the bandwidth.
    """
    resolved = settings or get_settings()
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("keyword must not be empty")

    items, result = await search_platforms(
        keyword, platforms=platforms, limit=limit, settings=resolved, client=client
    )
    if not items:
        result.dedup = {"input": 0, "kept": 0, "removed": 0}
        result.stored = {"inserted": 0, "updated": 0}
        return result

    should_download = (
        resolved.media_download_enabled if download_media is None else download_media
    )
    if should_download:
        bundles = [item.media for item in items if item.media.has_image]
        if bundles:
            download: DownloadReport = await download_for_bundles(bundles, resolved)
            result.images = download.as_dict()
        else:
            result.images = {"attempted": 0, "downloaded": 0, "note": "no images returned"}
    else:
        result.images = {"attempted": 0, "downloaded": 0, "note": "download disabled"}

    # The same store path the hot pipeline uses, so both origins obey one definition
    # of a duplicate and one topic-aggregation step.
    stored = await store_items(items, settings=resolved)
    result.dedup = stored.dedup
    result.stored = stored.stored
    result.withheld = max(0, len(items) - int(stored.dedup.get("kept", len(items))))
    result.topics = stored.topics

    logger.info("keyword search complete: %s", result.as_dict())
    return result


__all__ = ["SearchResult", "collect_by_keyword", "search_platforms"]

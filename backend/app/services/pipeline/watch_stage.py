"""The ``watch`` pipeline stage: scheduled domain searches (Phase 10).

Interest *scoring* can only reorder what the boards already contain, and the boards are
dominated by entertainment. This stage is the other half: it searches the operator's
own keywords so technology/programming material arrives even when it never trends.

**It is the most expensive stage per run**, and that is deliberate and visible: the
number of billed calls is exactly ``keywords x platforms``, it is logged at startup, in
the run report and in the task record, and the whole stage is behind
``WATCH_SEARCH_ENABLED``. A recurring charge nobody can see is a charge nobody can
control.

Results are stored like any other search hit (``origin=search`` + ``source_keyword``),
so they flow through the same dedup, analysis, rewriting and notification path — and
their images land in the material library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.services.pipeline.search_pipeline import collect_by_keyword

logger = logging.getLogger(__name__)


@dataclass
class WatchResult:
    """What one watch pass did, per keyword."""

    enabled: bool = True
    keywords: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    per_keyword: list[dict[str, Any]] = field(default_factory=list)
    billed_calls: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    images_downloaded: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "keywords": self.keywords,
            "platforms": self.platforms,
            "billed_calls": self.billed_calls,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "images_downloaded": self.images_downloaded,
            "skipped": self.skipped,
            "per_keyword": self.per_keyword,
            "errors": self.errors[:10],
        }


async def run_watch_stage(
    settings: Settings | None = None, *, client: Any | None = None
) -> WatchResult:
    """Search every configured keyword and store the results.

    A keyword that fails is recorded and the others still run: one bad query must not
    lose the rest of the pass, the same contract the fetch stage follows.
    """
    resolved = settings or get_settings()
    result = WatchResult(platforms=resolved.watch_platform_list)

    if not resolved.watch_search_enabled:
        result.enabled = False
        result.skipped = 1
        logger.info("watch stage skipped: WATCH_SEARCH_ENABLED is false")
        return result

    keywords = resolved.watch_keyword_list
    result.keywords = keywords
    if not keywords:
        result.enabled = False
        result.skipped = 1
        logger.info("watch stage skipped: no WATCH_KEYWORDS configured")
        return result

    planned = len(keywords) * len(result.platforms)
    logger.warning(
        "watch stage: searching %d keyword(s) on %s — %d billed TikHub call(s)",
        len(keywords),
        ", ".join(result.platforms),
        planned,
    )

    for keyword in keywords:
        try:
            outcome = await collect_by_keyword(
                keyword,
                platforms=result.platforms,
                limit=resolved.watch_search_limit,
                download_media=resolved.watch_download_media,
                settings=resolved,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001 - one keyword must not stop the pass
            result.errors.append(f"{keyword}: {type(exc).__name__}: {exc}")
            logger.error("watch stage: keyword %r failed: %s", keyword, exc)
            continue

        payload = outcome.as_dict()
        result.per_keyword.append(
            {
                "keyword": keyword,
                "fetched": payload.get("total_fetched", 0),
                "stored": payload.get("stored", {}),
                "images": payload.get("images", {}),
                "billed_calls": payload.get("billed_calls", 0),
                "errors": payload.get("errors", {}),
            }
        )
        result.billed_calls += int(payload.get("billed_calls", 0))
        result.fetched += int(payload.get("total_fetched", 0))
        stored = payload.get("stored") or {}
        result.inserted += int(stored.get("inserted", 0) or 0)
        result.updated += int(stored.get("updated", 0) or 0)
        images = payload.get("images") or {}
        result.images_downloaded += int(images.get("downloaded", 0) or 0)
        for platform, message in (payload.get("errors") or {}).items():
            result.errors.append(f"{keyword}/{platform}: {message}")

    logger.info("watch stage complete: %s", result.as_dict())
    return result


__all__ = ["WatchResult", "run_watch_stage"]

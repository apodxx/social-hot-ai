"""Section 十六 pipeline stage: fetch the body behind each selected item.

Kept separate from :mod:`app.services.pipeline.runner` so the orchestration stays
readable, and because this stage is the only one whose *existence* is optional:
it is off by default (``DETAIL_FETCH_ENABLED``) since every item costs a billed
TikHub call.

Two behaviours are deliberate:

* **A failed fetch is not fatal.** The item keeps its headline, is recorded as
  failed, and the run continues — a missing body degrades the rewrite, it does not
  invalidate the run.
* **A successful fetch always records provenance**, even when the extracted text
  turns out to be too thin to use (a hashtag-only note, for instance). Otherwise
  the same unusable item would be re-fetched — and re-billed — on every run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.db.database import session_scope
from app.db.repository import rows_needing_detail, store_detail
from app.services.tikhub.client import TikHubClient, TikHubError
from app.services.tikhub.details import DetailResult, fetch_detail

logger = logging.getLogger(__name__)


@dataclass
class DetailStageResult:
    """What the detail stage did, for the task report."""

    considered: int = 0
    fetched: int = 0
    usable: int = 0
    failed: int = 0
    skipped: int = 0
    billed_calls: int = 0
    errors: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "fetched": self.fetched,
            "usable": self.usable,
            "failed": self.failed,
            "skipped": self.skipped,
            "billed_calls": self.billed_calls,
            "errors": self.errors,
            "samples": self.samples,
        }


async def run_detail_stage(
    settings: Settings,
    *,
    client: TikHubClient | None = None,
) -> DetailStageResult:
    """Fetch and store detail text for selected items that lack it."""
    result = DetailStageResult()
    if not settings.detail_fetch_enabled:
        result.skipped = 1
        result.errors.append(
            "DETAIL_FETCH_ENABLED is false: rewriting will run from headlines only "
            "(each detail fetch is a billed TikHub call)"
        )
        logger.info("detail stage skipped: DETAIL_FETCH_ENABLED is false")
        return result

    async with session_scope(settings) as session:
        rows = await rows_needing_detail(session, limit=settings.detail_max_items)
        # Detach the values we need: the fetch is slow and we do not want to hold
        # a transaction open across network calls.
        subjects = [
            {
                "id": row.id,
                "platform": row.platform,
                "title": row.title,
                "platform_content_id": row.platform_content_id,
                "raw_data": row.raw_data or {},
            }
            for row in rows
        ]
    result.considered = len(subjects)
    if not subjects:
        logger.info("detail stage: nothing to fetch (every selected item already has a body)")
        return result

    owns_client = client is None
    active_client = client or TikHubClient(settings)
    try:
        for subject in subjects:
            item = _Subject(subject)
            try:
                detail = await fetch_detail(active_client, item)
            except TikHubError as exc:
                result.failed += 1
                result.errors.append(f"{subject['id']} ({subject['platform']}): {exc}")
                logger.warning("detail fetch failed for %s: %s", subject["id"], exc)
                continue
            result.billed_calls += 1
            if detail is None:
                result.skipped += 1
                result.errors.append(
                    f"{subject['id']} ({subject['platform']}): no detail route for this item"
                )
                continue
            result.fetched += 1
            if detail.usable:
                result.usable += 1
            async with session_scope(settings) as session:
                await store_detail(session, subject["id"], detail)
            if len(result.samples) < 3:
                result.samples.append({"id": subject["id"], **detail.as_dict()})
    finally:
        if owns_client:
            await active_client.aclose()

    logger.info("detail stage: %s", {k: v for k, v in result.as_dict().items() if k != "samples"})
    return result


class _Subject:
    """The minimal item shape :func:`fetch_detail` needs, rebuilt from a row.

    Built explicitly rather than passing the ORM object so no database attributes
    can be touched lazily after the session closed.
    """

    __slots__ = ("id", "platform", "title", "platform_content_id", "raw_data", "hot_value", "rank")

    def __init__(self, values: dict[str, Any]) -> None:
        self.id = values["id"]
        self.platform = values["platform"]
        self.title = values["title"]
        self.platform_content_id = values["platform_content_id"]
        self.raw_data = values["raw_data"]
        self.hot_value = None
        self.rank = None


__all__ = ["DetailStageResult", "run_detail_stage"]

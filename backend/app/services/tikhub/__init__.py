"""TikHub service package.

Public surface: the client, the three platform adapters, and
:func:`fetch_all_hot` — the aggregation entry point Phase 2's deduplication and
Phase 3's analysis will consume.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from app.models.hot_content import HotContent, Platform
from app.services.tikhub.base import BasePlatformAdapter
from app.services.tikhub.client import (
    TikHubAuthError,
    TikHubClient,
    TikHubError,
    TikHubNetworkError,
    TikHubNotFoundError,
    TikHubRateLimitError,
    TikHubResponseError,
    TikHubServerError,
)
from app.services.tikhub.douyin import DouyinAdapter
from app.services.tikhub.weibo import WeiboAdapter
from app.services.tikhub.xiaohongshu import XiaohongshuAdapter

logger = logging.getLogger(__name__)

#: Every Phase 1 platform. Order matches the API response keys.
ADAPTER_CLASSES: tuple[type[BasePlatformAdapter], ...] = (
    XiaohongshuAdapter,
    WeiboAdapter,
    DouyinAdapter,
)

__all__ = [
    "ADAPTER_CLASSES",
    "DouyinAdapter",
    "TikHubAuthError",
    "TikHubClient",
    "TikHubError",
    "TikHubNetworkError",
    "TikHubNotFoundError",
    "TikHubRateLimitError",
    "TikHubResponseError",
    "TikHubServerError",
    "WeiboAdapter",
    "XiaohongshuAdapter",
    "fetch_all_hot",
    "fetch_platform_hot",
]


async def fetch_platform_hot(
    client: TikHubClient,
    adapter_class: type[BasePlatformAdapter],
    limit: int,
) -> list[HotContent]:
    """Fetch one platform in isolation (used by tests and future tuning)."""
    adapter = adapter_class(client)
    logger.info("fetching %s hot content (limit=%d)", adapter.platform.value, limit)
    items = await adapter.fetch_hot(limit)
    logger.info("%s returned %d items", adapter.platform.value, len(items))
    return items


async def fetch_all_hot(
    client: TikHubClient,
    limit: int,
    *,
    adapter_classes: Sequence[type[BasePlatformAdapter]] = ADAPTER_CLASSES,
) -> tuple[dict[Platform, list[HotContent]], dict[str, str]]:
    """Fetch every platform concurrently.

    One platform failing must not affect the others, so each runs isolated and
    its error is reported per platform:

    :returns: ``(items_by_platform, errors_by_platform_name)``. A platform whose
        fetch failed is absent from the first mapping rather than present-empty.
    """
    adapters = [cls(client) for cls in adapter_classes]
    outcomes: list[Any] = await asyncio.gather(
        *(adapter.fetch_hot(limit) for adapter in adapters),
        return_exceptions=True,
    )

    items_by_platform: dict[Platform, list[HotContent]] = {}
    errors: dict[str, str] = {}
    for adapter, outcome in zip(adapters, outcomes):
        name = adapter.platform.value
        if isinstance(outcome, BaseException):
            # Defensive: a bug in one adapter must not kill the whole run.
            detail = f"{type(outcome).__name__}: {outcome}"
            logger.error("%s adapter failed: %s", name, detail)
            errors[name] = detail
            continue
        items_by_platform[adapter.platform] = outcome
        logger.info("%s adapter produced %d items", name, len(outcome))
    return items_by_platform, errors

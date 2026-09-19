"""Weibo adapter.

Endpoint (confirmed against TikHub's published OpenAPI document, 1047 paths):
``GET /api/v1/weibo/web_v2/fetch_hot_search_summary`` — summary reads
"获取微博完整热搜榜单(50条)", i.e. the complete 50-item ranking in one call and no
parameters. The documented fallback ``GET /api/v1/weibo/app/fetch_hot_search``
takes ``category``/``page``/``count``/``region_name`` when the primary fails.

Weibo's hot list is a keyword ranking, so ``rank`` is the position in the
returned ranking and the content type is a topic — not a fabricated note.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.hot_content import ContentType, HotContent, Platform
from app.services.tikhub.base import (
    HotListAdapter,
    as_int,
    as_text,
    find_item_list,
    first_of,
)
from app.services.tikhub.client import TikHubError

logger = logging.getLogger(__name__)


class WeiboAdapter(HotListAdapter):
    """Fetches the Weibo hot-search ranking."""

    platform = Platform.WEIBO

    PRIMARY_PATH = "/api/v1/weibo/web_v2/fetch_hot_search_summary"
    FALLBACK_PATH = "/api/v1/weibo/app/fetch_hot_search"

    #: Preferred item-list locations; the generic search is the safety net.
    #: A real capture put the ranking at ``data.data`` (52 items, ``total: 52``).
    ITEM_PATH_HINTS = ("data.data", "data.0", "data.band_list", "data.realtime", "data")

    #: The endpoint returns ``keyword_url`` as a site-relative path.
    SITE_BASE = "https://s.weibo.com"

    async def fetch_hot(self, limit: int) -> list[HotContent]:
        """Return up to ``limit`` Weibo hot-search entries."""
        try:
            payload = await self.client.get_json(self.PRIMARY_PATH)
        except TikHubError as exc:
            logger.warning("weibo: primary endpoint failed (%s); using fallback", exc)
            payload = await self.client.get_json(
                self.FALLBACK_PATH,
                {"category": "realtimehot", "page": 1, "count": limit},
            )
        items = find_item_list(payload, path_hint=self.ITEM_PATH_HINTS)
        logger.info("weibo: %d raw items, top-level keys=%s", len(items), sorted(payload.keys()))
        return self.normalize(items)[:limit]

    def normalize(self, items: list[dict[str, Any]]) -> list[HotContent]:
        """Map raw weibo entries onto :class:`HotContent`.

        Confirmed against a real capture: an entry is
        ``{rank, is_top, keyword, keyword_url, tag, heat}``.
        """
        results: list[HotContent] = []
        for position, item in enumerate(items, start=1):
            # ``keyword`` is the real title field; the rest are kept as harmless
            # alternatives in case the endpoint's shape ever shifts.
            title = as_text(
                first_of(item, ("keyword", "word", "title", "name", "note", "word_scheme"))
            )
            if not title:
                logger.debug("weibo: skipping an entry with no usable title")
                continue
            results.append(
                self._build(
                    item,
                    # List order, not the payload's ``rank``: real data shows
                    # multiple pinned entries sharing rank 0, so the provider's
                    # rank is not unique. It stays in raw_data.
                    rank=position,
                    content_id=self._content_id(
                        item, ("mid", "id", "word_id", "note_id", "keyword"), fallback=title
                    ),
                    title=title,
                    description=as_text(
                        first_of(item, ("note", "desc", "description", "word_scheme"))
                    ),
                    # ``heat`` exists but arrived empty for all 52 entries, so
                    # weibo hot-search currently yields no heat number.
                    hot_value=as_int(
                        first_of(item, ("heat", "num", "raw_hot", "hot_value", "hot_num"))
                    ),
                    url=self._absolute_url(item),
                    author=as_text(
                        first_of(item, ("author", "user.screen_name", "user.name", "nickname"))
                    ),
                    author_id=as_text(first_of(item, ("author_id", "user.id", "uid"))),
                    content_type=ContentType.TOPIC,
                    cover_url=as_text(first_of(item, ("cover", "pic", "picture", "thumbnail"))),
                )
            )
        return results

    def _absolute_url(self, item: dict[str, Any]) -> str | None:
        """The provider's own link, absolutised.

        ``keyword_url`` is site-relative (``/weibo?q=%23...%23``), so the site
        base is prepended. Nothing is constructed beyond that: when the field is
        absent the result is ``None``.
        """
        raw = as_text(first_of(item, ("keyword_url", "url", "scheme", "link", "note_url")))
        if not raw:
            return None
        if raw.startswith("//"):
            return f"https:{raw}"
        if raw.startswith("/"):
            return f"{self.SITE_BASE}{raw}"
        return raw

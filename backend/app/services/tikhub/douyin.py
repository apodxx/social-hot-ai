"""Douyin adapter.

Endpoints (confirmed against TikHub's published OpenAPI document):

* ``GET /api/v1/douyin/app/v3/fetch_hot_search_list`` — "获取抖音热搜榜数据", the
  hot-search board; ``board_type`` defaults to ``"0"`` and no parameter is
  required, so one call returns the whole board.
* ``GET /api/v1/douyin/billboard/fetch_hot_total_list`` — the documented
  fallback; requires ``page``/``page_size`` and ``type``, whose allowed values
  the spec gives as ``snapshot``/``range`` (defaults in the method signature).

``type`` is taken from the spec's own examples rather than guessed.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.hot_content import ContentType, HotContent, Platform
from app.services.tikhub.base import (
    HotListAdapter,
    as_int,
    as_text,
    as_datetime,
    dig,
    find_item_list,
    first_of,
)
from app.services.tikhub.client import TikHubError

logger = logging.getLogger(__name__)


class DouyinAdapter(HotListAdapter):
    """Fetches the Douyin hot-search board."""

    platform = Platform.DOUYIN

    PRIMARY_PATH = "/api/v1/douyin/app/v3/fetch_hot_search_list"
    FALLBACK_PATH = "/api/v1/douyin/billboard/fetch_hot_total_list"

    #: ``type`` values documented in the OpenAPI parameter examples.
    BILLBOARD_TYPE = "snapshot"

    ITEM_PATH_HINTS = (
        "data.word_list",
        "data.list",
        "data.data",
        "data.0",
        "data",
    )

    async def fetch_hot(self, limit: int) -> list[HotContent]:
        """Return up to ``limit`` Douyin hot-search entries."""
        try:
            payload = await self.client.get_json(self.PRIMARY_PATH, {"board_type": "0"})
        except TikHubError as exc:
            logger.warning("douyin: primary endpoint failed (%s); using billboard fallback", exc)
            payload = await self.client.get_json(
                self.FALLBACK_PATH,
                {"page": 1, "page_size": max(limit, 10), "type": self.BILLBOARD_TYPE},
            )
        items = find_item_list(payload, path_hint=self.ITEM_PATH_HINTS)
        logger.info("douyin: %d raw items, top-level keys=%s", len(items), sorted(payload.keys()))
        return self.normalize(items)[:limit]

    def normalize(self, items: list[dict[str, Any]]) -> list[HotContent]:
        """Map raw douyin entries onto :class:`HotContent`."""
        results: list[HotContent] = []
        for position, item in enumerate(items, start=1):
            title = as_text(
                first_of(item, ("word", "sentence", "title", "hot_word", "name", "desc"))
            )
            if not title:
                logger.debug("douyin: skipping an entry with no usable title")
                continue
            # List order IS the ranking. The provider's own ``position`` is
            # deliberately not used as ``rank``: real captures show it
            # restarting per board section, which produced duplicate ranks
            # (two different items both reporting position 1). It is preserved
            # in raw_data for anyone who wants it.
            rank = position
            content_id = self._content_id(
                item, ("word_id", "aweme_id", "id", "item_id", "group_id", "sentence_id"),
                fallback=title,
            )
            results.append(
                self._build(
                    item,
                    rank=rank,
                    content_id=content_id,
                    title=title,
                    description=as_text(first_of(item, ("desc", "description", "sentence"))),
                    hot_value=as_int(
                        first_of(item, ("hot_value", "value", "hot_num", "hotness", "score"))
                    ),
                    likes=as_int(first_of(item, ("like_count", "digg_count", "likes"))),
                    comments=as_int(first_of(item, ("comment_count", "comments"))),
                    shares=as_int(first_of(item, ("share_count", "shares"))),
                    collects=as_int(first_of(item, ("collect_count", "collects"))),
                    url=as_text(first_of(item, ("url", "share_url", "link"))),
                    publish_time=as_datetime(
                        first_of(
                            item,
                            ("event_time", "create_time", "publish_time", "release_time"),
                        )
                    ),
                    # Confirmed key paths from a real capture: the board returns
                    # ``word_cover.url_list`` (a list) and ``word_cover.uri``.
                    cover_url=as_text(
                        first_of(
                            item,
                            (
                                "word_cover.url_list.0",
                                "word_cover.uri",
                                "cover_url",
                                "cover",
                                "aweme.cover.url_list.0",
                            ),
                        )
                    ),
                    video_url=as_text(
                        first_of(item, ("video_url", "aweme.video.play_addr.url_list.0"))
                    ),
                    content_type=self._content_type(item),
                )
            )
        return results

    @staticmethod
    def _content_type(item: dict[str, Any]) -> ContentType:
        """Video when the payload carries a video, else treat it as a topic."""
        if dig(item, "aweme.video.play_addr.url_list.0") or first_of(
            item, ("video_url", "aweme_id", "item_id")
        ):
            return ContentType.VIDEO
        return ContentType.TOPIC

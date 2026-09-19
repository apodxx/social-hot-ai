"""Xiaohongshu adapter.

**There is no Xiaohongshu hot-list endpoint in TikHub.** All 45 published
``/api/v1/xiaohongshu/*`` paths were enumerated from the OpenAPI document; the
only candidates are:

* ``app_v2/get_creator_hot_inspiration_feed`` — creator-centre inspiration,
  i.e. an operator-facing surface, not a public ranking.
* ``app_v2/get_topic_feed`` — a topic's note list, which needs a topic id.
* ``web_v3/fetch_homefeed`` — the homepage recommendation feed.

Per the project spec's rule ("if TikHub has no hot list, combine real
endpoints instead of inventing one"), Phase 1 uses ``fetch_homefeed``. Two
consequences are stated rather than hidden:

1. This is a **recommendation feed, not a ranked hot list**. ``rank`` is the
   feed position and is documented as such.
2. ``num`` is capped at **40** by the API, so a 50-item request needs a second
   paginated call — an extra billed request, logged when it happens.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.hot_content import ContentType, HotContent, Platform
from app.services.tikhub.base import (
    HotListAdapter,
    as_int,
    as_text,
    dig,
    find_item_list,
    first_of,
)

logger = logging.getLogger(__name__)


class XiaohongshuAdapter(HotListAdapter):
    """Fetches Xiaohongshu's homepage recommendation feed."""

    platform = Platform.XIAOHONGSHU

    PATH = "/api/v1/xiaohongshu/web_v3/fetch_homefeed"

    #: Documented maximum for the ``num`` parameter (OpenAPI schema).
    MAX_PER_CALL = 40

    #: Hard cap on paginated calls, so one run can never fan out.
    MAX_PAGES = 2

    ITEM_PATH_HINTS = ("data.items", "data.0", "data.data", "data")

    async def fetch_hot(self, limit: int) -> list[HotContent]:
        """Return up to ``limit`` Xiaohongshu feed items."""
        collected: list[dict[str, Any]] = []
        cursor_score = ""
        for page in range(1, self.MAX_PAGES + 1):
            remaining = limit - len(collected)
            if remaining <= 0:
                break
            if page > 1:
                logger.info(
                    "xiaohongshu: requesting page %d because num is capped at %d "
                    "(this is an extra billed call)",
                    page,
                    self.MAX_PER_CALL,
                )
            payload = await self.client.get_json(
                self.PATH,
                {
                    "num": min(remaining, self.MAX_PER_CALL),
                    "cursor_score": cursor_score or None,
                    "category": "homefeed_recommend",
                },
            )
            items = find_item_list(payload, path_hint=self.ITEM_PATH_HINTS)
            logger.info(
                "xiaohongshu: page %d returned %d raw items, top-level keys=%s",
                page,
                len(items),
                sorted(payload.keys()),
            )
            if not items:
                break
            collected.extend(items)
            cursor_score = as_text(
                first_of(payload, ("data.cursor_score", "data.cursor", "cursor_score"))
            ) or ""
            if not cursor_score:
                break

        return self.normalize(collected)[:limit]

    def normalize(self, items: list[dict[str, Any]]) -> list[HotContent]:
        """Map raw feed entries onto :class:`HotContent`."""
        results: list[HotContent] = []
        for position, item in enumerate(items, start=1):
            # A feed card often nests the note one level down.
            card = item if isinstance(item.get("note_card"), dict) else item
            note_card = card.get("note_card") if isinstance(card.get("note_card"), dict) else {}
            title = as_text(
                first_of(card, ("display_title", "title", "note_card.display_title", "desc"))
                or first_of(note_card, ("display_title", "title", "desc"))
            )
            if not title:
                logger.debug("xiaohongshu: skipping an entry with no usable title")
                continue
            user = note_card.get("user") if isinstance(note_card.get("user"), dict) else {}
            interact = (
                note_card.get("interact_info")
                if isinstance(note_card.get("interact_info"), dict)
                else {}
            )
            content_id = self._content_id(
                card, ("id", "note_id", "note_card.note_id", "note_card.id"), fallback=title
            )
            results.append(
                self._build(
                    item,
                    rank=position,
                    content_id=content_id,
                    title=title,
                    url=self._note_url(content_id, card),
                    description=as_text(first_of(card, ("desc", "note_card.desc"))),
                    author=as_text(
                        first_of(card, ("user.nickname", "note_card.user.nickname", "nickname"))
                        or user.get("nickname")
                    ),
                    author_id=as_text(
                        first_of(card, ("user.user_id", "note_card.user.user_id", "user_id"))
                        or user.get("user_id")
                    ),
                    likes=as_int(
                        first_of(card, ("liked_count", "note_card.interact_info.liked_count"))
                        or interact.get("liked_count")
                    ),
                    collects=as_int(
                        first_of(card, ("collected_count", "note_card.interact_info.collected_count"))
                        or interact.get("collected_count")
                    ),
                    comments=as_int(
                        first_of(card, ("comment_count", "note_card.interact_info.comment_count"))
                        or interact.get("comment_count")
                    ),
                    cover_url=as_text(
                        first_of(
                            card,
                            (
                                "cover.url_default",
                                "note_card.cover.url_default",
                                "cover.url",
                                "note_card.cover.url",
                            ),
                        )
                    ),
                    video_url=as_text(
                        first_of(
                            card,
                            (
                                "video.media.stream.h264.0.master_url",
                                "note_card.video.media.stream.h264.0.master_url",
                            ),
                        )
                    ),
                    content_type=self._content_type(card, note_card),
                )
            )
        return results

    @staticmethod
    def _note_url(content_id: str, card: dict[str, Any]) -> str | None:
        """The note's canonical URL.

        The feed card carries no URL field (confirmed against a real capture), so
        when a genuine note id is present the public note URL is **derived**
        here and marked as derived: it points at an existing note rather than
        inventing data. With only a synthetic id, nothing is returned.
        """
        provider_url = as_text(first_of(card, ("url", "share_url", "note_card.url")))
        if provider_url:
            return provider_url
        if content_id and not content_id.startswith("synth-"):
            return f"https://www.xiaohongshu.com/explore/{content_id}"
        return None

    @staticmethod
    def _content_type(card: dict[str, Any], note_card: dict[str, Any]) -> ContentType:
        """Video note when a video stream is present, else a plain note."""
        if dig(card, "video.media.stream.h264.0.master_url") or dig(
            note_card, "video.media.stream.h264.0.master_url"
        ):
            return ContentType.VIDEO
        if as_text(first_of(card, ("type", "note_card.type"))) == "video":
            return ContentType.VIDEO
        return ContentType.NOTE

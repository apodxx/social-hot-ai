"""Keyword search adapters (Phase 9, spec section 十二 "热点搜索").

Why this file exists: the collection path until now could only read *fixed* surfaces —
Weibo's hot-search ranking, Douyin's hot-search ranking, Xiaohongshu's home feed. None
of them can be pointed at a topic the operator cares about, and two of the three
return **keywords**, not posts, so they carry no images and no video at all.

Every field below was read from a real captured response (see
``scripts/discover_search.py`` and ``tests/fixtures/raw/search_*.json``); TikHub
publishes no response schemas, so nothing here is guessed.

Three endpoints, three shapes, and one shared consequence:

* **Xiaohongshu** ``app_v2/search_notes`` (GET, ``keyword``) → ``data.data.items[]``;
  ``note.type`` is ``normal`` or ``video``, and ``note.images_list[]`` carries the
  **full** image set (1-7 images, verified 20/20 items) with a 1440w ``url_size_large``.
  Because the list is already complete, images do **not** require a second billed
  detail call — which is exactly the kind of thing worth checking before building.
* **Douyin** ``search/fetch_general_search_v2`` (**POST, JSON body**) →
  ``data.business_data[]``; ``aweme_type`` 68 is an image post (``images[].url_list``)
  and 0 is a video (``video.play_addr`` + ``video.cover``). V2 is used rather than V3
  because only V2 exposes the ``content_type`` filter.
* **Weibo** ``web_v2/fetch_pic_search`` (GET, ``query``) → ``data.pic_list[]``, each with
  a real image URL in ``original_pic``. This closes a gap found earlier: the weibo
  *realtime search* endpoint we already call returns only a ``has_image`` boolean and
  **no image URLs at all**.

Provider URLs are signed and expire (``…?sign=…&t=…``), which is why the media library
downloads rather than merely linking.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.models.hot_content import (
    ContentOrigin,
    ContentType,
    HotContent,
    MediaBundle,
    MediaImage,
    MediaVideo,
    Platform,
)
from app.services.tikhub.base import BasePlatformAdapter, as_int, as_text, dig, first_of

logger = logging.getLogger(__name__)

XIAOHONGSHU_SEARCH = "/api/v1/xiaohongshu/app_v2/search_notes"
DOUYIN_SEARCH = "/api/v1/douyin/search/fetch_general_search_v2"
WEIBO_PIC_SEARCH = "/api/v1/weibo/web_v2/fetch_pic_search"

#: Douyin ``aweme_type`` for an image post. 0 (and most other values) are video.
DOUYIN_IMAGE_TYPE = 68

#: Format hints douyin's CDN puts in the URL, worst-first for our purposes.
#:
#: Douyin's ``url_list`` carries the same picture twice — once as ``…:q80.heic`` and
#: once as ``…:q80.jpeg`` — and taking index 0 blindly (the first version of this
#: adapter did) downloads HEIC, which **no browser can display**. Since the whole point
#: of the material library is that the operator can look at and use the pictures,
#: the web-friendly variant is chosen explicitly.
UNWEB_FRIENDLY_SUFFIXES = (".heic", ".heif", ".avif")

#: Preferred, in order, when a provider offers several encodings.
WEB_FRIENDLY_SUFFIXES = (".jpeg", ".jpg", ".png", ".webp")


def _https(url: str | None) -> str | None:
    """Weibo image URLs arrive protocol-relative (``//wx4.sinaimg.cn/…``)."""
    if not url:
        return None
    text = url.strip()
    if text.startswith("//"):
        return f"https:{text}"
    return text


def pick_web_image(urls: Any) -> str | None:
    """Pick the most usable URL from a provider's list of the same image.

    Prefers a format a browser can actually render, then anything that is not a
    known-unusable one, then the first entry. Never returns an invented URL.
    """
    if not isinstance(urls, list):
        return None
    candidates = [_https(url) for url in urls if isinstance(url, str) and url.strip()]
    candidates = [url for url in candidates if url]
    if not candidates:
        return None

    for suffix in WEB_FRIENDLY_SUFFIXES:
        for url in candidates:
            if suffix in url.lower():
                return url
    for url in candidates:
        if not any(suffix in url.lower() for suffix in UNWEB_FRIENDLY_SUFFIXES):
            return url
    # Everything offered is HEIC/AVIF; better to store it than to store nothing.
    return candidates[0]


class XiaohongshuSearchAdapter(BasePlatformAdapter):
    """Keyword search over Xiaohongshu notes (images and video alike)."""

    platform = Platform.XIAOHONGSHU

    async def search(self, keyword: str, limit: int) -> list[HotContent]:
        payload = await self.client.get_json(
            XIAOHONGSHU_SEARCH,
            {
                "keyword": keyword,
                "page": 1,
                "sort_type": "general",
                "note_type": "不限",
            },
        )
        items = dig(payload, "data.data.items") or []
        if not isinstance(items, list):
            logger.warning("xiaohongshu search: unexpected items shape %s", type(items).__name__)
            return []
        logger.info("xiaohongshu search %r: %d raw items", keyword, len(items))
        return self.normalize_search(items, keyword=keyword)[:limit]

    def normalize_search(self, items: list[dict[str, Any]], *, keyword: str) -> list[HotContent]:
        results: list[HotContent] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            note = item.get("note") if isinstance(item.get("note"), dict) else {}
            if not note:
                continue
            title = as_text(first_of(note, ("title", "desc")))
            content_id = as_text(note.get("id"))
            if not title or not content_id:
                # Without an id there is no stable identity and no dedup key.
                logger.debug("xiaohongshu search: skipping an entry without id/title")
                continue

            user = note.get("user") if isinstance(note.get("user"), dict) else {}
            images = self._images(note)
            is_video = as_text(note.get("type")) == "video"
            video_url = (
                as_text(first_of(note, ("video.media.stream.h264.0.master_url",)))
                if is_video
                else None
            )

            results.append(
                self._build(
                    item,
                    rank=None,
                    content_id=content_id,
                    title=title,
                    url=f"https://www.xiaohongshu.com/explore/{content_id}",
                    description=as_text(first_of(note, ("desc",))),
                    author=as_text(user.get("nickname")),
                    author_id=as_text(user.get("user_id")),
                    likes=as_int(note.get("liked_count")),
                    collects=as_int(note.get("collected_count")),
                    comments=as_int(note.get("comments_count")),
                    cover_url=images[0].url_large or images[0].url if images else None,
                    video_url=video_url,
                    content_type=ContentType.VIDEO if is_video else ContentType.NOTE,
                    media=MediaBundle(
                        images=images,
                        video=(
                            MediaVideo(
                                url=video_url,
                                cover_url=images[0].url_large or images[0].url if images else None,
                            )
                            if is_video
                            else None
                        ),
                    ),
                    # Carried because the detail stage needs it to fetch the body.
                    raw_data={
                        "xsec_token": note.get("xsec_token"),
                        "note_type": note.get("type"),
                        "search_keyword": keyword,
                    },
                    origin=ContentOrigin.SEARCH,
                    source_keyword=keyword,
                )
            )
        return results

    @staticmethod
    def _images(note: dict[str, Any]) -> list[MediaImage]:
        """Every image of a note, largest variant first.

        The search response already carries the whole list (``images_list``), so no
        detail call is needed to get pictures.
        """
        entries = note.get("images_list")
        if not isinstance(entries, list):
            return []
        images: list[MediaImage] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            preview = as_text(first_of(entry, ("url", "url_default", "url_pre")))
            large = as_text(entry.get("url_size_large"))
            if not preview and not large:
                continue
            # ``url`` stays the preview and ``url_large`` the 1440w variant, rather
            # than collapsing both onto one field: the downloader prefers the large
            # one, and a reader of the row should be able to tell them apart.
            images.append(
                MediaImage(
                    url=preview or large or "",
                    url_large=large if large != preview else None,
                    width=as_int(entry.get("width")),
                    height=as_int(entry.get("height")),
                )
            )
        return images


class DouyinSearchAdapter(BasePlatformAdapter):
    """Keyword search over Douyin, where image posts and videos are distinguished."""

    platform = Platform.DOUYIN

    async def search(self, keyword: str, limit: int, *, content_type: str = "0") -> list[HotContent]:
        """``content_type``: 0=all, 1=video, 2=image, 3=article (provider's own filter)."""
        payload = await self.client.post_json(
            DOUYIN_SEARCH,
            {
                "keyword": keyword,
                "cursor": 0,
                "sort_type": "0",
                "publish_time": "0",
                "content_type": content_type,
            },
        )
        entries = dig(payload, "data.business_data") or []
        if not isinstance(entries, list):
            logger.warning("douyin search: unexpected entries shape %s", type(entries).__name__)
            return []
        logger.info("douyin search %r: %d raw entries", keyword, len(entries))
        return self.normalize_search(entries, keyword=keyword)[:limit]

    def normalize_search(self, entries: list[dict[str, Any]], *, keyword: str) -> list[HotContent]:
        results: list[HotContent] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # 16 of 19 entries carry aweme_info; the rest are non-post cards.
            info = dig(entry, "data.aweme_info")
            if not isinstance(info, dict):
                continue
            aweme_id = as_text(info.get("aweme_id"))
            description = as_text(info.get("desc"))
            if not aweme_id:
                continue

            author = info.get("author") if isinstance(info.get("author"), dict) else {}
            stats = info.get("statistics") if isinstance(info.get("statistics"), dict) else {}
            is_image_post = as_int(info.get("aweme_type")) == DOUYIN_IMAGE_TYPE

            images = self._images(info) if is_image_post else []
            cover = as_text(dig(info, "video.cover.url_list.0"))
            video_url = None if is_image_post else as_text(dig(info, "video.play_addr.url_list.0"))
            if is_image_post and not images:
                # An image post with no images is not usable as image material; keep
                # the cover so the row still shows something.
                images = [MediaImage(url=cover)] if cover else []

            duration = as_int(dig(info, "video.duration"))

            results.append(
                self._build(
                    entry,
                    rank=None,
                    content_id=aweme_id,
                    title=description,
                    url=as_text(info.get("share_url"))
                    or f"https://www.douyin.com/video/{aweme_id}",
                    description=description,
                    author=as_text(author.get("nickname")),
                    author_id=as_text(author.get("uid")),
                    publish_time=_from_epoch(as_int(info.get("create_time"))),
                    likes=as_int(stats.get("digg_count")),
                    comments=as_int(stats.get("comment_count")),
                    shares=as_int(stats.get("share_count")),
                    collects=as_int(stats.get("collect_count")),
                    cover_url=cover,
                    video_url=video_url,
                    content_type=ContentType.NOTE if is_image_post else ContentType.VIDEO,
                    media=MediaBundle(
                        images=images,
                        video=(
                            MediaVideo(url=video_url, cover_url=cover, duration_ms=duration)
                            if video_url
                            else None
                        ),
                    ),
                    raw_data={"aweme_type": info.get("aweme_type"), "search_keyword": keyword},
                    origin=ContentOrigin.SEARCH,
                    source_keyword=keyword,
                )
            )
        return results

    @staticmethod
    def _images(info: dict[str, Any]) -> list[MediaImage]:
        """An image post's pictures: ``images[].url_list[]``.

        ``url_list`` holds the same picture in several encodings, so the web-friendly
        one is picked rather than the first (see :func:`pick_web_image`).
        """
        entries = info.get("images")
        if not isinstance(entries, list):
            return []
        images: list[MediaImage] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = pick_web_image(entry.get("url_list"))
            if not url:
                continue
            images.append(
                MediaImage(
                    url=url,
                    width=as_int(entry.get("width")),
                    height=as_int(entry.get("height")),
                )
            )
        return images


class WeiboPicSearchAdapter(BasePlatformAdapter):
    """Weibo picture search — the only weibo endpoint that yields image URLs."""

    platform = Platform.WEIBO

    async def search(self, keyword: str, limit: int) -> list[HotContent]:
        payload = await self.client.get_json(WEIBO_PIC_SEARCH, {"query": keyword, "page": 1})
        entries = dig(payload, "data.pic_list") or []
        if not isinstance(entries, list):
            logger.warning("weibo pic search: unexpected entries shape %s", type(entries).__name__)
            return []
        logger.info("weibo pic search %r: %d raw entries", keyword, len(entries))
        return self.normalize_search(entries, keyword=keyword)[:limit]

    def normalize_search(self, entries: list[dict[str, Any]], *, keyword: str) -> list[HotContent]:
        results: list[HotContent] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            mid = as_text(entry.get("mid"))
            image_url = _https(as_text(entry.get("original_pic")))
            text = as_text(entry.get("text"))
            if not mid or not image_url:
                continue
            user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
            results.append(
                self._build(
                    entry,
                    rank=None,
                    content_id=mid,
                    title=text,
                    url=f"https://weibo.com/{user.get('id')}/{mid}" if user.get("id") else None,
                    description=text,
                    author=as_text(user.get("name")),
                    author_id=as_text(user.get("id")),
                    publish_time=_parse_weibo_time(as_text(entry.get("created_at"))),
                    cover_url=image_url,
                    content_type=ContentType.NOTE,
                    media=MediaBundle(images=[MediaImage(url=image_url)]),
                    raw_data={
                        "sub_name": entry.get("sub_name"),
                        "is_forward": entry.get("is_forward"),
                        "search_keyword": keyword,
                    },
                    origin=ContentOrigin.SEARCH,
                    source_keyword=keyword,
                )
            )
        return results


def _from_epoch(seconds: int | None) -> datetime | None:
    """Douyin timestamps are unix seconds."""
    if not seconds:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_weibo_time(value: str | None) -> datetime | None:
    """Weibo's ``Fri Sep 18 20:16:41 +0800 2026`` format."""
    if not value:
        return None
    for pattern in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S %z %Y"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


#: The search adapters, keyed by platform, for the pipeline to iterate.
SEARCH_ADAPTERS: dict[str, type[BasePlatformAdapter]] = {
    Platform.XIAOHONGSHU.value: XiaohongshuSearchAdapter,
    Platform.DOUYIN.value: DouyinSearchAdapter,
    Platform.WEIBO.value: WeiboPicSearchAdapter,
}

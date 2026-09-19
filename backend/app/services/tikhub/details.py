"""Section 十六 — fetching the detailed content behind a selected hot item.

This is the step the spec places between selection and rewriting, and it exists
because the list endpoints return **only headlines**: a rewriter given nothing but
a hot word can produce framing, not reporting, and the §18 anti-copy check has no
source body to compare against.

Three platforms, three different meanings — stated plainly because they are not
interchangeable:

* **Xiaohongshu** — a real note detail. ``note_id`` is the item id and
  ``xsec_token`` arrived with the homefeed payload, so both parameters are already
  in hand. Verified capture: ``data.data.items[0].note_card`` (note the doubled
  ``data``), carrying ``title`` / ``desc`` / ``tag_list`` / ``image_list``.
* **Douyin** — ``index/fetch_hot_detail`` takes a ``topic_name``, so it returns
  topic index detail: verified capture gives ``data.content_item`` (50 real posts
  with ``item_title``, ``author_name``, ``digg_cnt``) plus ``data.trend_item``
  (the topic's index samples). That is genuinely useful material.
* **Weibo** — a hot word has no post id at all, so the only route is a realtime
  **search** whose posts become source material. Verified capture:
  ``data.parsed_data.results`` with ``content`` / ``user_nick`` / ``post_url`` /
  ``publish_time``.

**Every call is billed**, so fetching is opt-in (``DETAIL_FETCH_ENABLED``) and the
pipeline's ``detail`` stage reports how many calls it made.

An earlier version of this module used "the longest string in the payload" as a
generic body fallback. The first real capture showed why that is wrong: the longest
string was TikHub's 215-character cache notice, which would have been handed to the
rewriter as source material. Every field below was read off a captured response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.services.pipeline.topic_grouping import platform_name
from app.services.tikhub.base import as_int, as_text, dig, first_of
from app.services.tikhub.client import TikHubClient

logger = logging.getLogger(__name__)

#: Endpoints, confirmed from the published OpenAPI document.
XIAOHONGSHU_NOTE_DETAIL = "/api/v1/xiaohongshu/web_v3/fetch_note_detail"
XIAOHONGSHU_IMAGE_DETAIL = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
DOUYIN_TOPIC_DETAIL = "/api/v1/douyin/index/fetch_hot_detail"
WEIBO_REALTIME_SEARCH = "/api/v1/weibo/web_v2/fetch_realtime_search"

# --- verified response paths (read off captured fixtures) --------------------
XHS_ITEMS_PATH = "data.data.items"
DOUYIN_CONTENT_PATH = "data.content_item"
DOUYIN_TREND_PATH = "data.trend_item"
WEIBO_RESULTS_PATH = "data.parsed_data.results"

#: Where Xiaohongshu keeps the note token.
XIAOHONGSHU_TOKEN_PATHS: Sequence[str] = ("xsec_token", "note_card.xsec_token")

#: How much fetched text is kept. Rewriting needs the substance, not an archive.
MAX_DETAIL_CHARS = 4000

#: How many source posts contribute to the material.
MAX_SOURCE_ITEMS = 5


@dataclass
class DetailResult:
    """What one detail fetch produced."""

    platform: str
    endpoint: str
    text: str = ""
    images: list[str] = field(default_factory=list)
    video_url: str | None = None
    source_items: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """True when there is text worth feeding to the rewriter."""
        return len(self.text.strip()) >= 40

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "endpoint": self.endpoint,
            "chars": len(self.text),
            "images": len(self.images),
            "video_url": self.video_url,
            "source_items": self.source_items,
            "usable": self.usable,
            "preview": self.text[:200],
        }


def build_detail_request(item: Any) -> tuple[str, dict[str, Any]] | None:
    """The endpoint and parameters for one item, or ``None`` when impossible.

    Xiaohongshu needs ``note_id`` **and** ``xsec_token``; the token came with the
    homefeed payload and is kept in ``raw_data``. Without it the app-side endpoint
    is used, which accepts a bare ``note_id``.

    Weibo and Douyin hot words have no post id, so their "detail" is a keyword
    search and a topic lookup respectively — never a fabricated id.
    """
    platform = platform_name(item)
    title = (getattr(item, "title", "") or "").strip()
    raw = getattr(item, "raw_data", None) or {}

    if platform == "xiaohongshu":
        note_id = (getattr(item, "platform_content_id", "") or raw.get("id") or "").strip()
        if not note_id or note_id.startswith("synth-"):
            return None
        token = first_of(raw, XIAOHONGSHU_TOKEN_PATHS)
        if isinstance(token, str) and token.strip():
            return XIAOHONGSHU_NOTE_DETAIL, {"note_id": note_id, "xsec_token": token.strip()}
        return XIAOHONGSHU_IMAGE_DETAIL, {"note_id": note_id}

    if platform == "douyin":
        if not title:
            return None
        return DOUYIN_TOPIC_DETAIL, {"topic_name": title}

    if platform == "weibo":
        if not title:
            return None
        return WEIBO_REALTIME_SEARCH, {"query": title, "page": 1}

    return None


def normalize_whitespace(text: str) -> str:
    """Collapse provider whitespace into readable single-spaced prose."""
    return " ".join(text.split()).strip()


def absolutize(url: Any) -> str | None:
    """Provider links are often protocol-relative (``//weibo.com/...``)."""
    text = as_text(url)
    if not text:
        return None
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("/"):
        return f"https://weibo.com{text}"
    return text


def extract_xiaohongshu_text(payload: dict[str, Any]) -> tuple[str, int, list[str]]:
    """Title, body, topics and images of the first note card."""
    items = dig(payload, XHS_ITEMS_PATH)
    if not isinstance(items, list) or not items:
        return "", 0, []
    card = dig(items[0], "note_card")
    if not isinstance(card, dict):
        return "", 0, []

    parts: list[str] = []
    title = as_text(card.get("title"))
    if title:
        parts.append(f"标题：{title}")
    body = as_text(card.get("desc"))
    if body:
        parts.append(f"正文：{normalize_whitespace(body)}")

    tags = [
        str(tag.get("name"))
        for tag in (card.get("tag_list") or [])
        if isinstance(tag, dict) and tag.get("name")
    ]
    if tags:
        parts.append("话题：" + "、".join(tags))

    author = as_text(dig(card, "user.nickname")) or as_text(dig(card, "user.nick_name"))
    if author:
        parts.append(f"作者：{author}")
    note_type = as_text(card.get("type"))
    liked = as_int(dig(card, "interact_info.liked_count"))
    meta = [item for item in (note_type, f"点赞 {liked}" if liked is not None else None) if item]
    if meta:
        parts.append("类型：" + " ｜ ".join(meta))

    images = [
        url
        for url in (
            as_text(dig(image, "url_default")) or as_text(dig(image, "url"))
            for image in (card.get("image_list") or [])
        )
        if url
    ]
    return "\n".join(parts), 1, images


def extract_douyin_text(
    payload: dict[str, Any], topic: str = ""
) -> tuple[str, int, list[str]]:
    """The topic's top works as material, plus the topic's own index trend."""
    items = dig(payload, DOUYIN_CONTENT_PATH)
    snippets: list[str] = []
    images: list[str] = []
    if isinstance(items, list):
        for item in items[:MAX_SOURCE_ITEMS]:
            if not isinstance(item, dict):
                continue
            title = as_text(item.get("item_title"))
            image = as_text(item.get("item_image"))
            if image:
                images.append(image)
            if not title:
                continue
            author = as_text(item.get("author_name")) or "未知作者"
            digg = as_int(item.get("digg_cnt"))
            suffix = f"（点赞 {digg}）" if digg is not None else ""
            snippets.append(f"- [{author}]{suffix} {normalize_whitespace(title)}")

    if not snippets:
        return "", 0, images

    trend = dig(payload, DOUYIN_TREND_PATH)
    trend_line = ""
    if isinstance(trend, list) and trend:
        latest = trend[-1].get("index") if isinstance(trend[-1], dict) else None
        trend_line = f"\n话题指数趋势：{len(trend)} 个采样点"
        if latest is not None:
            trend_line += f"，最新指数 {latest}"

    label = topic or "该话题"
    header = f"抖音话题「{label}」的相关作品（榜单/搜索素材，非单一原文）：\n"
    return header + "\n".join(snippets) + trend_line, len(snippets), images


def extract_weibo_text(
    payload: dict[str, Any], query: str = ""
) -> tuple[str, int, list[str]]:
    """Search results joined into material, with author and link per post."""
    rows = dig(payload, WEIBO_RESULTS_PATH)
    snippets: list[str] = []
    images: list[str] = []
    if isinstance(rows, list):
        for row in rows[:MAX_SOURCE_ITEMS]:
            if not isinstance(row, dict):
                continue
            media = row.get("media")
            if isinstance(media, dict):
                for value in media.values():
                    url = as_text(value)
                    if url and url.startswith("http"):
                        images.append(url)
            content = as_text(row.get("content"))
            if not content:
                continue
            author = as_text(row.get("user_nick")) or as_text(row.get("user_name")) or "未知作者"
            when = as_text(row.get("publish_time"))
            url = absolutize(row.get("post_url"))
            meta = " · ".join(part for part in (author, when) if part)
            line = f"- [{meta}] {normalize_whitespace(content)}"
            if url:
                line += f"（{url}）"
            snippets.append(line)

    if not snippets:
        return "", 0, images
    label = query or "该热词"
    header = (
        f"微博实时搜索「{label}」返回 {len(snippets)} 条相关帖文"
        "（搜索素材，不是单一原文；引用须注明来源未经核实）：\n"
    )
    return header + "\n".join(snippets), len(snippets), images


def extract_detail(item: Any, endpoint: str, payload: dict[str, Any]) -> DetailResult:
    """Turn one detail response into text, media and provenance."""
    platform = platform_name(item)
    query = getattr(item, "title", "") or ""

    if platform == "xiaohongshu":
        text, count, images = extract_xiaohongshu_text(payload)
    elif platform == "weibo":
        text, count, images = extract_weibo_text(payload, query=query)
    else:
        text, count, images = extract_douyin_text(payload, topic=query)

    video_url = as_text(
        first_of(
            payload,
            (
                "data.data.items.0.note_card.video.media.stream.h264.0.master_url",
                "data.data.items.0.note_card.video.media.stream.h265.0.master_url",
            ),
        )
    )
    return DetailResult(
        platform=platform,
        endpoint=endpoint,
        text=text[:MAX_DETAIL_CHARS],
        images=images[:12],
        video_url=video_url,
        source_items=count,
        raw=payload,
    )


async def fetch_detail(client: TikHubClient, item: Any) -> DetailResult | None:
    """Fetch one item's detail; ``None`` when no route exists for the platform."""
    request = build_detail_request(item)
    if request is None:
        logger.debug(
            "no detail route for %s item %s", platform_name(item), getattr(item, "id", "?")
        )
        return None
    path, params = request
    payload = await client.get_json(path, params)
    result = extract_detail(item, path, payload)
    logger.info(
        "detail for %s item %s via %s: %d chars from %d source item(s), usable=%s",
        result.platform,
        getattr(item, "id", "?"),
        path,
        len(result.text),
        result.source_items,
        result.usable,
    )
    return result


__all__ = [
    "DOUYIN_TOPIC_DETAIL",
    "DetailResult",
    "WEIBO_REALTIME_SEARCH",
    "XIAOHONGSHU_IMAGE_DETAIL",
    "XIAOHONGSHU_NOTE_DETAIL",
    "build_detail_request",
    "extract_detail",
    "fetch_detail",
]

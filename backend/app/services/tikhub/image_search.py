"""图片搜索：为文章/文案找真实配图（Phase 13）。

**这是"生图"的廉价替代**，也是运营方明确要的功能。对比：

* 搜图：1 次 TikHub 调用 **$0.0078**，一次返回 20 条带图内容；
* 生图（千问）：**$0.034/张**，且一次只出一张。

一次搜图 ≈ 生一张图的四分之一，而且拿到的是真实相关图片。**默认用搜图，生图按需。**

结构来自实测（**第一批代码就是照着我自己的错误诊断写的**，所以这里记下真实形状）：

```
GET /api/v1/xiaohongshu/app_v2/search_images?keyword=数据结构&page=1
  data.data.items[]                  ← 注意是**两层 data**
    image_info: {fileid, url, original, url_size_large, width, height, trace_id}
    note_info:  {title, desc, liked_count, collected_count, comments_count,
                 note_id, cover_image_index, model_type}
    user_info:  {nickname, user_id, red_id, images}
    share_info: {link, title, content, image}   ← link 是笔记地址，image 是低清图
```

第一版把字段当成 item 的直接子键，解析结果 0/20——因为写解析器时依赖了一个会把嵌套键
"压平"打印的诊断函数，看起来像直接子键。**诊断工具的输出也会骗人**，所以拿真实 JSON
逐层核对了一遍。

三个必须注意的点：

1. **图片链接会过期**（`xhscdn.com` 签名 URL），搜到就立刻下载进素材库；
2. **图片是别人的**，所以每条都保留作者与原文链接——用于署名与溯源；
3. 下载复用 :class:`MediaStore`，格式校验、大小上限、内容寻址去重都自动生效。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.models.hot_content import MediaBundle, MediaImage
from app.services.media.store import MediaStore
from app.services.tikhub.base import as_int, as_text, dig

logger = logging.getLogger(__name__)

#: 实测可用的小红书图片搜索。微博的 ``fetch_pic_search`` 试过同关键词返回 0 张，
#: 所以不作为默认来源。
XIAOHONGSHU_IMAGE_SEARCH = "/api/v1/xiaohongshu/app_v2/search_images"


@dataclass
class FoundImage:
    """一张搜到的图，连同它的出处。"""

    url: str
    width: int = 0
    height: int = 0
    title: str = ""
    description: str = ""
    author: str = ""
    author_id: str = ""
    note_id: str = ""
    link: str = ""
    likes: int = 0
    collects: int = 0
    local_path: str = ""
    bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "width": self.width,
            "height": self.height,
            "title": self.title,
            "description": self.description[:200],
            "author": self.author,
            "note_id": self.note_id,
            "link": self.link,
            "likes": self.likes,
            "collects": self.collects,
            "local_path": self.local_path,
            "bytes": self.bytes,
        }


def _pick_url(image_info: dict[str, Any]) -> str:
    """挑一个能直接下载的图片地址。

    优先 ``original``/``url_size_large``（清晰），退回 ``url``。
    实测这三个都是 ``sns-*.xhscdn.com`` 的签名地址，可以用。
    """
    for key in ("original", "url_size_large", "url"):
        value = as_text(image_info.get(key))
        if value and value.startswith("http"):
            return value
    return ""


def normalize_images(items: list[Any]) -> list[FoundImage]:
    """把 ``data.data.items[]`` 转成 :class:`FoundImage`。

    每条的字段分散在 ``image_info`` / ``note_info`` / ``user_info`` / ``share_info``
    四组里（**不是** item 的直接子键）。缺图或缺 id 的条目跳过，而不是伪造字段。
    """
    found: list[FoundImage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        image_info = item.get("image_info") if isinstance(item.get("image_info"), dict) else {}
        note_info = item.get("note_info") if isinstance(item.get("note_info"), dict) else {}
        user_info = item.get("user_info") if isinstance(item.get("user_info"), dict) else {}
        share_info = item.get("share_info") if isinstance(item.get("share_info"), dict) else {}

        url = _pick_url(image_info)
        if not url:
            logger.debug("image search: entry without a usable image url")
            continue
        note_id = as_text(note_info.get("note_id")) or ""
        # ``as_text`` returns None for a missing value, so every string field is coerced:
        # otherwise the dataclass promises ``str`` and hands out ``None`` downstream
        # (an f-string would print "None", and JSON would carry null instead of "").
        found.append(
            FoundImage(
                url=url,
                width=as_int(image_info.get("width")) or 0,
                height=as_int(image_info.get("height")) or 0,
                title=as_text(note_info.get("title")) or as_text(share_info.get("title")) or "",
                description=as_text(note_info.get("desc"))
                or as_text(share_info.get("content"))
                or "",
                author=as_text(user_info.get("nickname")) or "",
                author_id=as_text(user_info.get("user_id")) or "",
                note_id=note_id,
                link=as_text(share_info.get("link"))
                or (f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""),
                likes=as_int(note_info.get("liked_count")) or 0,
                collects=as_int(note_info.get("collected_count")) or 0,
            )
        )
    return found


#: 实测这个接口会**偶发返回 400**：同一个请求几秒后重试就是 200。TikHub 客户端默认把
#: 4xx（429 除外）当成终态——对这个接口过于严格，所以这里自己再试几次。
#: 失败请求不计费（响应里写的是"请求成功才计费"），所以重试的代价只是时间。
SEARCH_ATTEMPTS = 3
SEARCH_RETRY_DELAY_SECONDS = 1.5


async def search_images(
    keyword: str,
    *,
    limit: int = 12,
    page: int = 1,
    settings: Settings | None = None,
    client: Any | None = None,
) -> list[FoundImage]:
    """按关键词搜图。**1 次计费调用**（失败会重试，重试成功才计费）。"""
    import asyncio

    from app.services.tikhub.client import TikHubError

    resolved = settings or get_settings()
    if not keyword.strip():
        raise ValueError("关键词不能为空")

    owns_client = client is None
    if client is None:
        from app.services.tikhub.client import TikHubClient

        client = TikHubClient(resolved)
    payload: Any = None
    last_error: Exception | None = None
    try:
        for attempt in range(1, SEARCH_ATTEMPTS + 1):
            try:
                payload = await client.get_json(
                    XIAOHONGSHU_IMAGE_SEARCH, {"keyword": keyword.strip(), "page": page}
                )
                break
            except TikHubError as exc:
                last_error = exc
                if attempt >= SEARCH_ATTEMPTS:
                    raise
                logger.warning(
                    "image search %r failed (attempt %d/%d): %s — retrying",
                    keyword,
                    attempt,
                    SEARCH_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(SEARCH_RETRY_DELAY_SECONDS * attempt)
    finally:
        if owns_client:
            await client.aclose()
    if payload is None:  # pragma: no cover - the loop either breaks or raises
        raise last_error or RuntimeError("image search produced no response")

    items = dig(payload, "data.items") or dig(payload, "data.data.items") or []
    if not isinstance(items, list):
        logger.warning("image search: unexpected items shape %s", type(items).__name__)
        return []
    logger.info("image search %r: %d raw items", keyword, len(items))
    return normalize_images(items)[:limit]


async def search_and_download(
    keyword: str,
    *,
    limit: int = 8,
    page: int = 1,
    settings: Settings | None = None,
    client: Any | None = None,
) -> tuple[list[FoundImage], dict[str, Any]]:
    """搜图并**立刻下载进素材库**。返回 ``(images, report)``。

    先下载后使用是必须的：这些是签名 URL，过期后就连不上了（项目里已经栽过一次）。
    """
    resolved = settings or get_settings()
    images = await search_images(
        keyword, limit=limit, page=page, settings=resolved, client=client
    )
    if not images:
        return [], {"searched": 0, "downloaded": 0, "errors": ["没有搜到可用图片"]}

    # 复用 MediaStore：格式识别、大小上限、内容寻址去重都是现成的。
    bundle = MediaBundle(
        images=[MediaImage(url=image.url, width=image.width, height=image.height) for image in images]
    )
    store = MediaStore(resolved)
    report = await store.download_bundle(bundle)

    for found, stored in zip(images, bundle.images):
        if stored.local_path:
            found.local_path = stored.local_path
            found.bytes = stored.bytes or 0

    downloaded = sum(1 for image in images if image.local_path)
    summary = {
        "searched": len(images),
        "downloaded": downloaded,
        "failed": len(images) - downloaded,
        "errors": list(getattr(report, "errors", []) or [])[:5],
        "note": (
            "图片已下载到本地素材库——平台链接会过期，不下载就用不了。"
            "这些图来自他人笔记，每条都保留了作者与原文链接用于署名。"
        ),
    }
    logger.info(
        "image search %r: %d found, %d downloaded", keyword, len(images), downloaded
    )
    return images, summary


__all__ = [
    "XIAOHONGSHU_IMAGE_SEARCH",
    "FoundImage",
    "normalize_images",
    "search_and_download",
    "search_images",
]

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
import re
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


def _normalize(text: str) -> str:
    """归一化后比较：去空格、统一大小写。

    必须去空格：真实数据里出现过 ``B + 树`` 与 ``B+树``，直接包含比较会漏掉前者。
    """
    return re.sub(r"\s+", "", text or "").casefold()


def is_relevant(image: FoundImage, topic: str, *, loose: bool = False) -> bool:
    """来源笔记的标题或正文里是否真的出现了主题词。

    **这是解决"图不对题"的关键一步。** 小红书图片搜索是**宽松的文本匹配**：搜
    「红黑树」会返回「红黑美学」「树的哲学」这类自然摄影与壁纸（它把"红黑"和"树"当两个
    词分别匹配了），也会返回「红豆杉树墙」「树冠羞避」这类园艺内容。所以"搜到图"完全不
    等于"图与主题相关"。

    我们看不到图，但**看得到来源笔记的标题与正文**——那是免费且有效的判据。

    ``loose=True`` 时再宽一档：把主题里的 ``+``/``-``/``#``/空格 去掉后比较。
    这是为了「B+树」这种写法——实测「这样构建3阶B树对吗」「B树」都被精确匹配误杀了，
    而 B 树与 B+树 的图对读者是通用的。
    """
    needle = _normalize(topic)
    if not needle:
        return True
    haystack = _normalize(f"{image.title} {image.description}")
    if needle in haystack:
        return True
    if loose:
        stripped = re.sub(r"[+\-#·、,，.。/／]+", "", needle)
        # 太短的残片（如只剩「树」）会放进一堆无关内容，所以要求至少 2 个字符。
        if len(stripped) >= 2 and stripped in haystack:
            return True
    return False


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


#: 检索词后缀，按"命中相关图的概率"排序。裸主题词实测会捞回同名不同域的内容
#: （搜「红黑树」得到「红黑美学」壁纸），加「图解」显著提高命中；
#: 「原理」「笔记」作为备选，在前一个不够时才用。
QUERY_SUFFIXES: tuple[str, ...] = ("图解", "原理", "笔记")

#: 最多为一批配图花几次搜索调用。1 次约 $0.0078，所以这里既是质量也是成本的旋钮。
MAX_SEARCH_ATTEMPTS = 2


async def search_topic_images(
    topic: str,
    *,
    want: int,
    settings: Settings | None = None,
) -> tuple[list[FoundImage], dict[str, Any]]:
    """为某个主题找配图：**自适应地试检索词，够用就停**。

    先用「<主题> 图解」；相关图不足 ``want`` 张时再用「原理」，最多 ``MAX_SEARCH_ATTEMPTS`` 次。
    这样常见的顺利情况只花 1 次调用，只有确实搜不到时才多花一次——
    而不是每次都固定烧 3 次。

    返回 ``(images, report)``；``report`` 里记了每个检索词各拿到几张，便于事后判断
    是"这个词不好"还是"小红书上就是没有这个主题的图"。
    """
    resolved = settings or get_settings()
    core = topic.strip()
    if not core:
        return [], {"errors": ["主题为空"], "attempts": []}

    collected: list[FoundImage] = []
    seen: set[str] = set()
    attempts: list[dict[str, Any]] = []
    last_summary: dict[str, Any] = {}

    for suffix in QUERY_SUFFIXES[: MAX_SEARCH_ATTEMPTS if want <= 6 else MAX_SEARCH_ATTEMPTS + 1]:
        query = f"{core} {suffix}"
        found, summary = await search_and_download(
            query,
            # 过滤会剔掉一批，所以每次都要得比目标多。
            limit=max(want * 3, want + 4),
            settings=resolved,
            topic=core,
        )
        last_summary = summary
        added = 0
        for image in found:
            key = image.note_id or image.url
            if key in seen:
                continue
            seen.add(key)
            collected.append(image)
            added += 1
        attempts.append(
            {
                "query": query,
                "relevant": summary.get("relevant", 0),
                "added": added,
                "dropped": len(summary.get("dropped_titles") or []),
            }
        )
        logger.info(
            "image search for %r: %r -> %d relevant (%d new)",
            core,
            query,
            summary.get("relevant", 0),
            added,
        )
        if len(collected) >= want:
            break

    report = dict(last_summary)
    report["attempts"] = attempts
    report["queries_used"] = len(attempts)
    report["relevant"] = len(collected)
    report["note"] = (
        f"共试了 {len(attempts)} 个检索词："
        + "；".join(f"{a['query']}→{a['relevant']}张" for a in attempts)
        + "。"
        + (last_summary.get("note") or "")
    )
    return collected[:want], report


async def search_and_download(
    keyword: str,
    *,
    limit: int = 8,
    page: int = 1,
    settings: Settings | None = None,
    client: Any | None = None,
    topic: str = "",
    require_relevant: bool = True,
) -> tuple[list[FoundImage], dict[str, Any]]:
    """搜图并**立刻下载进素材库**。返回 ``(images, report)``。

    ``topic`` 是**相关性判据**（通常是文章主题）。给定时只保留来源笔记标题/正文里真的
    出现了该词的条目——不然会混进「红黑美学」这类同名不同域的图（实测踩到过）。
    注意 ``keyword`` 与 ``topic`` 可以不同：前者用于检索（可以加「图解」提高命中），
    后者用于过滤。

    先下载后使用是必须的：这些是签名 URL，过期后就连不上了。
    """
    resolved = settings or get_settings()
    images = await search_images(
        keyword, limit=limit, page=page, settings=resolved, client=client
    )
    found_total = len(images)

    dropped: list[str] = []
    loosened = 0
    if require_relevant and topic.strip():
        strict: list[FoundImage] = []
        weak: list[FoundImage] = []
        for image in images:
            if is_relevant(image, topic):
                strict.append(image)
            elif is_relevant(image, topic, loose=True):
                # 宽匹配命中的（如搜「B+树」时的「B树」）排在精确命中之后。
                weak.append(image)
            else:
                dropped.append(str(image.title or "")[:30])
        loosened = len(weak)
        images = (strict + weak)[:limit]

    if not images:
        return [], {
            "searched": found_total,
            "relevant": 0,
            "downloaded": 0,
            "errors": [
                f"搜到的 {found_total} 条里没有与「{topic or keyword}」相关的"
                "（配图来自他人笔记的封面，检索是宽松文本匹配，容易混进同名不同域的内容）。"
                "可以换个说法重搜，例如加「图解」「原理」，或改用文字生成配图。"
            ],
            "dropped_titles": dropped[:6],
        }

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
        "searched": found_total,
        "relevant": len(images),
        "loose_matches": loosened,
        "downloaded": downloaded,
        "failed": len(images) - downloaded,
        "dropped_titles": dropped[:6],
        "errors": list(getattr(report, "errors", []) or [])[:5],
        "note": (
            "图片已下载到本地素材库——平台链接会过期，不下载就用不了。"
            "这些图来自他人笔记，每条都保留了作者与原文链接用于署名。"
            + (
                f"已剔除 {len(dropped)} 张来源笔记里没出现主题词的（同名不同域，如搜「红黑树」"
                "捞到的自然摄影与壁纸）。"
                if dropped
                else ""
            )
            + (f"其中 {loosened} 张是宽匹配（如 B树 之于 B+树）。" if loosened else "")
        ),
    }
    logger.info(
        "image search %r: %d found, %d relevant, %d downloaded",
        keyword,
        found_total,
        len(images),
        downloaded,
    )
    return images, summary


__all__ = [
    "XIAOHONGSHU_IMAGE_SEARCH",
    "FoundImage",
    "normalize_images",
    "search_and_download",
    "search_images",
]

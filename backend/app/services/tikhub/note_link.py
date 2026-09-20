"""从小红书分享链接取笔记正文与配图 —— **走 TikHub，不走网页抓取**。

为什么不能抓网页：分享短链会重定向到 ``www.xiaohongshu.com/login?...``，
返回的是登录页（实测 HTML 里 ``<img>`` 数量为 0、无任何 xhscdn 图片地址）。
匿名抓取拿不到笔记图片。

可行路径：短链重定向后的地址里**同时带着 note_id 与 xsec_token**
（形如 ``.../discovery/item/<note_id>?...&xsec_token=<token>...``），
拿这两个调 TikHub 的笔记详情接口即可。

费用：解析重定向免费；TikHub 笔记详情 1 次调用（约 $0.0078）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

#: **必须用这个端点。** 实测对比同一个笔记：
#:   ``web_v3/fetch_note_detail``    → 返回 8303 字符，出现 ``xhscdn`` **0 次**（没有图片地址）
#:   ``app_v2/get_image_note_detail`` → 返回 21308 字符，出现 ``xhscdn`` **3 次** ✓
#: 名字里那个 image 是关键——图文笔记详情要用 app_v2 这个。
NOTE_DETAIL_ENDPOINT = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
#: 笔记页地址里 note_id 的位置。分享链接的 redirectPath 是 URL 编码的，所以两种都要认。
NOTE_ID_RE = re.compile(r"/(?:discovery/)?item/([0-9a-fA-F]{16,32})")
NOTE_ID_PARAM_RE = re.compile(r"note_?id=([0-9a-fA-F]{16,32})")
XHS_HOSTS = ("xhslink.cn", "xhslink.com", "xiaohongshu.com")


@dataclass
class NoteLink:
    """从分享链接解析出的笔记定位信息。"""

    note_id: str = ""
    xsec_token: str = ""
    final_url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.note_id)


def is_xiaohongshu_link(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host.endswith(candidate) for candidate in XHS_HOSTS)


def parse_note_ref(url: str) -> NoteLink:
    """从（可能被 URL 编码的）地址里抽出 note_id 与 xsec_token。**不发请求。**"""
    # redirectPath 把整条目标地址编码了一层，先解一次再匹配。
    decoded = unquote(url)
    for source in (url, decoded):
        match = NOTE_ID_PARAM_RE.search(source) or NOTE_ID_RE.search(source)
        if match:
            token = ""
            query = parse_qs(urlparse(source).query)
            for key in ("xsec_token", "xsecToken"):
                if query.get(key):
                    token = query[key][0]
                    break
            return NoteLink(note_id=match.group(1), xsec_token=token, final_url=source)
    return NoteLink(error="地址里找不到 note_id")


async def resolve_note_link(url: str, *, timeout: float = 20.0) -> NoteLink:
    """跟随重定向，拿到带 note_id 与 xsec_token 的最终地址。"""
    import httpx

    if not is_xiaohongshu_link(url):
        return NoteLink(error="不是小红书链接")
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            # 必须：本机注册表的系统代理会劫持请求。
            trust_env=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SocialHotAI/1.0)"},
        ) as client:
            response = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        return NoteLink(error=f"{type(exc).__name__}: {exc}")

    parsed = parse_note_ref(str(response.url))
    parsed.final_url = str(response.url)
    return parsed


#: 笔记详情里图片可能出现的键（不同接口版本的字段名不一样）。
IMAGE_KEYS = ("url_default", "url", "url_pre", "original", "url_size_large", "info_list")


def collect_image_urls(payload: Any, *, limit: int = 9) -> list[str]:
    """从笔记详情返回里挖出图片地址，**每张图只取一个（最大的那个）**。

    两个坑（都是实测踩到的）：

    **① 不能按 CDN 域名过滤。** 第一版要求地址里含 ``xhscdn``——但真实图片在
    ``sns-i11.rednotecdn.com`` 上，于是 7 张图全被过滤掉。小红书换过 CDN 域名，
    写死任何一个都会漏。

    **② 必须按图片去重。** 同一张图在返回里有 6 个尺寸变体，**路径相同、只有查询参数
    不同**::

        .images_list[0].original       → .../1040g2sg3250vpqan3oe?imageView2/2/w/5000/h/5000/f
        .images_list[0].url_size_large → .../1040g2sg3250vpqan3oe?imageView2/2/w/1440
        .images_list[0].url            → .../1040g2sg3250vpqan3oe?imageView2/2/w/576

    不按路径去重的话，一次"7 张图"会变成 40 多个地址，其中大多是同一张图的重复——
    用户看到的就是"**只识别出一张**"（因为发出去的多半是同图不同尺寸）。
    去重键取**去掉查询串的路径**，并按优先级挑最清晰的那个。
    """
    #: 这些域名/路径出现在返回里但不是笔记内容：TikHub 自己的文档与缓存链接、笔记页本身、
    #: 作者头像、以及平台自动生成的翻译覆盖图。
    NOT_IMAGES = (
        "api.tikhub.io",
        "cache.tikhub.io",
        "/discovery/item/",
        "/explore/",
        "avatar",
        "cloudrender-translate",
    )
    #: 明显的缩略图标记——我们要大图，缩略图 OCR 出来也糊。
    THUMBNAIL_MARKERS = ("r_120w_120h", "w/360", "w/540", "/thumb", "@r_")

    def is_note_image(url: str) -> bool:
        lowered = url.lower()
        if any(marker in lowered for marker in NOT_IMAGES):
            return False
        if any(marker in lowered for marker in THUMBNAIL_MARKERS):
            return False
        path = lowered.split("?", 1)[0]
        # 小红书的图片走 imageView2 参数；也接受直接以图片扩展名结尾的。
        return "imageview2" in lowered or path.endswith((".jpg", ".jpeg", ".png", ".webp"))

    #: 路径 → (优先级, 完整地址)。优先级越小越清晰。
    best: dict[str, tuple[int, str]] = {}
    order: list[str] = []

    def rank_of(hint: str) -> int:
        lowered = hint.lower()
        if "original" in lowered:
            return 0
        if "url_size_large" in lowered:
            return 1
        if lowered == "url" or lowered.endswith(".url"):
            return 2
        if "multi_level.low" in lowered:
            return 4
        return 3

    def walk(node: Any, key_hint: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, str(key))
        elif isinstance(node, list):
            for item in node:
                walk(item, key_hint)
        elif isinstance(node, str):
            if not node.startswith("http") or not is_note_image(node):
                return
            path = node.split("?", 1)[0]
            rank = rank_of(key_hint)
            current = best.get(path)
            if current is None:
                best[path] = (rank, node)
                order.append(path)
            elif rank < current[0]:
                best[path] = (rank, node)

    walk(payload)
    return [best[path][1] for path in order[:limit]]


async def fetch_note_payload(url: str, *, settings: Any) -> tuple[dict[str, Any], str]:
    """取笔记详情原始返回。返回 ``(payload, error)``。**1 次 TikHub 调用。**"""
    import httpx

    link = await resolve_note_link(url)
    if not link.ok:
        return {}, link.error
    params = {"note_id": link.note_id}
    if link.xsec_token:
        params["xsec_token"] = link.xsec_token
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.get(
                settings.tikhub_base_url.rstrip("/") + NOTE_DETAIL_ENDPOINT,
                params=params,
                headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
            )
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"
    if response.status_code >= 400:
        return {}, f"TikHub HTTP {response.status_code}: {response.text[:120]}"
    try:
        return response.json(), ""
    except ValueError as exc:
        return {}, f"返回不是 JSON：{exc}"


def collect_video_urls(payload: Any, *, limit: int = 3) -> list[str]:
    """从笔记详情里挖视频地址。

    判据比图片更严：必须像视频（带扩展名或明显的视频字段），否则会把图片误当视频。
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any, key_hint: str = "") -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, str(key))
        elif isinstance(node, list):
            for item in node:
                walk(item, key_hint)
        elif isinstance(node, str):
            lowered = node.lower()
            hint = key_hint.lower()
            looks_video = (
                lowered.endswith((".mp4", ".mov", ".m3u8"))
                or "video" in hint
                or "stream" in hint
            )
            if node.startswith("http") and looks_video and node not in seen:
                seen.add(node)
                found.append(node)

    walk(payload)
    return found[:limit]


async def fetch_xhs_note_images(
    url: str,
    *,
    settings: Any,
    limit: int = 3,
) -> tuple[list[str], dict[str, Any]]:
    """从小红书链接下载笔记配图到本地素材库。返回 ``(相对路径, 报告)``。

    **必须下载**：xhscdn 是签名地址，会过期；不下载就发不出去（项目里踩过）。
    """
    import httpx

    report: dict[str, Any] = {"note_id": "", "found": 0, "saved": 0, "error": ""}
    link = await resolve_note_link(url)
    if not link.ok:
        report["error"] = link.error
        return [], report
    report["note_id"] = link.note_id

    endpoint = NOTE_DETAIL_ENDPOINT
    params = {"note_id": link.note_id}
    if link.xsec_token:
        params["xsec_token"] = link.xsec_token

    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.get(
                settings.tikhub_base_url.rstrip("/") + endpoint,
                params=params,
                headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
            )
            if response.status_code >= 400:
                report["error"] = f"TikHub HTTP {response.status_code}: {response.text[:120]}"
                return [], report
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        return [], report

    candidates = collect_image_urls(payload, limit=max(limit * 3, 9))
    report["found"] = len(candidates)
    if not candidates:
        report["error"] = "笔记详情里没有图片地址"
        return [], report

    target_dir = Path(settings.media_root_path) / "from_url"
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            for candidate in candidates:
                if len(saved) >= limit:
                    break
                try:
                    blob = (await client.get(candidate)).content
                except Exception:  # noqa: BLE001 - 单张失败不影响其它
                    continue
                if len(blob) < 4 * 1024:
                    continue
                digest = hashlib.sha256(blob).hexdigest()[:32]
                suffix = ".png" if candidate.lower().endswith(".png") else ".jpg"
                path = target_dir / f"{digest}{suffix}"
                if not path.is_file():
                    path.write_bytes(blob)
                saved.append(
                    str(path.relative_to(Path(settings.media_root_path).parent)).replace("\\", "/")
                )
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"

    report["saved"] = len(saved)
    logger.info("xhs note %s: %d images saved", link.note_id, len(saved))
    return saved, report

"""从链接里抓正文与配图。

分工：
  * 正文沿用 ``promo_service.load_readme``（已有重定向、体积上限、HTML 登录页检测）；
  * **配图**是本模块新增的——它需要原始 HTML，而 ``load_readme`` 返回的是纯文本。

铁律（项目里踩过两次）：
  * ``trust_env=False`` —— 本机注册表里的代理会劫持请求；
  * **不下载就发不出去** —— 平台图片链接会过期。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

#: 单张图上限。超过就不下——网页里常嵌几 MB 的原图，发到群里也没意义。
MAX_IMAGE_BYTES = 4 * 1024 * 1024
#: 太小的基本是图标、分隔线、埋点像素。
MIN_IMAGE_BYTES = 6 * 1024
#: 默认最多下几张。
DEFAULT_IMAGE_LIMIT = 3
#: ``og:image`` 优先——它是站点自己挑的分享图，通常最合适。
OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["'][^>]*>""",
    re.IGNORECASE,
)
CONTENT_RE = re.compile(r"""<meta[^>]+content\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
IMG_SRC_RE = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
#: srcset 里的第一段通常是同图的不同尺寸，用它做补充。
SRCSET_RE = re.compile(r"""<img[^>]+srcset\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

SKIP_PREFIXES = ("data:", "blob:", "javascript:", "about:")
SKIP_SUFFIXES = (".svg", ".gif")
SKIP_HINTS = ("icon", "logo", "avatar", "sprite", "pixel", "spacer", "badge")


@dataclass
class PageImages:
    """抓取结果。"""

    paths: list[str] = field(default_factory=list)
    candidates: int = 0
    skipped: list[str] = field(default_factory=list)
    error: str = ""


def _absolute(candidate: str, base: str) -> str:
    candidate = unescape(candidate.strip())
    if not candidate or candidate.startswith(SKIP_PREFIXES):
        return ""
    return urljoin(base, candidate)


def find_image_urls(html: str, base_url: str, *, limit: int = 20) -> list[str]:
    """从 HTML 里挑出可能的配图地址，**og:image 排在最前**。"""
    found: list[str] = []

    for tag in OG_IMAGE_RE.findall(html):
        match = CONTENT_RE.search(tag)
        if match:
            url = _absolute(match.group(1), base_url)
            if url:
                found.append(url)

    for regex in (IMG_SRC_RE, SRCSET_RE):
        for raw in regex.findall(html):
            # srcset 形如 "a.jpg 1x, b.jpg 2x"——取第一段。
            first = raw.split(",")[0].strip().split(" ")[0]
            url = _absolute(first, base_url)
            if url:
                found.append(url)

    # 去重并保序。
    seen: set[str] = set()
    ordered: list[str] = []
    for url in found:
        if url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return ordered[:limit]


def looks_like_content_image(url: str) -> bool:
    """粗略过滤：图标/头像/埋点像素不值得发到群里。"""
    lowered = url.lower()
    if lowered.endswith(SKIP_SUFFIXES):
        return False
    path = urlparse(lowered).path
    return not any(hint in path for hint in SKIP_HINTS)


async def fetch_page_images(
    url: str,
    *,
    settings: Any,
    limit: int = DEFAULT_IMAGE_LIMIT,
) -> PageImages:
    """抓取网页正文里的配图并**下载到本地素材库**，返回相对路径。"""
    import httpx

    if not url.startswith(("http://", "https://")):
        return PageImages(error="不是 http(s) 链接")

    timeout = 20.0
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            # 必须：本机注册表里的系统代理会劫持请求（项目里反复踩到）。
            trust_env=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SocialHotAI/1.0)"},
        ) as client:
            page = await client.get(url)
            if page.status_code >= 400:
                return PageImages(error=f"页面返回 HTTP {page.status_code}")
            content_type = page.headers.get("content-type", "")
            if "html" not in content_type.lower():
                return PageImages(error=f"链接不是网页（content-type={content_type[:40]}）")

            candidates = [
                candidate
                for candidate in find_image_urls(page.text, str(page.url))
                if looks_like_content_image(candidate)
            ]
            if not candidates:
                return PageImages(error="这个页面里没找到可用的图片")

            target_dir = Path(settings.media_root_path) / "from_url"
            target_dir.mkdir(parents=True, exist_ok=True)

            saved: list[str] = []
            skipped: list[str] = []
            for candidate in candidates:
                if len(saved) >= limit:
                    break
                try:
                    response = await client.get(candidate)
                except Exception as exc:  # noqa: BLE001 - 单张图失败不该影响其它
                    skipped.append(f"{candidate[:40]}…：{type(exc).__name__}")
                    continue
                if response.status_code >= 400:
                    skipped.append(f"{candidate[:40]}…：HTTP {response.status_code}")
                    continue
                blob = response.content
                kind = response.headers.get("content-type", "")
                if not kind.startswith("image/"):
                    skipped.append(f"{candidate[:40]}…：不是图片（{kind[:20]}）")
                    continue
                if not (MIN_IMAGE_BYTES <= len(blob) <= MAX_IMAGE_BYTES):
                    skipped.append(f"{candidate[:40]}…：体积 {len(blob) // 1024}KB 不合适")
                    continue
                # 内容寻址命名：同一张图重复抓也只存一份。
                digest = hashlib.sha256(blob).hexdigest()[:32]
                suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
                    kind.split(";")[0].strip(), ".jpg"
                )
                path = target_dir / f"{digest}{suffix}"
                if not path.is_file():
                    path.write_bytes(blob)
                # send_rich 需要的是相对项目根的路径。
                saved.append(str(path.relative_to(Path(settings.media_root_path).parent)).replace("\\", "/"))

            logger.info("fetched %d images from %s (%d skipped)", len(saved), url, len(skipped))
            return PageImages(
                paths=saved, candidates=len(candidates), skipped=skipped[:5]
            )
    except Exception as exc:  # noqa: BLE001
        return PageImages(error=f"{type(exc).__name__}: {exc}")

"""从**视频链接**取可理解的内容 —— 抖音走 TikHub，再交给全模态模型。

为什么必须这样：抖音分享页是客户端渲染的，HTML 里**没有**视频地址
（实测 0 个 mp4、0 个 m3u8、无内嵌 JSON）。但 TikHub 的
``fetch_one_video_by_share_url`` 能直接吃分享链接并返回播放地址，
全模态模型也拉得到那个地址（实测成功读到视频里的字幕与贴纸文字）。

**不做这一步会怎样**：文案只能靠标题瞎编，用户实测反馈"一点用没有"——
标题是「无广分享！准大一们看过来！电子设备怎么选」，写出来的文案就只是
把这几个词换个说法重复一遍。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
FETCH_ONE_VIDEO = "/api/v1/douyin/app/v3/fetch_one_video_by_share_url"


@dataclass
class VideoMaterial:
    """视频理解的结果。"""

    text: str = ""
    play_url: str = ""
    title: str = ""
    tokens: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


def is_douyin_link(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host.endswith(candidate) for candidate in DOUYIN_HOSTS)


def collect_video_urls(payload: Any, *, limit: int = 3) -> list[str]:
    """从抖音详情返回里挖播放地址。

    判据放得很宽：只要 http + 域名像抖音 CDN，或者带 .mp4/.m3u8。
    实测地址在 ``douyinvod.com`` 上，字段名是 ``url_list``。
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
                ".mp4" in lowered
                or ".m3u8" in lowered
                or "douyinvod" in lowered
                or "play" in hint
            )
            if node.startswith("http") and looks_video and node not in seen:
                seen.add(node)
                found.append(node)

    walk(payload)
    return found[:limit]


def collect_title(payload: Any) -> str:
    """取视频文案/标题，作为理解结果之外的补充材料。"""
    for key in ("desc", "title", "caption", "text_extra"):
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:300]
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return ""


#: **笔记真图挂在这个字段下**（实测确认）：
#: ``.data.aweme_detail.images[].url_list[]``
#:
#: 为什么要认准字段而不是通用遍历：第一版用通用遍历，结果抓到了
#: ``cha_list[].author.avatar_*``（**别人的头像**）和 ``music.cover_*``（音乐封面），
#: 用户收到 5 张同样的动漫头像，而笔记里明明是笔记本推荐清单。
IMAGES_FIELD_TAIL = ("aweme_detail", "images")


def _find_note_images(payload: Any) -> list[dict[str, Any]]:
    """定位 ``aweme_detail.images`` 数组（图文笔记的真图在这个数组里）。"""
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            # 命中：当前节点有 images 且它是个 list，列表元素是含 url_list 的 dict。
            images = node.get("images")
            if isinstance(images, list) and any(
                isinstance(item, dict) and "url_list" in item for item in images
            ):
                return [item for item in images if isinstance(item, dict)]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return []


def collect_image_urls(payload: Any, *, limit: int = 9) -> list[str]:
    """挖出**笔记自己的**图片地址（按图片 ID 去重，每张取一个地址）。

    两个必须做对的地方：
      * **认准 ``aweme_detail.images[].url_list``**，不能通用遍历——
        否则会抓到别人的头像、音乐封面（实测用户收到 5 张动漫头像而笔记是笔记本清单）；
      * **按 ``~`` 之前的图片 ID 去重**：同一张图有多个尺寸变体，
        区别在**路径里**（``…AL~tplv-dy-aw`` / ``~tplv-dy-ku``），按查询串去重会漏。
    """
    entries = _find_note_images(payload)
    found: list[str] = []
    seen: set[str] = set()

    def rank(url: str) -> int:
        """选地址的优先级。**越小越好。**

        实测每张图的 ``url_list`` 里有 3 个地址，第一个是 ``_offtrans_`` 开头的
        **HEIF/HEIC**（文件头 ``ftypvvic``）——Pillow 打不开，存成 .jpg 也是坏的，
        拼图直接失败。可用的那个带 ``tplv-dy-aweme-images``。
        """
        lowered = url.lower()
        if "tplv-dy-aweme-images" in lowered:
            return 0
        if "tplv-dy" in lowered:
            return 1
        if "douyinpic.com" in lowered:
            return 2
        if "_offtrans_" in lowered:
            return 9  # HEIF，尽量不用
        return 5

    for entry in entries:
        if len(found) >= limit:
            break
        candidates: list[str] = []
        for key in ("url_list", "download_url_list"):
            value = entry.get(key)
            if isinstance(value, list):
                candidates.extend(str(item) for item in value if isinstance(item, str))
        usable = [c for c in candidates if c.startswith("http")]
        if not usable:
            continue
        # 按优先级挑最好的那个地址。
        best = sorted(usable, key=rank)[0]
        key = best.split("?", 1)[0].split("~", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        found.append(best)
    return found[:limit]


@dataclass
class DouyinMaterial:
    """抖音链接的解析结果。**视频与图文是两条不同的处理路径。**"""

    kind: str = ""  # "video" | "images" | ""
    text: str = ""
    media_paths: list[str] = field(default_factory=list)
    caption: str = ""
    tokens: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


async def _download_images(images: list[str], *, settings: Any) -> tuple[list[str], str]:
    """下载图片到素材库，返回 ``(相对路径, 错误)``。"""
    import hashlib
    from pathlib import Path

    import httpx

    target_dir = Path(settings.media_root_path) / "from_url"
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            for candidate in images:
                try:
                    blob = (await client.get(candidate)).content
                except Exception:  # noqa: BLE001 - 单张失败不影响其它
                    continue
                if len(blob) < 4 * 1024:
                    continue
                digest = hashlib.sha256(blob).hexdigest()[:32]
                path = target_dir / f"{digest}.jpg"
                if not path.is_file():
                    path.write_bytes(blob)
                saved.append(
                    str(path.relative_to(Path(settings.media_root_path).parent)).replace("\\", "/")
                )
    except Exception as exc:  # noqa: BLE001
        return saved, f"{type(exc).__name__}: {exc}"
    return saved, ""


async def fetch_douyin_material(
    url: str, *, settings: Any, image_limit: int = 6
) -> DouyinMaterial:
    """抓抖音链接并**自动判断视频还是图文**，分别处理。

    为什么必须自动判断：用户实测发了一条 ``/share/note/`` 的**图文**笔记，却被当成视频处理，
    收到"没有找到可播放的视频地址（可能是图文）"——**明明是图文，却要用户自己去猜**。

    实测同一个 TikHub 接口对两类链接返回的字段完全不同，所以**从返回内容判断最可靠**
    （不靠 URL 猜）：
      * 有 ``douyinvod`` / ``.mp4`` → 视频 → 全模态理解（画面 + 字幕）；
      * 有 ``.webp`` / ``.jpg``    → 图文 → 下载图片 + **OCR 图上文字**，
        再拼上作者自己写的文案作为素材。
    """
    import httpx

    if not is_douyin_link(url):
        return DouyinMaterial(error="不是抖音链接")

    try:
        async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
            response = await client.get(
                settings.tikhub_base_url.rstrip("/") + FETCH_ONE_VIDEO,
                params={"share_url": url},
                headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
            )
    except Exception as exc:  # noqa: BLE001
        return DouyinMaterial(error=f"取内容失败：{type(exc).__name__}: {exc}")
    if response.status_code >= 400:
        return DouyinMaterial(error=f"TikHub HTTP {response.status_code}: {response.text[:120]}")
    try:
        payload = response.json()
    except ValueError as exc:
        return DouyinMaterial(error=f"返回不是 JSON：{exc}")

    caption = collect_title(payload)
    videos = collect_video_urls(payload)

    # --- 视频 ---
    if videos:
        from app.services.ai.vision import understand_video

        result = await understand_video(videos[0], settings=settings)
        if not result.ok:
            return DouyinMaterial(
                kind="video", caption=caption, error=f"视频理解失败：{result.error[:150]}"
            )
        text = f"（以下是对这个抖音视频的识别结果）\n\n{result.text}"
        if caption:
            text += f"\n\n视频自带文案：{caption}"
        logger.info("douyin video understood: %d chars", len(result.text))
        return DouyinMaterial(
            kind="video",
            text=text,
            caption=caption,
            tokens=result.prompt_tokens + result.completion_tokens,
        )

    # --- 图文 ---
    images = collect_image_urls(payload, limit=image_limit)
    if not images:
        return DouyinMaterial(
            error="这条链接里既没有视频也没有图片，拿不到内容（可能是纯文字或已删除）"
        )

    saved, download_error = await _download_images(images, settings=settings)
    if not saved:
        # 图下不下来也别放弃：作者写的文案本身往往就是完整内容。
        if caption:
            return DouyinMaterial(
                kind="images", text=f"（以下是一条抖音图文的文案）\n\n{caption}", caption=caption
            )
        return DouyinMaterial(error=f"有图片但下载失败：{download_error or '未知原因'}")

    from app.services.ai.image_stitch import stitch_images
    from app.services.ai.vision import ocr_images

    # **先拼成一张网格图再 OCR**（用户要求的做法，实测也明显更好）。
    # 小红书那边验证过：分开送多图时模型只转述一部分（576 字），
    # 拼成一张后它当一个 OCR 任务全篇转写（8116 字）——**差 14 倍**。
    # 拼图还有个附带好处：只占 1 条 QQ 消息（多图会撞被动回复条数上限）。
    stitched = stitch_images(saved, settings=settings)
    if stitched.ok:
        ocr_input = [stitched.path]
        media = [stitched.path]
        print(f"  已拼接 {stitched.count} 张为 {stitched.width}×{stitched.height}")
    else:
        # 拼图失败就退回逐张，别因此拿不到文字。
        ocr_input = saved
        media = saved
        print(f"  拼图失败（{stitched.error}），改为逐张 OCR")

    vision = await ocr_images(ocr_input, settings=settings)
    parts = ["（以下是一条抖音图文笔记的内容）"]
    if caption:
        parts.append(f"作者写的文案：{caption}")
    if vision.ok:
        parts.append(f"图片上的文字（OCR）：\n{vision.text}")
    elif vision.error:
        parts.append(f"（图上文字没识别出来：{vision.error[:80]}）")
    logger.info(
        "douyin note: %d images -> stitched=%s, caption %d chars, ocr %d chars",
        len(saved),
        stitched.ok,
        len(caption),
        len(vision.text),
    )
    return DouyinMaterial(
        kind="images",
        text="\n\n".join(parts),
        media_paths=media,
        caption=caption,
        tokens=vision.prompt_tokens + vision.completion_tokens,
    )


async def fetch_douyin_video_material(url: str, *, settings: Any) -> VideoMaterial:
    """兼容旧调用方。**图文链接也能拿到素材**（OCR + 作者文案），不再报"找不到视频"。"""
    material = await fetch_douyin_material(url, settings=settings)
    if material.ok:
        return VideoMaterial(text=material.text, title=material.caption, tokens=material.tokens)
    return VideoMaterial(error=material.error)

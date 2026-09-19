"""把一条热点组装成"原帖"和"二创"两部分，用于推送（QQ 群）。

运营方的需求是**分两条发送**：二创的图文一条、该帖原本的图文一条。所以组装的目标不是
一段摘要，而是两个自包含的 ``ComposePart``：

* ``原帖``：标题 + 正文 + 来源/作者 + **从平台下载下来的原图**
* ``二创``：小红书标题 + 正文 + 标签 + 配图方案 + **AI 新生成的图片**

**一条富媒体消息只能带一个 file_info**（官方接口如此），所以"一条"在实践中是
「一条文本 + 每张图各一条」。这一点必须如实说明，否则用户会以为只发两条。

图片只挑能用本地文件：平台原链接会过期，而且 QQ 的上传接口只吃 png/jpg
（素材库里大量 webp 会被明确跳过并给出原因，而不是静默少图）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.database import session_scope

logger = logging.getLogger(__name__)

#: 一条文本消息的字符上限（保守，官方上限随消息类型变化）。
MAX_TEXT_CHARS = 800


@dataclass
class ComposePart:
    """一部分推送内容：一段文字 + 若干张本地图片。"""

    label: str
    text: str
    image_paths: list[str] = field(default_factory=list)
    #: 被跳过的图片及原因（webp、缺文件等），如实报告而不是静默丢图。
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "text": self.text,
            "image_paths": self.image_paths,
            "skipped": self.skipped,
            "image_count": len(self.image_paths),
            "text_chars": len(self.text),
        }


def _usable_images(
    candidates: list[str], *, settings: Settings, limit: int, skipped: list[str]
) -> list[str]:
    """挑出真实存在的图片。

    **不再按格式过滤**：QQ 只吃 png/jpg，但素材库里大多是 webp，所以格式问题交给发送
    路径转换（:func:`ensure_sendable`）。这里只排除本地文件真的不在的情况——否则
    "原帖图文"会静默退化成只发文字，而那正是这个功能要解决的问题。
    """
    root = settings.media_root_path.parent
    chosen: list[str] = []
    for relative in candidates:
        if len(chosen) >= limit:
            skipped.append(f"{Path(relative).name}（超过每次 {limit} 张上限）")
            continue
        path = root / relative
        if not path.is_file():
            skipped.append(f"{Path(relative).name}（本地文件不存在）")
            continue
        chosen.append(relative)
    return chosen


def _truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 20].rstrip() + "\n…（内容较长已截断）"


async def build_push_parts(
    hot_content_id: int,
    *,
    settings: Settings | None = None,
    order: str = "rewrite_first",
    split_platforms: bool = True,
) -> list[ComposePart]:
    """组装该条要发送的内容。**不发送，也不花钱。**

    默认按运营方的要求**分平台拆开**：小红书、微博、抖音各一条，再加一条原帖——
    而不是把三平台塞进同一条里。

    图片：每个二创部分带 **AI 新生成的图 + 原帖自带的图**（去重、按 ``qq_max_images``
    截断）。原帖那条只带原图。

    ``order``：``rewrite_first``（默认，平台在前）或 ``original_first``。
    ``split_platforms=False`` 时退回旧行为（一条二创 + 一条原帖）。

    ⚠️ 消息条数：富媒体一条只能带一张图，所以每部分 = 1 文本 + N 图。三个平台 + 原帖
    很容易超过被动回复的上限（每条用户消息 5 条），调用方需要分批。
    """
    resolved = settings or get_settings()
    from app.models.ai_analysis import AiAnalysisRecord
    from app.models.ai_rewrite import AiRewriteRecord
    from app.models.hot_content import HotContentRecord

    async with session_scope(resolved) as session:
        item = (
            await session.execute(
                select(HotContentRecord).where(HotContentRecord.id == hot_content_id)
            )
        ).scalars().first()
        if item is None:
            raise ValueError(f"找不到条目 {hot_content_id}")
        rewrite = (
            await session.execute(
                select(AiRewriteRecord).where(AiRewriteRecord.hot_content_id == hot_content_id)
            )
        ).scalars().first()
        analysis = (
            await session.execute(
                select(AiAnalysisRecord).where(AiAnalysisRecord.hot_content_id == hot_content_id)
            )
        ).scalars().first()

    # ---------------- 图片池 ----------------
    original_skipped: list[str] = []
    source_images = [
        str(image.get("local_path"))
        for image in ((item.media or {}).get("images") or [])
        if isinstance(image, dict) and image.get("local_path")
    ]
    original_images = _usable_images(
        source_images, settings=resolved, limit=resolved.qq_max_images, skipped=original_skipped
    )

    generated: list[str] = []
    if rewrite is not None:
        from app.models.image_generation import ImageGenerationRecord

        async with session_scope(resolved) as session:
            for record in (
                await session.execute(
                    select(ImageGenerationRecord)
                    .where(ImageGenerationRecord.hot_content_id == hot_content_id)
                    .where(ImageGenerationRecord.status == "success")
                    .order_by(ImageGenerationRecord.id.desc())
                )
            ).scalars().all():
                generated.extend(record.local_paths or [])

    # 生成图在前：那是我们自己的素材，也是发布时优先要用的。
    def _both_kinds(skipped: list[str], limit: int) -> list[str]:
        combined: list[str] = []
        for candidate in [*generated, *original_images]:
            if candidate not in combined:
                combined.append(candidate)
        return _usable_images(combined, settings=resolved, limit=limit, skipped=skipped)

    # ---------------- ① 二创（按平台拆分） ----------------
    platforms: list[compose_platform] = []
    if rewrite is not None:
        platforms = [
            compose_platform(
                "小红书",
                rewrite.xiaohongshu_title or "",
                rewrite.xiaohongshu_content or "",
                rewrite.xiaohongshu_ending or "",
                [str(tag) for tag in (rewrite.xiaohongshu_hashtags or [])],
            ),
            compose_platform(
                "微博",
                rewrite.weibo_title or rewrite.weibo_opening or "",
                rewrite.weibo_content or "",
                "",
                [str(tag) for tag in (rewrite.weibo_hashtags or [])],
            ),
            compose_platform(
                "抖音",
                rewrite.douyin_hook or "",
                rewrite.douyin_script or "",
                rewrite.douyin_cta or "",
                [],
                scenes=[str(scene) for scene in (rewrite.douyin_scene_suggestions or [])],
                subtitles=rewrite.douyin_subtitles or "",
            ),
        ]

    rewrite_parts: list[ComposePart] = []
    if not platforms:
        rewrite_parts.append(
            ComposePart(
                label="二创",
                text=f"【二创】{item.title or ''}\n\n（这一条还没有二创。先到「二创」里生成，再推送。）",
            )
        )
    else:
        for platform in platforms:
            skipped: list[str] = []
            images = _both_kinds(skipped, resolved.qq_max_images)
            lines = [f"【{platform.name}】{platform.title or '(无标题)'}"]
            if platform.body:
                lines.append("")
                lines.append(_truncate(platform.body))
            if platform.ending:
                lines.append("")
                lines.append(_truncate(platform.ending, 200))
            if platform.hashtags:
                normalised = [tag if tag.startswith("#") else f"#{tag}" for tag in platform.hashtags]
                lines.append("")
                lines.append(" ".join(normalised))
            if platform.scenes:
                lines.append("")
                lines.append("分镜建议：")
                for index, scene in enumerate(platform.scenes[:6], start=1):
                    lines.append(f"  {index}. {scene}")
            if platform.subtitles:
                lines.append("")
                lines.append(f"字幕：{_truncate(platform.subtitles, 200)}")
            if images:
                lines.append("")
                lines.append(f"配图 {len(images)} 张（AI 生成 + 原帖自带），随后发送")
            rewrite_parts.append(
                ComposePart(
                    label=platform.name, text="\n".join(lines), image_paths=images, skipped=skipped
                )
            )
        if rewrite is not None:
            note = _review_note(rewrite, analysis)
            if note:
                rewrite_parts[0].text += note

    # ---------------- ② 原帖 ----------------
    original_lines = [
        f"【原帖】{item.title or '(无标题)'}",
        f"平台：{item.platform}　作者：{item.author or '未知'}",
    ]
    if item.url:
        original_lines.append(f"链接：{item.url}")
    if item.description:
        original_lines.append("")
        original_lines.append(_truncate(item.description))
    elif item.origin == "hot":
        original_lines.append("")
        original_lines.append("（榜单来源是词条，本身没有正文和图片）")
    if original_images:
        original_lines.append("")
        original_lines.append(f"原图 {len(original_images)} 张，随后发送")
    original_part = ComposePart(
        label="原帖",
        text="\n".join(original_lines),
        image_paths=list(original_images),
        skipped=original_skipped,
    )

    parts = rewrite_parts + [original_part]
    if order == "original_first":
        return [original_part, *rewrite_parts]
    return parts


@dataclass
class compose_platform:
    """一个平台的一份草稿（内部结构，用于按平台拆分）。"""

    name: str
    title: str = ""
    body: str = ""
    ending: str = ""
    hashtags: list[str] = field(default_factory=list)
    scenes: list[str] = field(default_factory=list)
    subtitles: str = ""


def _review_note(rewrite: Any, analysis: Any) -> str:
    """需要人工核实的提醒，附在第一部分末尾。"""
    lines: list[str] = []
    flags = [str(flag) for flag in (getattr(rewrite, "risk_flags", None) or [])]
    if flags:
        lines.append(f"风险标记：{'；'.join(flags)}")
    note = getattr(rewrite, "verification_note", "") or ""
    if note:
        lines.append(f"需核实：{_truncate(note, 300)}")
    status = getattr(rewrite, "status", "") or ""
    if status:
        lines.append(f"状态：{status}")
    if analysis is not None and getattr(analysis, "needs_verification", False):
        lines.append("⚠️ 该条被标记为需要核实，发布前请先核对事实。")
    return ("\n\n" + "\n".join(lines)) if lines else ""


__all__ = ["MAX_TEXT_CHARS", "ComposePart", "build_push_parts"]

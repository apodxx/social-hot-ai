"""把一条热点的「原帖」与「二创」图文推送到 QQ 群（Phase 12）。

**这是唯一会真的把内容发到外部的地方**，所以设计上有三条硬约束：

1. **必须先 dry-run**：``push_item(dry_run=True)`` 只组装、不发送、不花钱，返回两条的
   完整文本与图片清单。界面默认就是预览，确认后才真发。
2. **图片只发本地已有的 png/jpg**：平台原链接会过期，而 QQ 富媒体图片只吃 png/jpg——
   素材库里下载的 webp 会被**明确列出并说明原因**，不是静默少图。
3. **失败如实报告**：哪一条失败、失败在哪一步、跳过了哪张图，都进 ``PushReport``。
   绝不把"发了一部分"报成成功。

注意"两条"的实际含义：**一条富媒体消息只能带一个 ``file_info``**，所以每部分是
「1 条文本 + 每张图各 1 条」。一次推送约 1+2 条（每部分），QQ 主动消息限制是
单关系 20/qpm、1000 条/群/天，图片多的时候要注意。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.services.notification.base import SendResult
from app.services.notification.compose import ComposePart, build_push_parts

logger = logging.getLogger(__name__)


@dataclass
class PartReport:
    """一部分（原帖或二创）的发送结果。"""

    label: str
    text: str = ""
    image_paths: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    ok: bool = False
    text_parts: int = 0
    images_sent: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ok": self.ok,
            "text_parts": self.text_parts,
            "images_sent": self.images_sent,
            "image_count": len(self.image_paths),
            # The paths matter to the caller: the UI shows which pictures will be
            # attached, and a preview without them is not reviewable.
            "image_paths": list(self.image_paths),
            "skipped": self.skipped,
            "error": self.error or None,
            "text": self.text,
        }


@dataclass
class PushReport:
    """一次推送的完整结果。"""

    hot_content_id: int
    dry_run: bool = True
    ok: bool = False
    parts: list[PartReport] = field(default_factory=list)
    messages_sent: int = 0
    target: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "hot_content_id": self.hot_content_id,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "target": self.target,
            "messages_sent": self.messages_sent,
            "error": self.error or None,
            "parts": [part.as_dict() for part in self.parts],
        }


async def send_parts(
    channel: Any,
    parts: list[PartReport],
    *,
    passive_id: str = "",
    max_messages: int | None = None,
) -> int:
    """依次发送各部分，**序号跨部分连续**。返回实际发出的消息条数。

    这个函数存在的唯一理由是避免重复实现：``push_item`` 和命令行脚本都需要这段逻辑，
    而脚本最初自己写了一遍循环、忘了传 ``start_seq``，于是第二部分又从 1 开始编号、
    撞上第一部分，官方报 ``40054005 消息被去重``（真实踩到过两次）。
    被动回复到同一个 ``msg_id`` 时，所有消息共享一个序号空间。

    ``max_messages`` 对应被动回复的额度（每条用户消息最多 5 条）：达到上限就停下，
    剩下的留给下一次 @。已成功的部分标记为 ``ok``，所以**可以重复调用继续发**。

    **同一张图只上传一次**：三个平台带同一批图时本是 24 次上传，而 ``file_info`` 在
    ttl 内可复用，所以这里维护跨部分的缓存。
    """
    sequence = 0
    total = 0
    media_cache: dict[str, tuple[str, float]] = {}
    for part in parts:
        if part.ok:
            continue  # 上一次 @ 里已经发成功，不重发
        if max_messages is not None and total >= max_messages:
            break
        outcome: SendResult = await channel.send_rich(
            part.label or "",
            part.text,
            part.image_paths,
            passive_id=passive_id,
            start_seq=sequence,
            media_cache=media_cache,
        )
        part.ok = outcome.ok
        part.error = outcome.error or ""
        part.text_parts = int(outcome.detail.get("text_parts") or 0) if outcome.ok else 0
        part.images_sent = int(outcome.detail.get("images_sent") or 0) if outcome.ok else 0
        # 无论成功与否都推进序号：已发出的消息已经占用了它。
        sequence += part.text_parts + part.images_sent
        total += part.text_parts + part.images_sent
        for skipped in outcome.detail.get("skipped") or []:
            # 上传/发送阶段才发现的问题（格式、频控、群权限）也要露出来。
            part.skipped.append(str(skipped))
    return total


async def push_item(
    hot_content_id: int,
    *,
    settings: Settings | None = None,
    dry_run: bool = True,
    order: str = "rewrite_first",
    passive_id: str = "",
) -> PushReport:
    """把一条热点的原帖与二创推送到 QQ 群。

    ``dry_run=True``（默认）只组装内容，**不发送、不花钱**。

    ``passive_id`` 是**被动回复**用的 ``msg_id``：主动消息需要单独权限（实测
    ``40034105``），带上它就改用被动额度。回复同一条消息的多条内容**共享一个序号空间**，
    所以这里统一分配 ``msg_seq``——不这样做，第二部分会从 1 重新开始并撞上第一部分，
    官方报 ``40054005 消息被去重``（真实踩到过）。
    """
    resolved = settings or get_settings()
    report = PushReport(hot_content_id=hot_content_id, dry_run=dry_run)

    try:
        composed = await build_push_parts(hot_content_id, settings=resolved, order=order)
    except ValueError as exc:
        report.error = str(exc)
        return report

    report.parts = [
        PartReport(
            label=part.label,
            text=part.text,
            image_paths=part.image_paths,
            skipped=part.skipped,
        )
        for part in composed
    ]

    if dry_run:
        # 预览也算成功：调用方要的就是"给我看会发什么"。
        report.ok = True
        return report

    from app.services.notification.qq import QQBotChannel

    channel = QQBotChannel(resolved)
    target = channel.target
    report.target = f"{target[0]}:{target[1][:8]}…" if target else "未配置"

    if not channel.configured():
        report.error = "QQ 未配置完成：需要 QQ_ENABLED=true、QQ_APP_ID、QQ_APP_SECRET、QQ_GROUP_OPENID"
        return report

    logger.warning(
        "推送到 QQ（真实发送）：item=%s target=%s 两条内容",
        hot_content_id,
        report.target,
    )

    sent_total = 0
    for part, part_report in zip(composed, report.parts):
        title = "二创" if part.label == "二创" else "原帖"
        part_report.label = title
    # 序号与错误处理都在 send_parts 里，脚本与接口走同一条路径。
    total_messages = await send_parts(channel, report.parts, passive_id=passive_id)
    sent_total = sum(1 for part in report.parts if part.ok)

    report.messages_sent = total_messages
    report.ok = sent_total == len(report.parts)
    if not report.ok:
        report.error = "；".join(
            f"{part.label}：{part.error}" for part in report.parts if not part.ok and part.error
        ) or "部分内容发送失败"
    return report


__all__ = ["PartReport", "PushReport", "push_item", "send_parts"]

"""预览早中晚定时推送会发什么 —— **不发送、不花钱**。

定时推送走的是管线的 notify 阶段：把本轮二创的条目渲染成 §25 格式的摘要，再交给通知通道。
这个脚本只做前一半——**生成摘要并打印**，让你在打开开关之前就看到内容长什么样。

    python scripts/preview_scheduled_push.py            # 用最近二创出来的条目
    python scripts/preview_scheduled_push.py --fetch    # 顺便预览采集后的候选（更贴近真实轮次）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.manager import NotificationManager, format_digest  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=6, help="摘要里放几条")
    parser.add_argument("--full", action="store_true", help="同时对比审核摘要的长度")
    args = parser.parse_args()

    settings = get_settings()
    print("=== 定时推送的配置（当前）===")
    print(f"  时间点          : {settings.hot_fetch_times}")
    print(f"  时区            : {settings.scheduler_timezone}")
    print(f"  调度器          : {'开' if settings.scheduler_enabled else '关'}")
    print(f"  通知            : {'开' if settings.notification_enabled else '关'}")
    print(f"  通道            : {settings.notification_channel_names}")
    print(f"  每轮领域搜索    : {settings.watch_billed_calls_per_run} 次计费调用")
    print()

    manager = NotificationManager(settings)
    described = manager.describe()
    print("=== 通道状态 ===")
    for channel in described.get("channels", []):
        print(f"  {channel}")
    if not manager.enabled:
        print("  （通知开关是关的，下面只做渲染预览，不会发送）")
    print()

    # 取最近二创出来的条目来做摘要——正是 notify 阶段会用的那批。
    from sqlalchemy import select

    from app.db.database import session_scope
    from app.models.ai_rewrite import AiRewriteRecord
    from app.models.hot_content import HotContentRecord

    async with session_scope(settings) as session:
        rows = (
            await session.execute(
                select(AiRewriteRecord, HotContentRecord)
                .join(HotContentRecord, HotContentRecord.id == AiRewriteRecord.hot_content_id)
                .order_by(AiRewriteRecord.id.desc())
                .limit(max(1, args.limit))
            )
        ).all()

    if not rows:
        print("库里还没有二创内容——定时轮次里这一步会是空的（会发「本轮没有新的可发布内容」）。")
        return 0

    # format_digest(entries, *, max_items, title) -> str   （返回单个字符串）
    from app.services.notification.manager import format_digest_compact

    compact = format_digest_compact(
        [(rewrite, item) for rewrite, item in rows],
        max_items=args.limit,
        title="🔥 SocialHot AI 今日热点",
    )
    print("=" * 70)
    print("【速览版】定时推送实际会发的就是这个：")
    print("=" * 70)
    print(compact)
    print("=" * 70)
    print()
    # 换算成 QQ 消息条数：单条 800 字符。
    qq_parts = max(1, -(-len(compact) // 800))
    print(f"速览版：{len(compact)} 字符 → {qq_parts} 条 QQ 消息")
    if args.full:
        full = format_digest(
            [(rewrite, item) for rewrite, item in rows],
            max_items=args.limit,
            title="🔥 SocialHot AI 今日热点",
        )
        full_parts = max(1, -(-len(full) // 800))
        print(f"审核版：{len(full)} 字符 → {full_parts} 条 QQ 消息（{full_parts / qq_parts:.1f} 倍）")
    else:
        print("（加 --full 可以对比审核摘要的长度，那才是原来的格式）")
    print("**未发送任何消息，也没有产生费用。**")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

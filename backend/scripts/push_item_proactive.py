"""主动推送某一条的「二创图文 + 原帖图文」到 QQ 群 —— **不需要有人 @**。

与 ``push_to_qq.py`` 的区别：那个走**被动回复**（要先有人 @，再用 msg_id 回，受 5 分钟 /
5 条限制）；这个走**主动消息**，直接发，没有 5 分钟窗口。

主动消息的限制是频控而不是窗口：单关系（每个群）20/qpm、每群每天 1000 条。
所以一次推送的条数要留意——4 部分 × (1 文本 + N 图) 很容易超过 20 条。

    python scripts/push_item_proactive.py 489 --dry-run     # 只预览，不发送
    python scripts/push_item_proactive.py 489               # 真的发送
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.push import push_item  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="主动推送一条的图文到 QQ 群")
    parser.add_argument("hot_content_id", type=int, help="热点条目 id")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不发送")
    parser.add_argument(
        "--order", default="rewrite_first", choices=("rewrite_first", "original_first")
    )
    args = parser.parse_args()

    settings = get_settings()
    report = await push_item(
        args.hot_content_id,
        settings=settings,
        dry_run=args.dry_run,
        order=args.order,
    )
    if not report.parts:
        print(f"组装失败：{report.error}")
        return 1

    total = sum(1 + len(part.image_paths) for part in report.parts)
    print(f"条目 {args.hot_content_id}：{len(report.parts)} 部分，合计 {total} 条消息")
    print(f"目标群：{settings.qq_group_openid[:12]}…")
    print()
    for part in report.parts:
        print(f"── {part.label} ──  文本 {len(part.text)} 字，图片 {len(part.image_paths)} 张")
        print(part.text[:400] + ("…" if len(part.text) > 400 else ""))
        for path in part.image_paths:
            print(f"    [图] {path}")
        for skipped in part.skipped:
            print(f"    [跳过] {skipped}")
        print()

    # 群聊主动消息的单关系频控是 20/qpm，超了会被拒。
    if total > 20:
        print(f"⚠️ 共 {total} 条，超过群聊主动消息的 20 条/分钟限制，可能会被拒。")
        print("   可以降低 QQ_MAX_IMAGES（当前 "
              f"{settings.qq_max_images}）来减少条数。")

    if args.dry_run:
        print("**dry-run：未发送任何消息。** 去掉 --dry-run 才真的发。")
        return 0

    print(f"正在主动发送… ok={report.ok} 共发出 {report.messages_sent} 条")
    if report.error:
        print(f"错误：{report.error}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

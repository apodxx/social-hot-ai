"""Preview the two QQ messages for an item WITHOUT sending anything.

Free and offline: this only composes text and lists the images that would be attached, so
the operator can see exactly what would arrive before any credential exists. Run it after
changing the compose logic to check the wording.

    python scripts/preview_qq_push.py 489
    python scripts/preview_qq_push.py 489 --order original_first
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.push import push_item  # noqa: E402


async def main() -> int:
    item_id = int(sys.argv[1]) if len(sys.argv) > 1 else 489
    order = "rewrite_first"
    if "--order" in sys.argv:
        order = sys.argv[sys.argv.index("--order") + 1]

    settings = get_settings()
    print(f"media root : {settings.media_root_path}")
    print(f"QQ configured: enabled={settings.qq_enabled} group={bool(settings.qq_group_openid)}")
    print(f"max images : {settings.qq_max_images}")
    print()

    report = await push_item(item_id, settings=settings, dry_run=True, order=order)
    payload = report.as_dict()
    print(f"=== dry run for item {item_id} — {len(payload['parts'])} 条，未发送任何内容 ===")
    for index, part in enumerate(payload["parts"], start=1):
        print()
        print(f"----- 第 {index} 条：{part['label']} -----")
        print(part["text"])
        print(f"--- 图片：{part['image_count']} 张 ---")
        for path in part["image_paths"]:
            size = (settings.media_root_path.parent / path).stat().st_size
            print(f"    {path}  ({size / 1024:.0f} KB)")
        for skipped in part["skipped"]:
            print(f"    [跳过] {skipped}")
        if part["image_count"]:
            # 一条富媒体消息只能带一个 file_info，所以每张图都是一条消息。
            print(
                f"--- 实际会发出 {1 + part['image_count']} 条 QQ 消息"
                f"（1 条文本 + {part['image_count']} 条图片）---"
            )
    print()
    print(f"合计：{sum(1 + p['image_count'] for p in payload['parts'])} 条消息")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

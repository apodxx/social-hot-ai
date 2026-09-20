"""试：小红书分享链接 → 笔记配图。

会花 **1 次 TikHub 调用**（约 $0.0078 ≈ ¥0.057）——解析重定向本身免费，取笔记详情才计费。

    python scripts/try_xhs_link.py "https://xhslink.cn/o/8GVbJQYwbGs"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.tikhub.note_link import (  # noqa: E402
    fetch_xhs_note_images,
    is_xiaohongshu_link,
    parse_note_ref,
    resolve_note_link,
)


async def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/try_xhs_link.py <小红书分享链接> [张数]")
        return 1
    url = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    settings = get_settings()

    print(f"链接：{url}")
    print(f"是小红书链接：{is_xiaohongshu_link(url)}")

    # 先只看解析（免费），再看取图（计费）——分开看才知道钱花在哪一步。
    parsed = parse_note_ref(url)
    print(f"直接解析：note_id={parsed.note_id or '(无)'} token={'(有)' if parsed.xsec_token else '(无)'}")

    resolved = await resolve_note_link(url)
    print(f"跟随重定向：note_id={resolved.note_id or '(无)'} "
          f"token={'(有)' if resolved.xsec_token else '(无)'} err={resolved.error or '无'}")

    if not resolved.ok:
        print("解析不出 note_id，无法继续")
        return 1

    images, report = await fetch_xhs_note_images(url, settings=settings, limit=limit)
    print()
    print(f"笔记 {report.get('note_id')}：找到 {report.get('found')} 个图片地址，"
          f"下载 {report.get('saved')} 张")
    for path in images:
        full = Path(settings.media_root_path).parent / path
        size = full.stat().st_size // 1024 if full.is_file() else -1
        print(f"  ✓ {path}  ({size} KB)")
    if report.get("error"):
        print(f"  错误：{report['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

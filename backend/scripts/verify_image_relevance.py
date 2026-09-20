"""验证「配图不对题」的修复：用同一个失败案例重搜一次，看过滤效果。

会花 **1 次 TikHub 调用**（约 $0.0078），因为要拿真实的搜索结果——离线 fixture 里
没有「红黑树」那次的结果。

    python scripts/verify_image_relevance.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.tikhub.image_search import search_and_download  # noqa: E402

CASES = [
    ("红黑树 图解", "红黑树"),
    ("B+树 图解", "B+树"),
]


async def main() -> int:
    settings = get_settings()
    for query, topic in CASES:
        print("=" * 68)
        print(f"检索词 {query!r}   过滤词 {topic!r}")
        images, summary = await search_and_download(
            query, limit=12, settings=settings, topic=topic
        )
        print(
            f"  搜到 {summary.get('searched')} 条 -> 相关 {summary.get('relevant')} 条"
            f" -> 下载 {summary.get('downloaded')} 条"
        )
        for image in images:
            print(f"    ✓ 保留  {image.title[:44]}")
        for dropped in summary.get("dropped_titles") or []:
            print(f"    ✗ 剔除  {dropped}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

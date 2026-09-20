"""列出知识科普文章，标出「@小红书 会发哪一篇」。

常驻机器人回复平台指令时读的是**库里最新的一篇**（id 最大），这个脚本把这件事显示出来。

    python scripts/list_articles.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.knowledge_service import list_articles  # noqa: E402


async def main() -> int:
    settings = get_settings()
    rows, total = await list_articles(settings, limit=15)
    print(f"知识科普文章共 {total} 篇（按 id 倒序）：")
    print()
    for index, record in enumerate(rows):
        newest = index == 0
        if newest:
            print("  ┌─ 最新一篇：@小红书 / @微博 / @抖音 会发这一篇")
        prefix = "  │ " if newest else "    "
        print(f"{prefix}id={record.id}  配图={len(record.images or [])}  "
              f"¥{record.estimated_cny}  《{record.title}》")
        # **转成本地时间再显示。** ``created_at`` 是带时区的 UTC，直接用
        # ``isoformat()[:19]`` 会把偏移量切掉，看起来比实际早 8 小时——
        # 我第一版就是这么写的，把 16:43 显示成 08:43。
        if record.created_at:
            local = record.created_at.astimezone()
            stamp = local.strftime("%Y-%m-%d %H:%M:%S")
        else:
            stamp = "-"
        print(f"{prefix}     {stamp}")
        platforms = sorted((record.platforms or {}).keys())
        print(f"{prefix}     平台版本：{platforms or '（无）'}")
        if newest:
            print("  └" + "─" * 50)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

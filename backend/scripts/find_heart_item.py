"""找出「C语言爱心」那条内容，并看它现有的分析/二创里有哪些关键词。

只读数据库，免费。

    python scripts/find_heart_item.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import or_, select

from app.core.config import get_settings  # noqa: E402
from app.db.database import session_scope  # noqa: E402
from app.models.hot_content import HotContentRecord  # noqa: E402

HINTS = ("爱心", "C语言", "c语言", "跳動", "跳动", "心形")


async def main() -> int:
    settings = get_settings()
    async with session_scope(settings) as session:
        rows = (
            await session.execute(
                select(HotContentRecord)
                .where(
                    or_(
                        *[HotContentRecord.title.like(f"%{hint}%") for hint in HINTS],
                        *[HotContentRecord.description.like(f"%{hint}%") for hint in HINTS],
                    )
                )
                .order_by(HotContentRecord.id.desc())
                .limit(12)
            )
        ).scalars().all()

    print(f"匹配到 {len(rows)} 条：")
    for record in rows:
        print()
        print(f"  id={record.id}  [{record.platform}]  热度={record.hot_value}")
        print(f"  标题：{(record.title or '')[:70]}")
        if record.description:
            print(f"  正文：{(record.description or '')[:120]}")
        print(f"  链接：{(record.url or '(无)')[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

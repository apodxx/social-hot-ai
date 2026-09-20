"""统计各平台热点条目的**链接覆盖率** —— 决定"列表里能不能给链接"。

只读，不花钱。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import func, select

from app.core.config import get_settings  # noqa: E402
from app.db.database import session_scope  # noqa: E402
from app.models.hot_content import HotContentRecord  # noqa: E402


async def main() -> int:
    async with session_scope(get_settings()) as session:
        rows = (
            await session.execute(
                select(
                    HotContentRecord.platform,
                    func.count(),
                    func.count(HotContentRecord.url),
                ).group_by(HotContentRecord.platform)
            )
        ).all()

    print("平台            总数   有链接   覆盖率")
    for platform, total, with_url in rows:
        ratio = (with_url / total * 100) if total else 0
        print(f"  {platform:14} {total:5}  {with_url:5}   {ratio:5.0f}%")

    async with session_scope(get_settings()) as session:
        samples = (
            await session.execute(
                select(HotContentRecord)
                .where(HotContentRecord.url.isnot(None))
                .order_by(HotContentRecord.id.desc())
                .limit(4)
            )
        ).scalars().all()

    print()
    print("最近有链接的样例：")
    for record in samples:
        url = record.url or ""
        print(f"  [{record.platform}] {url[:100]}")

    # 没有链接的样例，看看能不能靠标题构造一个搜索链接。
    async with session_scope(get_settings()) as session:
        missing = (
            await session.execute(
                select(HotContentRecord)
                .where(HotContentRecord.url.is_(None))
                .order_by(HotContentRecord.id.desc())
                .limit(3)
            )
        ).scalars().all()
    print()
    print("没有链接的样例（只能靠标题构造搜索页）：")
    for record in missing:
        print(f"  [{record.platform}] {(record.title or '')[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

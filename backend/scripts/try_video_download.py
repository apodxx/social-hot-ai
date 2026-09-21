"""用库里真实的抖音视频链接测试「下载视频」。TikHub 1 次调用 + 下载。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select

from app.core.config import get_settings  # noqa: E402
from app.db.database import session_scope  # noqa: E402
from app.models.hot_content import HotContentRecord  # noqa: E402
from app.services.tikhub.video_link import download_video_file  # noqa: E402


async def main() -> int:
    settings = get_settings()
    async with session_scope(settings) as session:
        rows = (
            await session.execute(
                select(HotContentRecord)
                .where(
                    HotContentRecord.platform == "douyin",
                    HotContentRecord.url.like("%/share/video/%"),
                )
                .order_by(HotContentRecord.id.desc())
                .limit(1)
            )
        ).scalars().all()
    if not rows:
        print("库里没有抖音视频链接")
        return 1

    url = rows[0].url
    print(f"用：{url[:100]}")
    path, size, error = await download_video_file(url, settings=settings)
    if error:
        print(f"  失败：{error}")
        return 1
    print(f"  ✅ {path}")
    print(f"  {size // 1024 // 1024} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

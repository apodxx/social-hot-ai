"""直接调 ``convert_text_to_note``，打印 repr，用来定位"返回空"到底空在哪。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402


async def main() -> int:
    from app.services.ai.agent import convert_text_to_note

    settings = get_settings()
    result = await convert_text_to_note(
        "树莓派 5 换上了 PCIe 接口，SSD 速度翻了五倍",
        platform="xiaohongshu",
        settings=settings,
    )
    print(f"type(text)   = {type(result.text).__name__}")
    print(f"len(text)    = {len(result.text)}")
    print(f"repr(text)   = {result.text[:80]!r}")
    print(f"repr(error)  = {result.error!r}")
    print(f"ok           = {result.ok}")
    print(f"usage_cny    = {result.usage_cny}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""对比：GitHub 链接 → 小红书文案，重点验证**文案里不含链接**（平台会限流）。

花费约 ¥0.01。

    python scripts/try_github_copy.py
    python scripts/try_github_copy.py https://github.com/psf/requests
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.agent import convert_text_to_note  # noqa: E402

DEFAULT = "https://github.com/rustfs/rustfs"

#: 抓链接或裸域名。文案里出现任何一个都算违规。
LINK_PATTERN = re.compile(
    r"https?://\S+|www\.\S+|\b[a-z0-9-]+\.(?:com|cn|io|org|net|dev)\b", re.IGNORECASE
)


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    settings = get_settings()
    print(f"仓库：{url}")
    result = await convert_text_to_note(url, platform="xiaohongshu", settings=settings)
    print(f"成功: {result.ok}   花费 ¥{result.usage_cny:.4f}")
    print()
    if not result.ok:
        print(f"错误：{result.error}")
        return 1
    print("=" * 66)
    print(result.text)
    print("=" * 66)
    print()
    hits = LINK_PATTERN.findall(result.text)
    if hits:
        print(f"⚠️ 文案里出现了链接/域名（平台会限流）：{hits}")
    else:
        print("✅ 文案里没有链接或域名")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""试「链接 → 文案 + 原链接配图」：抓网页配图并下载。

只抓图，**不调用模型**，所以不花钱。用来先确认图片能不能抓到。

    python scripts/try_page_images.py https://github.com/psf/requests
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.page_images import find_image_urls, fetch_page_images  # noqa: E402


async def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/try_page_images.py <url> [张数]")
        return 1
    url = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    settings = get_settings()

    # 先看候选（不发请求下载），再看实际下载结果——分开看才知道卡在哪一步。
    import httpx

    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, trust_env=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SocialHotAI/1.0)"},
    ) as client:
        page = await client.get(url)
        print(f"页面 HTTP {page.status_code}  content-type={page.headers.get('content-type','')[:40]}")
        if page.status_code < 400 and "html" in page.headers.get("content-type", "").lower():
            candidates = find_image_urls(page.text, str(page.url))
            print(f"HTML 里找到 {len(candidates)} 个图片候选，前 5 个：")
            for candidate in candidates[:5]:
                print(f"  {candidate[:100]}")

    result = await fetch_page_images(url, settings=settings, limit=limit)
    print()
    print(f"实际下载 {len(result.paths)} 张（候选 {result.candidates}）")
    for path in result.paths:
        full = Path(settings.media_root_path).parent / path
        size = full.stat().st_size // 1024 if full.is_file() else -1
        print(f"  ✓ {path}  ({size} KB)")
    for skip in result.skipped:
        print(f"  ✗ {skip}")
    if result.error:
        print(f"  错误：{result.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

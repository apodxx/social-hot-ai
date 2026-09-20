"""诊断：小红书分享链接的正文与配图能不能通过普通网页抓取拿到。

结论预期（实测确认）：**拿不到配图**。小红书笔记页是客户端渲染的，图片在 JS 数据包里，
HTML 里没有可用的 ``<img>``。所以要抓小红书笔记的图，得走项目的 TikHub 接口。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.page_images import fetch_page_images, find_image_urls  # noqa: E402
from app.services.ai.promo_service import load_readme  # noqa: E402

URL = "https://xhslink.cn/o/8GVbJQYwbGs"


async def main() -> int:
    settings = get_settings()
    print(f"测试链接：{URL}")
    print()

    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, trust_env=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SocialHotAI/1.0)"},
    ) as client:
        page = await client.get(URL)
        print(f"最终地址：{page.url}")
        print(f"HTTP {page.status_code}  长度 {len(page.text)}  content-type={page.headers.get('content-type','')[:40]}")
        print(f"HTML 里 <img> 数量：{len(re.findall(r'<img', page.text, re.I))}")
        print(f"标题：{(re.search(r'<title[^>]*>(.*?)</title>', page.text, re.I|re.S) or [None,''])[1][:80]!r}")
        # 小红书真实图片通常挂在 sns-*.xhscdn.com 这类 CDN 上，看 HTML 里有没有出现。
        cdn = set(re.findall(r"https?://sns-[a-z0-9-]+\.xhscdn\.com/[^\s\"'<>]+", page.text))
        print(f"HTML 里出现的 xhscdn 图片地址：{len(cdn)} 个")
        for one in list(cdn)[:3]:
            print(f"   {one[:90]}")
        print(f"是否像验证/登录页：{'是' if ('验证' in page.text or '登录' in page.text) else '否'}")
        print()
        candidates = find_image_urls(page.text, str(page.url))
        print(f"find_image_urls 找到：{len(candidates)} 个")
        for one in candidates[:5]:
            print(f"   {one[:90]}")

    print()
    result = await fetch_page_images(URL, settings=settings, limit=3)
    print(f"下载结果：{len(result.paths)} 张，候选 {result.candidates}，错误={result.error or '无'}")
    for skip in result.skipped:
        print(f"  跳过 {skip}")

    print()
    try:
        text, kind, name = await asyncio.to_thread(load_readme, url=URL)
        print(f"load_readme 拿到正文 {len(text)} 字（来源 {kind}/{name}）")
        print(f"  前 120 字：{text[:120]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"load_readme 失败：{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

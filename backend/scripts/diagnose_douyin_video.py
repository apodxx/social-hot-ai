"""诊断：抖音分享链接能不能拿到**可播放的视频地址**。

拿到了才能送进全模态模型理解；拿不到就只能靠标题瞎写（用户已经指出这样没用）。

不调用模型，**免费**。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://www.iesdouyin.com/share/video/7525477523179490569/"
)

#: 抖音页面把播放地址藏在 JS 数据里；这些是常见的字段名。
VIDEO_HINTS = ("play_addr", "playAddr", "play_api", "video_url", "url_list", "master_url")
VIDEO_EXT_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.mp4[^\s\"'<>\\]*")
M3U8_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.m3u8[^\s\"'<>\\]*")


async def main() -> int:
    print(f"链接：{URL}")
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        trust_env=False,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
            )
        },
    ) as client:
        response = await client.get(URL)
    text = response.text
    print(f"最终地址：{response.url}")
    print(f"HTTP {response.status_code}  长度 {len(text)}")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    print(f"标题：{(title.group(1).strip()[:80] if title else '(无)')!r}")
    print()

    # 1) 直接找 mp4 / m3u8
    mp4 = set(VIDEO_EXT_RE.findall(text))
    m3u8 = set(M3U8_RE.findall(text))
    print(f"HTML 里直接出现的 .mp4 地址：{len(mp4)} 个")
    for one in list(mp4)[:3]:
        print(f"   {one[:110]}")
    print(f"HTML 里直接出现的 .m3u8 地址：{len(m3u8)} 个")
    for one in list(m3u8)[:3]:
        print(f"   {one[:110]}")

    # 2) 找疑似视频字段名，看数据在不在 JS 里（可能是转义过的 JSON）
    print()
    print("疑似视频字段出现次数：")
    for hint in VIDEO_HINTS:
        count = text.count(hint)
        if count:
            print(f"   {hint}: {count} 次")
            index = text.find(hint)
            print(f"      上下文：{text[max(0, index - 40):index + 90]!r}")

    # 3) 有没有内嵌的 JSON 数据块（抖音常放在 <script id="RENDER_DATA">）
    print()
    for pattern in (r'<script[^>]+id="RENDER_DATA"[^>]*>(.*?)</script>',
                    r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
                    r'_ROUTER_DATA\s*=\s*(\{.*?\});'):
        match = re.search(pattern, text, re.S)
        print(f"  {pattern[:44]:46} -> {'找到' if match else '没有'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

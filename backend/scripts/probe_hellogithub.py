"""探测 HelloGitHub 的公开 API —— 看能不能直接取到项目列表。

不依赖数据库，**免费**。

    python scripts/probe_hellogithub.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 已知/常见的候选端点。挨个试，看哪个通。
CANDIDATES = [
    ("官方 API v1 月刊列表", "https://api.hellogithub.com/v1/periodical/"),
    ("官方 API 首页", "https://api.hellogithub.com/v1/"),
    ("官网首页 HTML", "https://hellogithub.com/"),
    ("按星标排名", "https://api.hellogithub.com/v1/rating/"),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SocialHotAI/1.0)"}


def shape(node, depth=0, limit=26):
    """打印结构轮廓，不打印长文本。"""
    lines: list[str] = []

    def walk(value, pad):
        if len(lines) >= limit:
            return
        if isinstance(value, dict):
            for key, item in list(value.items())[:10]:
                if isinstance(item, (dict, list)):
                    lines.append(f"{pad}{key}: {type(item).__name__}")
                    walk(item, pad + "  ")
                else:
                    text = str(item)
                    lines.append(f"{pad}{key}: {text[:50]!r}")
        elif isinstance(value, list):
            lines.append(f"{pad}[{len(value)} 项]")
            if value:
                walk(value[0], pad + "  ")

    walk(node, "")
    return lines


async def main() -> int:
    async with httpx.AsyncClient(timeout=25.0, trust_env=False, follow_redirects=True) as client:
        for label, url in CANDIDATES:
            print("=" * 62)
            print(f"{label}  {url}")
            try:
                response = await client.get(url, headers=HEADERS)
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ {type(exc).__name__}: {exc}")
                continue
            print(f"  HTTP {response.status_code}  {len(response.content)} 字节  "
                  f"content-type={response.headers.get('content-type','')[:40]}")
            if response.status_code >= 400:
                continue
            if "json" in response.headers.get("content-type", ""):
                try:
                    payload = response.json()
                except ValueError:
                    print("  (不是合法 JSON)")
                    continue
                for line in shape(payload):
                    print(f"    {line}")
            else:
                text = response.text
                print(f"  标题: {(text.split('<title>')[1].split('</title>')[0][:60] if '<title>' in text else '(无)')!r}")
                for marker in ("__NEXT_DATA__", "window.__NUXT__", "application/json", "api.hellogithub"):
                    print(f"  含 {marker!r}: {marker in text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

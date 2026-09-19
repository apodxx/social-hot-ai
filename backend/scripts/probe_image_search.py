"""探测 TikHub 图片搜索接口的返回结构（1 次计费调用，约 $0.0078）。

写解析器必须先看到真实字段——这个项目已经因为"照文档猜字段"踩过好几次（分片序号、
`xiaohongshu_tags`、QQ 200 里带错误码）。所以先花一次调用把结构打出来。

    python scripts/probe_image_search.py [关键词]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.tikhub.client import TikHubClient  # noqa: E402

ENDPOINTS = {
    "xiaohongshu": ("/api/v1/xiaohongshu/app_v2/search_images", {"keyword": None, "page": 1}),
    "weibo": ("/api/v1/weibo/web_v2/fetch_pic_search", {"query": None, "page": 1}),
}


def _walk(node: Any, depth: int = 0, path: str = "") -> list[str]:
    """打印结构骨架：键名 + 类型 + 长度，不打印长文本。"""
    lines: list[str] = []
    pad = "  " * depth
    if isinstance(node, dict):
        for key, value in list(node.items())[:14]:
            here = f"{path}.{key}" if path else key
            if isinstance(value, dict):
                lines.append(f"{pad}{key}: dict({len(value)})")
                lines.extend(_walk(value, depth + 1, here))
            elif isinstance(value, list):
                lines.append(f"{pad}{key}: list({len(value)})")
                if value:
                    lines.extend(_walk(value[0], depth + 1, f"{here}[0]"))
            else:
                shown = str(value)
                if len(shown) > 60:
                    shown = shown[:57] + "…"
                lines.append(f"{pad}{key}: {type(value).__name__} = {shown}")
    elif isinstance(node, list) and node:
        lines.extend(_walk(node[0], depth, f"{path}[0]"))
    return lines


async def main(keyword: str) -> int:
    settings = get_settings()
    client = TikHubClient(settings)
    for name, (path, params) in ENDPOINTS.items():
        params = {key: (keyword if value is None else value) for key, value in params.items()}
        print("=" * 72)
        print(f"{name}: GET {path}  params={params}")
        try:
            payload = await client.get_json(path, params)
        except Exception as exc:  # noqa: BLE001 - 探测脚本要看到任何失败
            print(f"  失败：{type(exc).__name__}: {exc}")
            continue
        print(f"  顶层键：{list(payload) if isinstance(payload, dict) else type(payload).__name__}")
        for line in _walk(payload)[:40]:
            print("   ", line)
        # 找出所有 http 图片链接，确认能直接下载
        found: list[str] = []

        def collect(node: Any) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    collect(value)
            elif isinstance(node, list):
                for value in node:
                    collect(value)
            elif isinstance(node, str) and node.startswith("http"):
                if any(ext in node.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", "image")):
                    found.append(node)

        collect(payload)
        print(f"  -> 疑似图片链接 {len(found)} 个")
        for url in found[:6]:
            print(f"     {url[:100]}")
        print()
    await client.aclose()
    return 0


if __name__ == "__main__":
    word = sys.argv[1] if len(sys.argv) > 1 else "数据结构"
    raise SystemExit(asyncio.run(main(word)))

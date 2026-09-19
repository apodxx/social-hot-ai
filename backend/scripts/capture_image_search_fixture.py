"""抓一次小红书图片搜索的完整响应并存档（1 次计费调用，约 $0.0078）。

存盘的理由：写解析器需要反复看真实结构，而每次看都重新请求就是反复计费。存成 fixture
之后，解析器可以对着它离线开发与测试，一分钱不再花——这也是项目里其他搜索适配器的做法。

    python scripts/capture_image_search_fixture.py [关键词]
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

#: 输出到 docs/fixtures/ —— 与 tikhub_openapi.json 放一起。
FIXTURE = (
    Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "xiaohongshu_image_search.json"
)


def outline(node: Any, depth: int = 0, path: str = "", max_depth: int = 5) -> list[str]:
    """结构骨架：键名 / 类型 / 长度，不打印长文本。"""
    lines: list[str] = []
    pad = "  " * depth
    if depth > max_depth:
        return [f"{pad}…"]
    if isinstance(node, dict):
        for key, value in list(node.items())[:18]:
            here = f"{path}.{key}" if path else key
            if isinstance(value, dict):
                lines.append(f"{pad}{key}: dict")
                lines.extend(outline(value, depth + 1, here, max_depth))
            elif isinstance(value, list):
                lines.append(f"{pad}{key}: list({len(value)})")
                if value:
                    lines.extend(outline(value[0], depth + 1, f"{here}[0]", max_depth))
            else:
                shown = str(value)
                if len(shown) > 48:
                    shown = shown[:45] + "…"
                lines.append(f"{pad}{key}: {type(value).__name__} = {shown}")
    elif isinstance(node, list) and node:
        lines.extend(outline(node[0], depth, f"{path}[0]", max_depth))
    return lines


async def main(keyword: str) -> int:
    settings = get_settings()
    client = TikHubClient(settings)
    print(f"请求小红书图片搜索 keyword={keyword!r} …")
    payload = await client.get_json(
        "/api/v1/xiaohongshu/app_v2/search_images", {"keyword": keyword, "page": 1}
    )
    await client.aclose()

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已存到 {FIXTURE}（{FIXTURE.stat().st_size / 1024:.0f} KB）")
    print(f"cache_url（24 小时内免费复读）: {payload.get('cache_url')}")
    print()
    print("=== 结构 ===")
    for line in outline(payload.get("data"))[:60]:
        print(" ", line)
    return 0


if __name__ == "__main__":
    word = sys.argv[1] if len(sys.argv) > 1 else "数据结构"
    raise SystemExit(asyncio.run(main(word)))

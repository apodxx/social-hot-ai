"""把 TikHub 笔记详情的原始返回结构打出来，看图片地址到底在哪一层。

计费：1 次 TikHub 调用（约 ¥0.057）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.tikhub.note_link import resolve_note_link  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "https://xhslink.cn/o/8GVbJQYwbGs"
#: 两个端点都试：web_v3 与 app_v2 的返回结构不同。
ENDPOINTS = (
    "/api/v1/xiaohongshu/web_v3/fetch_note_detail",
    "/api/v1/xiaohongshu/app_v2/get_image_note_detail",
)


def shape(node, depth: int = 0, path: str = "") -> list[str]:
    """打印结构轮廓，不打印长文本。"""
    lines: list[str] = []
    pad = "  " * depth
    if isinstance(node, dict):
        for key, value in list(node.items())[:14]:
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}: {type(value).__name__}")
                lines.extend(shape(value, depth + 1, f"{path}.{key}"))
            else:
                text = str(value)
                lines.append(f"{pad}{key}: {text[:60]!r}")
    elif isinstance(node, list):
        lines.append(f"{pad}[{len(node)} 项]")
        if node:
            lines.extend(shape(node[0], depth + 1, path))
    return lines


async def main() -> int:
    settings = get_settings()
    link = await resolve_note_link(URL)
    print(f"note_id={link.note_id}  token={'有' if link.xsec_token else '无'}")
    print()

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        for endpoint in ENDPOINTS:
            params = {"note_id": link.note_id}
            if link.xsec_token:
                params["xsec_token"] = link.xsec_token
            response = await client.get(
                settings.tikhub_base_url.rstrip("/") + endpoint,
                params=params,
                headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
            )
            print("=" * 70)
            print(f"{endpoint}  ->  HTTP {response.status_code}")
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                print(response.text[:400])
                continue
            # 先看业务码。
            for key in ("code", "message", "msg", "detail"):
                if isinstance(payload, dict) and key in payload:
                    print(f"  {key} = {str(payload[key])[:120]}")
            print("  结构轮廓：")
            for line in shape(payload)[:40]:
                print(f"    {line}")
            # 统计一下返回里出现的 xhscdn 地址数量。
            raw = json.dumps(payload, ensure_ascii=False)
            print(f"  返回总长 {len(raw)}；出现 'xhscdn' {raw.count('xhscdn')} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

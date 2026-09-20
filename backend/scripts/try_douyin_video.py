"""试：抖音分享链接 → 视频地址 → 全模态模型理解。

两步分开测，才知道卡在哪：
  1. TikHub ``fetch_one_video_by_share_url`` 能不能给出可播放地址（1 次调用 ≈ ¥0.057）；
  2. 那个地址能不能被 DashScope 拉取并理解（1 次全模态调用）。

    python scripts/try_douyin_video.py
    python scripts/try_douyin_video.py --understand    # 多做第 2 步
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

URL = "https://www.iesdouyin.com/share/video/7525477523179490569/"
ENDPOINT = "/api/v1/douyin/app/v3/fetch_one_video_by_share_url"


def collect(node, key_hint="", found=None):
    """挖出所有像视频地址的字符串。"""
    if found is None:
        found = []
    if isinstance(node, dict):
        for key, value in node.items():
            collect(value, str(key), found)
    elif isinstance(node, list):
        for item in node:
            collect(item, key_hint, found)
    elif isinstance(node, str):
        lowered = node.lower()
        hint = key_hint.lower()
        if node.startswith("http") and (
            ".mp4" in lowered or ".m3u8" in lowered or "play" in hint or "video" in hint
        ):
            found.append((key_hint, node))
    return found


def outline(node, depth=0, limit=40, lines=None):
    if lines is None:
        lines = []
    if len(lines) >= limit:
        return lines
    pad = "  " * depth
    if isinstance(node, dict):
        for key, value in list(node.items())[:12]:
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}: {type(value).__name__}")
                outline(value, depth + 1, limit, lines)
            else:
                lines.append(f"{pad}{key}: {str(value)[:50]!r}")
    elif isinstance(node, list):
        lines.append(f"{pad}[{len(node)} 项]")
        if node:
            outline(node[0], depth + 1, limit, lines)
    return lines


async def main() -> int:
    understand = "--understand" in sys.argv
    settings = get_settings()
    async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
        response = await client.get(
            settings.tikhub_base_url.rstrip("/") + ENDPOINT,
            params={"share_url": URL},
            headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
        )
    print(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        print(response.text[:300])
        return 1
    payload = response.json()
    print(f"返回长度 {len(json.dumps(payload, ensure_ascii=False))}")
    for key in ("code", "message", "message_zh"):
        if key in payload:
            print(f"  {key} = {str(payload[key])[:100]}")

    print()
    print("结构轮廓（前 30 行）：")
    for line in outline(payload)[:30]:
        print(f"  {line}")

    candidates = collect(payload)
    print()
    print(f"疑似视频地址 {len(candidates)} 个：")
    for hint, url in candidates[:6]:
        print(f"  [{hint}] {url[:110]}")

    if understand and candidates:
        from app.services.ai.vision import understand_video

        print()
        print("送进全模态模型理解…")
        result = await understand_video(candidates[0][1], settings=settings)
        if result.ok:
            print(f"识别成功（{len(result.text)} 字，"
                  f"{result.prompt_tokens + result.completion_tokens} tokens）：")
            print(result.text[:600])
        else:
            print(f"识别失败：{result.error[:250]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

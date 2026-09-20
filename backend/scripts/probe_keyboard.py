"""探测：普通文本消息（msg_type=0）能否挂内嵌键盘（按钮）。

为什么必须实测：官方文档只在 **Markdown 消息（msg_type=2）** 的例子里演示了 ``keyboard``，
而 Markdown 需要单独权限（错误码 304036/304127 无权限）。如果按钮只能配 Markdown，
那就得先申请模板权限；如果普通文本也能挂，就能立刻用上。

主动发送，不需要有人 @。只发 1 条文本，不花钱（主动文本只占频控额度）。

    python scripts/probe_keyboard.py
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

BASE = "https://api.sgroup.qq.com"
HEADERS = {"User-Agent": "SocialHotAI/1.0 (QQBot)"}

#: 六个按钮，两行三列。label 上限 10 个字符。
KEYBOARD = {
    "content": {
        "rows": [
            {
                "buttons": [
                    {
                        "id": "cmd_xhs",
                        "render_data": {"label": "小红书", "style": 1},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "小红书"},
                    },
                    {
                        "id": "cmd_wb",
                        "render_data": {"label": "微博", "style": 1},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "微博"},
                    },
                    {
                        "id": "cmd_dy",
                        "render_data": {"label": "抖音", "style": 1},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "抖音"},
                    },
                ]
            },
            {
                "buttons": [
                    {
                        "id": "cmd_kp",
                        "render_data": {"label": "知识科普", "style": 4},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "知识科普"},
                    },
                    {
                        "id": "cmd_help",
                        "render_data": {"label": "帮助", "style": 0},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "帮助"},
                    },
                ]
            },
        ]
    }
}


async def main() -> int:
    settings = get_settings()
    if not settings.qq_group_openid:
        print("QQ_GROUP_OPENID 未配置")
        return 1

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        token = (
            await client.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": settings.qq_app_id, "clientSecret": settings.qq_app_secret},
            )
        ).json()["access_token"]
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json", **HEADERS}
        endpoint = f"{BASE}/v2/groups/{settings.qq_group_openid}/messages"

        cases = [
            ("文本 + 键盘 (msg_type=0)", {"msg_type": 0, "content": "点下面的按钮试试（文本消息挂按钮）", "keyboard": KEYBOARD}),
            ("Markdown + 键盘 (msg_type=2)", {"msg_type": 2, "markdown": {"content": "点下面的按钮试试"}, "keyboard": KEYBOARD}),
        ]
        for label, body in cases:
            response = await client.post(endpoint, json=body, headers=headers)
            ok = response.status_code < 400 and response.json().get("id")
            print(f"{label}")
            print(f"   HTTP {response.status_code}  {'✅ 接受' if ok else '❌ 被拒'}")
            print(f"   {response.text[:220]}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

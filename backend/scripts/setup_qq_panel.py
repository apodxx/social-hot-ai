"""创建/更新 QQ 机器人的**指令面板**（群里点机器人头像/菜单时弹出的指令列表）。

这是**控制台那块的 API 版本** —— 官方提供 ``POST /v2/panels``，所以面板配置可以跟着代码走，
不必到 q.qq.com 手点；换机器部署时重跑这个脚本就行。

与消息上的 ``keyboard`` 按钮的分工：
  * ``keyboard``（``qq_bot.py`` 里的 COMMAND_KEYBOARD）—— **跟着某条消息走**，
    消息划过去就找不到；好处是每条回复都顺手带一份。
  * **指令面板（本脚本）**—— **常驻**在机器人的入口里，随时能翻出来；但要单独配一次。

面板元素 ```type=command`` 的 ``name`` 会**填进聊天输入框**（和消息按钮一样），
所以 name 必须是机器人真正认得的指令。

    python scripts/setup_qq_panel.py            # 只显示现在的面板
    python scripts/setup_qq_panel.py --apply    # 创建或更新
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

BASE = "https://api.sgroup.qq.com"
HEADERS = {"User-Agent": "SocialHotAI/1.0 (QQBot)"}

#: 面板项。``name`` ≤14 字符（约 7 个汉字）、``desc`` ≤30 字符（约 15 个汉字）。
#: name 必须是 ``qq_bot.py`` 真正认得的指令，否则点了填进去、发出去机器人不认。
PANEL_ITEMS: list[dict[str, str]] = [
    {"type": "command", "name": "科普小红书", "desc": "最新一篇科普的小红书文案"},
    {"type": "command", "name": "科普微博", "desc": "最新一篇科普的微博文案"},
    {"type": "command", "name": "科普抖音", "desc": "最新一篇科普的抖音脚本"},
    {"type": "command", "name": "热点小红书", "desc": "最新热点二创的小红书文案"},
    {"type": "command", "name": "热点微博", "desc": "最新热点二创的微博文案"},
    {"type": "command", "name": "热点抖音", "desc": "最新热点二创的抖音脚本"},
    {"type": "command", "name": "今日热点", "desc": "今天的新闻，读已抓好的（免费）"},
    {"type": "command", "name": "抓最新热搜", "desc": "现在去抓最新热搜（计费）"},
    {"type": "command", "name": "知识科普列表", "desc": "给我 20 个选题，挑一个写"},
    {"type": "command", "name": "知识科普", "desc": "随机挑方向生成新文章（计费）"},
    {"type": "command", "name": "关键词：mysql", "desc": "换成你要的主题即可（计费）"},
    {"type": "command", "name": "生成3张图片", "desc": "文生图，约¥0.25/张（计费）"},
    {"type": "command", "name": "帮助", "desc": "显示全部指令说明"},
]

PANEL_REMARK = "SocialHot AI 指令面板"


async def _token_and_headers(client: httpx.AsyncClient, settings) -> dict[str, str]:
    token = (
        await client.post(
            "https://bots.qq.com/app/getAppAccessToken",
            json={"appId": settings.qq_app_id, "clientSecret": settings.qq_app_secret},
        )
    ).json()["access_token"]
    return {"Authorization": f"QQBot {token}", "Content-Type": "application/json", **HEADERS}


def _validate() -> list[str]:
    """本地先校验长度限制，省掉一次必然失败的请求。"""
    problems: list[str] = []
    for item in PANEL_ITEMS:
        if len(item["name"]) > 14:
            problems.append(f"name 超长（{len(item['name'])}>14）：{item['name']}")
        if len(item["desc"]) > 30:
            problems.append(f"desc 超长（{len(item['desc'])}>30）：{item['desc']}")
    if len(PANEL_ITEMS) > 20:
        problems.append(f"元素数 {len(PANEL_ITEMS)} 超过每面板上限 20")
    return problems


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的创建/更新")
    parser.add_argument("--scope", default="group", choices=("group", "c2c", "channel", "dm"))
    args = parser.parse_args()

    problems = _validate()
    if problems:
        print("本地校验没通过：")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1
    print(f"本地校验通过：{len(PANEL_ITEMS)} 项，名称最长 "
          f"{max(len(i['name']) for i in PANEL_ITEMS)}/14，描述最长 "
          f"{max(len(i['desc']) for i in PANEL_ITEMS)}/30")
    print()

    settings = get_settings()
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        headers = await _token_and_headers(client, settings)

        # 先看现有的面板，避免重复创建（每机器人最多 20 个）。
        # **列表接口必须带 scope**，否则返回 40030011「生效场景不合法」。
        existing = await client.get(
            f"{BASE}/v2/panels", params={"scope": args.scope}, headers=headers
        )
        print(f"GET /v2/panels -> HTTP {existing.status_code}")
        panels = []
        if existing.status_code < 400:
            body = existing.json()
            # 顶层键是 **records**（不是 panels）——实测确认，别猜。
            panels = body if isinstance(body, list) else (body.get("records") or [])
            for panel in panels:
                items = (panel.get("panel") or {}).get("items") or []
                print(f"  已有面板 {panel.get('panel_id')}  scope={panel.get('scope')} "
                      f"target={panel.get('target_type')}  指令 {len(items)} 项")
        else:
            print(f"  {existing.text[:200]}")
        print()

        payload = {
            "scope": args.scope,
            "target_type": "all",
            "panel": {"items": PANEL_ITEMS, "remark": PANEL_REMARK},
        }
        print("将要提交的面板项：")
        for index, item in enumerate(PANEL_ITEMS, 1):
            print(f"  {index}. {item['name']:12} — {item['desc']}")
        print()

        if not args.apply:
            print("（没加 --apply，只做了展示；加 --apply 才会真的创建）")
            return 0

        # 同一 scope 的 all 面板重新创建会报冲突，所以先删掉旧的。
        for panel in panels:
            if panel.get("scope") == args.scope and panel.get("target_type") == "all":
                panel_id = panel.get("panel_id")
                removed = await client.delete(f"{BASE}/v2/panels/{panel_id}", headers=headers)
                print(f"  删除旧面板 {panel_id} -> HTTP {removed.status_code}")

        created = await client.post(f"{BASE}/v2/panels", json=payload, headers=headers)
        print(f"POST /v2/panels -> HTTP {created.status_code}")
        print(f"  {created.text[:300]}")
        if created.status_code < 400:
            print()
            print(f"✅ 面板已创建：{created.json().get('panel_id')}")
            print("   在群里点机器人的入口即可看到这些指令。")
            return 0
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

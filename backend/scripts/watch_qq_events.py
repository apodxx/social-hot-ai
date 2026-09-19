"""Listen on the official QQ Bot gateway and print the group_openid.

``group_openid`` is not shown in the developer console — it is delivered with an event.
``GET /v2/users/@me/groups`` does not exist for this bot (404 / code 11001), so the only
remaining official ways are a webhook (needs public HTTPS, which this machine has not got)
or the gateway WebSocket. This is the gateway.

Usage:

    python scripts/watch_qq_events.py <AppID> <AppSecret> [--sandbox]

Then **@ the bot in your group** and send anything. The script prints the group_openid and
exits, so it writes nothing to disk and needs no long-running process.

Only ``GROUP_AT_MESSAGE_CREATE`` / ``C2C_MESSAGE_CREATE`` are printed; no message is sent
and nothing is billed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import httpx

PRODUCTION = "https://api.sgroup.qq.com"
SANDBOX = "https://sandbox.api.sgroup.qq.com"

#: Intents: group + C2C messages (1<<25), plus public guild messages (1<<30) so a
#: channel message also reveals the channel id if that is what the bot is in.
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_PUBLIC_MESSAGES = 1 << 30
#: 0 = dispatch, 1 = heartbeat, 2 = identify, 6 = resume, 10 = hello, 11 = heartbeat ack.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_HELLO = 10

#: The gateway refuses connections without a User-Agent that looks like a bot.
HEADERS = {"User-Agent": "SocialHotAI/1.0 (QQBot)"}


async def _token(client: httpx.AsyncClient, app_id: str, secret: str) -> str:
    response = await client.post(
        "https://bots.qq.com/app/getAppAccessToken",
        json={"appId": app_id, "clientSecret": secret},
    )
    body: Any = response.json()
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise RuntimeError(f"没有拿到 token：{json.dumps(body, ensure_ascii=False)[:200]}")
    return str(token)


async def _heartbeat(websocket: Any, interval: float) -> None:
    """Keep the connection alive; the server closes it without heartbeats."""
    while True:
        await asyncio.sleep(interval)
        try:
            await websocket.send(json.dumps({"op": OP_HEARTBEAT, "d": None}))
        except Exception:  # noqa: BLE001 - the main loop will surface a closed socket
            return


async def main(app_id: str, secret: str, *, sandbox: bool) -> int:
    import websockets

    base = SANDBOX if sandbox else PRODUCTION
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        token = await _token(client, app_id, secret)
        print(f"token 已获取（{len(token)} 字符），环境：{'沙箱' if sandbox else '生产'}")

        gateway = await client.get(f"{base}/gateway", headers={"Authorization": f"QQBot {token}", **HEADERS})
        if gateway.status_code >= 400:
            print(f"取网关地址失败：HTTP {gateway.status_code} {gateway.text[:200]}")
            return 1
        url = gateway.json().get("url")
        print(f"网关地址：{url}")

    intents = INTENT_GROUP_AND_C2C | INTENT_PUBLIC_MESSAGES
    async with websockets.connect(url, additional_headers=HEADERS) as websocket:
        hello = json.loads(await websocket.recv())
        if hello.get("op") != OP_HELLO:
            print(f"握手异常：{hello}")
            return 1
        interval = float(hello.get("d", {}).get("heartbeat_interval") or 40000) / 1000
        print(f"已连接（心跳 {interval:.1f}s），正在等待事件…")
        print()
        print(">>> 现在请到你的 QQ 群里 @机器人 并随便发一句话 <<<")
        print()

        await websocket.send(
            json.dumps(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": intents,
                        "shard": [0, 1],
                        "properties": {"$os": "windows", "$browser": "socialhot", "$device": "socialhot"},
                    },
                }
            )
        )

        heartbeat = asyncio.create_task(_heartbeat(websocket, interval))
        found = False
        try:
            while True:
                raw = await asyncio.wait_for(websocket.recv(), timeout=600)
                event = json.loads(raw)
                if event.get("op") != OP_DISPATCH:
                    continue
                kind = event.get("t") or ""
                data = event.get("d") or {}
                if kind == "READY":
                    print("机器人已就绪，等待群消息…")
                    continue
                if kind == "GROUP_AT_MESSAGE_CREATE":
                    group_openid = data.get("group_openid")
                    print("=" * 60)
                    print("✅ 收到群消息事件")
                    print(f"   group_openid : {group_openid}")
                    print(f"   发送者 openid: {data.get('author', {}).get('member_openid')}")
                    print(f"   消息内容     : {str(data.get('content'))[:60]}")
                    print("=" * 60)
                    print()
                    print("把这个值写进 .env：")
                    print(f"   QQ_GROUP_OPENID={group_openid}")
                    found = True
                    break
                if kind == "C2C_MESSAGE_CREATE":
                    print("收到**单聊**消息（不是群）：")
                    print(f"   user_openid  : {data.get('author', {}).get('user_openid')}")
                    print("   （单聊可以发文本，但图片富媒体需要群聊）")
                if kind in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"):
                    print(f"[{kind}] group_openid={data.get('group_openid')}")
        except asyncio.TimeoutError:
            print("10 分钟没有收到事件。请确认：")
            print("  1. 机器人已被拉进群（群设置 → 群机器人）")
            print("  2. 你在群里 @ 了它并发送了消息")
            print("  3. 后台「事件订阅」里已勾选群聊消息（GROUP_AT_MESSAGE_CREATE）")
        finally:
            heartbeat.cancel()

    return 0 if found else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python scripts/watch_qq_events.py <AppID> <AppSecret> [--sandbox]")
        raise SystemExit(2)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(
        asyncio.run(main(sys.argv[1], sys.argv[2], sandbox="--sandbox" in sys.argv))
    )

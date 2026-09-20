"""一次性把「二创图文」和「原帖图文」推送到 QQ 群 —— 走被动回复。

为什么不是"随时主动推送"：实测直接发会得到

    HTTP 400  code=40034105  主动消息失败, 无权限

QQ 机器人的**主动消息需要单独权限**，未开通时只能**被动回复**——即带上用户那条消息的
``msg_id``，官方称"被动消息"。群聊里被动回复有效期 5 分钟、每条消息最多回 5 条。
所以流程是：

1. 脚本连上网关等待；
2. **你在群里 @机器人 发一句话**；
3. 脚本立刻用那条消息的 ``msg_id`` 把两条图文作为回复发出。

要发的内容超过 5 条时（每张图占 1 条），脚本会如实告诉你发了几条、还剩哪些没发，
你可以再 @ 一次继续。也可以到 q.qq.com 后台开通主动消息权限，那样就能随时推送。

    python scripts/push_to_qq.py 489
    python scripts/push_to_qq.py 489 --sandbox
    python scripts/push_to_qq.py 489 --max-images 1     # 控制在 5 条以内
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.qq import QQBotChannel  # noqa: E402
from app.services.notification.push import push_item, send_parts  # noqa: E402

PRODUCTION = "https://api.sgroup.qq.com"
SANDBOX = "https://sandbox.api.sgroup.qq.com"
INTENT_GROUP_AND_C2C = 1 << 25
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_HELLO = 10
#: 群聊被动回复的官方**文档**上限是每条用户消息 5 条。但实测：第 4 条就被拒
#: （``40054005 消息被去重``，而 msg_seq 确实是递增的）——说明这个上限**实际比文档更紧**。
#: 所以按实测值来：3 条。
MAX_PASSIVE_REPLIES = int(os.environ.get("QQ_PASSIVE_BATCH", "3"))
#: 网关会主动断开空闲连接，所以断线要能重连而不是抛栈。
MAX_RECONNECTS = 5
HEADERS = {"User-Agent": "SocialHotAI/1.0 (QQBot)"}


async def _heartbeat(websocket: Any, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await websocket.send(json.dumps({"op": OP_HEARTBEAT, "d": None}))
        except Exception:  # noqa: BLE001
            return


async def _access_token(settings: Any) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.post(
            "https://bots.qq.com/app/getAppAccessToken",
            json={"appId": settings.qq_app_id, "clientSecret": settings.qq_app_secret},
        )
        token = response.json().get("access_token")
    if not token:
        raise RuntimeError("无法获取 QQ access token")
    return str(token)


async def main(hot_content_id: int, *, sandbox: bool, max_images: int | None) -> int:
    import httpx
    import websockets

    settings = get_settings()
    if max_images is not None:
        settings = settings.model_copy(update={"qq_max_images": max_images})

    report = await push_item(hot_content_id, settings=settings, dry_run=True)
    if not report.parts:
        print(f"组装失败：{report.error}")
        return 1

    total = sum(1 + len(part.image_paths) for part in report.parts)
    print(f"准备推送 {hot_content_id}：{len(report.parts)} 部分，合计 {total} 条消息")
    for part in report.parts:
        print(f"  · {part.label}：文本 + {len(part.image_paths)} 张图")
    batches = -(-total // MAX_PASSIVE_REPLIES)
    print()
    print(f"被动回复每条消息最多 {MAX_PASSIVE_REPLIES} 条，所以需要 **{batches} 次 @** 才能发完。")
    print("脚本会在每次 @ 时自动接着发下一批，发完为止。")
    print("（想一次发完，请到 q.qq.com 后台开通主动消息权限。）")
    print()

    token = await _access_token(settings)
    base = SANDBOX if sandbox else PRODUCTION

    # 网关会**主动断开空闲连接**（实测 "no close frame received or sent"）。第一版直接把
    # 这个异常抛成 traceback：用户在等消息，却只看到一个栈。所以这里重连，并在断开时
    # 明确说明"连接断了、正在重连、你的 @ 请再发一次"，而不是假装一切正常。
    attempt = 0
    while True:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
                gateway = await client.get(
                    f"{base}/gateway", headers={"Authorization": f"QQBot {token}", **HEADERS}
                )
                url = gateway.json().get("url")
            if attempt == 1:
                print(f"网关：{url}（{'沙箱' if sandbox else '生产'}）")
            done = await _session(websockets, url, token, settings, report)
            return 0 if done else 1
        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            if attempt >= MAX_RECONNECTS:
                print()
                print(f"连接反复断开（{type(exc).__name__}: {exc}），已重试 {attempt} 次，放弃。")
                print("可以重新运行本脚本——已成功发出的部分不会重发。")
                return 1
            print()
            print(f"⚠️ 与 QQ 网关的连接断开了（{type(exc).__name__}），正在重连（第 {attempt} 次）…")
            print("   断线期间如果你 @ 过机器人，那一次会漏掉——重连后请**再 @ 一次**。")
            await asyncio.sleep(2)


async def _session(websockets: Any, url: str, token: str, settings: Any, report: Any) -> bool:
    """一次网关会话。返回 True 表示全部发完。"""
    # proxy=None 是必须的：websockets 会读 Windows 注册表的系统代理（本机是
    # 127.0.0.1:7890 的 Clash），绕过去就会"连上了却收不到事件"。项目其他地方
    # 一律 trust_env=False，这里同理。
    async with websockets.connect(
        url, additional_headers=HEADERS, ping_interval=20, proxy=None
    ) as websocket:
        hello = json.loads(await websocket.recv())
        interval = float(hello.get("d", {}).get("heartbeat_interval") or 40000) / 1000
        await websocket.send(
            json.dumps(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": INTENT_GROUP_AND_C2C,
                        "shard": [0, 1],
                        "properties": {"$os": "windows", "$browser": "socialhot", "$device": "socialhot"},
                    },
                }
            )
        )
        print()
        print(">>> 现在请在目标群里 @机器人 并发送任意一句话 <<<")
        print()

        heartbeat = asyncio.create_task(_heartbeat(websocket, interval))
        try:
            while True:
                event = json.loads(await websocket.recv())
                if event.get("op") != OP_DISPATCH:
                    continue
                if event.get("t") == "READY":
                    print("机器人已就绪，等待你的 @ …")
                    continue
                if event.get("t") != "GROUP_AT_MESSAGE_CREATE":
                    continue

                data = event.get("d") or {}
                message_id = str(data.get("id") or "")
                group_openid = str(data.get("group_openid") or "")
                print(f"收到 @（group={group_openid[:8]}… msg_id={message_id[:8]}…），开始回复…")

                active_settings = settings
                if group_openid and group_openid != settings.qq_group_openid:
                    # 被 @ 的群和配置的目标不一致时如实说明，而不是发错地方。
                    active_settings = settings.model_copy(update={"qq_group_openid": group_openid})
                    print(f"  注意：群 id 与配置不同，本次改用 {group_openid}")

                channel = QQBotChannel(active_settings)
                # 走共享的 send_parts：序号跨部分连续，且同一张图只上传一次。
                # max_messages 限制在被动额度内，剩下的部分等下一次 @ 继续。
                used = await send_parts(
                    channel, report.parts, passive_id=message_id, max_messages=MAX_PASSIVE_REPLIES
                )
                remaining = [part.label for part in report.parts if not part.ok]
                for part in report.parts:
                    mark = "✅" if part.ok else "⏳"
                    print(
                        f"  {mark} {part.label}: 文本={part.text_parts} 图片={part.images_sent}"
                    )
                    if part.error:
                        print(f"      error: {part.error}")
                    for skipped in part.skipped:
                        print(f"      [跳过] {skipped}")

                print()
                print(f"本次回复发出 {used} 条（额度 {MAX_PASSIVE_REPLIES} 条）")
                if not remaining:
                    print("全部发完，请到群里确认。")
                    return True
                print(f"还剩 {len(remaining)} 部分：{'、'.join(remaining)}")
                print()
                print(f">>> 请再 @机器人 一次继续发送（{MAX_PASSIVE_REPLIES} 条额度会刷新）<<<")
                print()
        finally:
            heartbeat.cancel()


if __name__ == "__main__":
    item = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 489
    images = None
    if "--max-images" in sys.argv:
        images = int(sys.argv[sys.argv.index("--max-images") + 1])
    raise SystemExit(
        asyncio.run(main(item, sandbox="--sandbox" in sys.argv, max_images=images))
    )

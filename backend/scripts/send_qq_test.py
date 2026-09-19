"""Send ONE text message to the configured QQ group — the smallest real verification.

Deliberately not ``POST /api/notification/test``: that endpoint is gated by
``NOTIFICATION_ENABLED``, which controls the *pipeline's* notify stage and is a separate
decision. This exercises exactly the delivery path the push feature uses
(:class:`QQBotChannel` -> token -> group message) with a single message, so the first
thing that ever appears in the operator's group is one short line, not nine.

    python scripts/send_qq_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.qq import QQBotChannel  # noqa: E402


async def main() -> int:
    settings = get_settings()
    channel = QQBotChannel(settings)
    target = channel.target
    print(f"target      : {target[0] if target else '未配置'}")
    print(f"base_url    : {channel.base_url}")
    print(f"configured  : {channel.configured()}")
    if not channel.configured():
        print("QQ 未配置完成，退出。")
        return 1

    print()
    print("正在发送 1 条测试消息…")
    result = await channel.send(
        "SocialHot AI 连通性测试",
        "这是一条测试消息，用于确认机器人能向本群发送文本。\n"
        "接下来会推送「二创图文」与「原帖图文」两条内容。",
    )
    print(f"ok={result.ok} parts={result.parts} status={result.status_code}")
    if result.error:
        print(f"error: {result.error}")
    if result.detail:
        print(f"detail: {result.detail}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

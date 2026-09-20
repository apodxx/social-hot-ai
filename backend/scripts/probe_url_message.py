"""实测 QQ 是否接受**含 URL** 的消息（错误码 40054010「不允许发送URL」）。

主动发送，不需要有人 @。只发 1 条文本，不花钱。

    python scripts/probe_url_message.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.qq import QQBotChannel  # noqa: E402


def load_bot():
    path = Path(__file__).resolve().parent / "qq_bot.py"
    spec = importlib.util.spec_from_file_location("qq_bot", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qq_bot"] = module
    spec.loader.exec_module(module)
    return module


async def main() -> int:
    bot = load_bot()
    settings = get_settings()
    items, note = await bot.hot_items_for_period(settings, "all", 3)
    if not items:
        print("库里没有条目")
        return 1
    body = bot.format_hot_list(items, note, fetched=False)
    print("准备发送的内容：")
    print(body)
    print()
    print(f"字数 {len(body)}，含 {body.count('http')} 个链接")
    print()

    channel = QQBotChannel(settings)
    result = await channel.send("链接实测", body)
    print(f"发送结果：ok={result.ok} status={result.status_code} parts={result.parts}")
    if result.error:
        print(f"错误：{result.error}")
        print()
        print("→ 如果错误码是 40054010，说明 QQ 不允许机器人发 URL，")
        print("  那就只能退回「只给标题 + 让用户自己去平台搜」。")
    else:
        print("→ QQ 接受含链接的消息 ✓")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

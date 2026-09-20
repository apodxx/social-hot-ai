"""把机器人的帮助文案打印出来，并可选择主动发到群里。

用途：改过指令集之后，想立刻在群里看到新的帮助，不必先 @ 一次。
主动发送不需要有人 @，占主动消息频控额度（单关系 20/qpm）。

    python scripts/send_help.py            # 只打印
    python scripts/send_help.py --send     # 顺手发到群里
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402


def load_bot_module():
    """把 qq_bot.py 当模块加载，复用它的 HELP_TEXT 与按钮定义（避免复制一份文案）。"""
    path = Path(__file__).resolve().parent / "qq_bot.py"
    spec = importlib.util.spec_from_file_location("qq_bot", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qq_bot"] = module
    spec.loader.exec_module(module)
    return module


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="顺手主动发到群里")
    args = parser.parse_args()

    bot = load_bot_module()
    print("=" * 60)
    print(bot.HELP_TEXT)
    print("=" * 60)
    print()
    print("覆盖检查：")
    required = [
        "科普小红书", "科普微博", "科普抖音",
        "热点小红书", "热点微博", "热点抖音",
        "知识科普", "关键词", "帮助",
    ]
    missing = [token for token in required if token not in bot.HELP_TEXT]
    print(f"  应包含 {len(required)} 项，缺失：{missing or '无 ✅'}")

    rows = bot.COMMAND_KEYBOARD["content"]["rows"]
    labels = [b["render_data"]["label"] for row in rows for b in row["buttons"]]
    print(f"  按钮 {len(labels)} 个：{labels}")

    if not args.send:
        print()
        print("（加 --send 可主动发到群里）")
        return 0 if not missing else 1

    from app.services.notification.qq import QQBotChannel

    settings = get_settings()
    channel = QQBotChannel(settings)
    if not channel.configured():
        print("QQ 未配置，无法发送")
        return 1
    result = await channel.send("指令帮助", bot.HELP_TEXT, keyboard=bot.COMMAND_KEYBOARD)
    print()
    print(f"主动发送：ok={result.ok} parts={result.parts} status={result.status_code}")
    if result.error:
        print(f"错误：{result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

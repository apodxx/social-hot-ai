"""试一下意图识别（function calling）：随便说一句话，看它选哪个工具、抽出什么参数。

**每次会调用一次 DeepSeek**（路由本身约 ¥0.001-0.003）。不执行工具、不生成文案，
所以不会触发那些更贵的步骤。

    python scripts/try_intent.py
    python scripts/try_intent.py "把这段话改成小红书文案：树莓派5 用了新的 PCIe 接口"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.agent import route_intent  # noqa: E402

#: 覆盖几种典型说法：明确指令、贴文本、贴链接、闲聊、含糊。
CASES: list[str] = [
    "帮我把这段话改成小红书文案：树莓派 5 换上了 PCIe 接口，SSD 速度翻了五倍。",
    "https://github.com/xxx/yyy 这个项目帮我宣传一下，写个微博",
    "写一篇关于 mysql 索引的科普",
    "发最新的那篇给我看看",
    "你能干什么？",
    "今天天气不错啊",
    "小红书",  # 这是明确指令，不该走意图识别
]


async def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="*", help="要试的话；不传则跑内置用例")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真的执行选中的工具（会花钱；convert_text_to_note 约 ¥0.01）",
    )
    args = parser.parse_args()
    cases = args.text or CASES

    for text in cases:
        result = await route_intent(text, settings=settings)
        print("=" * 66)
        print(f"用户说：{text}")
        if result.error:
            print(f"  ❌ 出错：{result.error}")
        elif result.call is None:
            print("  （没选工具）")
            print(f"  模型回复：{result.message[:120]}")
        else:
            print(f"  → 工具：{result.call.name}")
            for key, value in result.call.arguments.items():
                shown = str(value)
                shown = shown[:60] + "…" if len(shown) > 60 else shown
                print(f"     {key} = {shown}")
        print(f"  路由花费 ≈ ¥{result.usage_cny:.4f}")

        if args.execute and result.call and result.call.name == "convert_text_to_note":
            from app.services.ai.agent import convert_text_to_note

            arguments = result.call.arguments
            conversion = await convert_text_to_note(
                str(arguments.get("text") or ""),
                platform=str(arguments.get("platform") or "xiaohongshu"),
                settings=settings,
            )
            print()
            print(f"  【执行结果】成功={conversion.ok}  花费 ≈ ¥{conversion.usage_cny:.4f}")
            print("  " + "-" * 60)
            body = conversion.text or f"错误：{conversion.error}"
            for line in body.splitlines():
                print(f"  {line}")
            print("  " + "-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

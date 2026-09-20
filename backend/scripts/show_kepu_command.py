"""`科普小红书` 这条指令到底做什么 —— 把它的实际输出打出来。**不发送、不花钱。**"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402


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
    command = "科普小红书"

    print(f"指令：{command}")
    print(f"映射到平台键：{bot.PLATFORM_COMMANDS.get(command)}")
    print()

    record = await bot.latest_article(settings)
    if record is None:
        print("库里没有文章 —— 这种情况会回一句提示")
        return 0

    print(f"取的是**最新一篇**（id 最大）：id={record.id} 《{record.title}》")
    print(f"  创建时间 {record.created_at.astimezone().strftime('%Y-%m-%d %H:%M')}")
    print(f"  它当初生成花了 ¥{record.estimated_cny}")
    print(f"  库里存了 {len(record.images or [])} 张配图")
    print()

    text = bot.platform_text(record, "xiaohongshu")
    print("=" * 66)
    print("实际会发出的文字：")
    print("=" * 66)
    print(text)
    print("=" * 66)
    print(f"（{len(text)} 字符）")
    print()

    picked = bot.images_for(record.images or [], 3, settings)
    print(f"实际会发出的图片：{len(picked)} 张（上限 3）")
    for path in picked:
        full = Path(settings.media_root_path).parent / path
        size = full.stat().st_size // 1024 if full.is_file() else -1
        print(f"  ✓ {path}  ({size} KB)")
    print()
    print("**以上全部不花钱**（只读数据库）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

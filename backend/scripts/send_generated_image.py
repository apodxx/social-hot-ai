"""把一张已经生成好的图发到 QQ 群，供人工确认风格。主动发送，不花钱。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.qq import QQBotChannel  # noqa: E402


async def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/send_generated_image.py <素材库相对路径>")
        return 1
    relative = sys.argv[1]
    settings = get_settings()
    root = Path(settings.media_root_path).parent
    full = root / relative
    if not full.is_file():
        print(f"找不到 {full}")
        return 1
    channel = QQBotChannel(settings)
    result = await channel.send_rich(
        "文生图效果",
        "这是一张文生图（无参考图）的结果，约 ¥0.25/张。看看风格行不行。",
        [relative],
    )
    print(f"发送：ok={result.ok} status={result.status_code}")
    if result.error:
        print(f"错误：{result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

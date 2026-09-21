"""验证新风格（白底手绘 + 短中文批注）实际画出来什么样。

**只生成一张**（约 ¥0.25）。生成后自己去 media/generated 看图。

    python scripts/try_handdrawn_style.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.agent import image_prompts_from  # noqa: E402
from app.services.ai.image_gen import QwenImageClient, download_generated  # noqa: E402

COPY = """学生党买笔记本，最怕的不是贵，是买错了用两年就卡。
1. 类型先定下来：选轻薄本。轻便好带，天天背去教室、图书馆、挤地铁都不累。
2. 续航要够用。白天满课、晚上回宿舍追剧剪视频，中途找插座很崩溃。"""


async def main() -> int:
    settings = get_settings()
    print(f"目标尺寸：{settings.image_gen_size}")
    prompts = await image_prompts_from(COPY, count=1, settings=settings)
    prompt = prompts[0]
    print(f"提示词：{prompt[:120]}…")
    print()

    client = QwenImageClient(settings)
    try:
        result = await client.edit_image(prompt=prompt, reference_paths=[])
    finally:
        await client.aclose()
    if not result.ok:
        print(f"失败：{result.error}")
        return 1
    paths, errors = await download_generated(result.urls, settings=settings)
    for error in errors:
        print(f"下载失败：{error}")
    root = Path(settings.media_root_path).parent
    for path in paths:
        full = root / path
        print(f"  ✓ {path}  ({full.stat().st_size // 1024} KB)")
    if paths:
        print()
        print("去看这张图：")
        print(f"  {root / paths[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

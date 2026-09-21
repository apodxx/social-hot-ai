"""连画 2 张新风格配图并**发到群里** —— 验证 shot list 是否真的产出不同概念。约 ¥0.5。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.agent import image_prompts_from  # noqa: E402
from app.services.ai.image_gen import QwenImageClient, download_generated  # noqa: E402
from app.services.notification.qq import QQBotChannel  # noqa: E402

COPY = """学生党买笔记本，最怕的不是贵，是买错了用两年就卡。
1. 类型先定下来：选轻薄本。轻便好带，天天背去教室、图书馆、挤地铁都不累。
2. 续航要够用。白天满课、晚上回宿舍追剧剪视频，中途找插座很崩溃。
3. 按预算挑：3500以下适合简单办公；3500-4500 性价比不错，配置缩水不多。"""


async def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    settings = get_settings()
    prompts = await image_prompts_from(COPY, count=count, settings=settings)
    print(f"设计出 {len(prompts)} 张图的提示词：")
    for index, prompt in enumerate(prompts, start=1):
        print(f"  第 {index} 张：{prompt[:90]}…")
    print()

    channel = QQBotChannel(settings)
    client = QwenImageClient(settings)
    sent_total = 0
    try:
        for index, prompt in enumerate(prompts, start=1):
            print(f"正在画第 {index}/{len(prompts)} 张…")
            result = await client.edit_image(prompt=prompt, reference_paths=[])
            if not result.ok:
                print(f"  失败：{result.error}")
                continue
            paths, errors = await download_generated(result.urls, settings=settings)
            for error in errors:
                print(f"  下载失败：{error}")
            if not paths:
                continue
            sent_total += len(paths)
            ok = await channel.send_rich("", "", paths, images_only=True)
            print(f"  已发（ok={ok.ok}，{result.elapsed_ms / 1000:.0f}s）")
    finally:
        await client.aclose()
    print()
    print(f"共发出 {sent_total} 张。请检查：① 两张概念是否不同 ② 中文批注有没有错字")
    if sent_total:
        await channel.send(
            "[配图测试]",
            f"这是白底手绘风格的第 {sent_total} 张测试图（约 ¥{0.25 * sent_total:.2f}）。\n"
            "请检查两点：① 两张图讲的是不是不同的点 ② 图上的中文批注有没有错字。\n"
            "如果有错字，告诉我错在哪，我会调整（减少批注条数或去掉批注）。",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

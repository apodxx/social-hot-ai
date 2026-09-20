"""试：**不带参考图**只给文案，能不能用现有模型生成图片（即文生图）。

如果能，用户要的"生成3张图片"就不需要接新模型；如果不能，就得换 t2i 模型。

**会花约 ¥0.25（一张 1K 图）。只生成一张。**
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.image_gen import QwenImageClient  # noqa: E402

PROMPT = (
    "一张小红书风格的封面图：浅色背景，画面中央是一张书桌，"
    "桌上摆着手机、平板和笔记本电脑，柔和光线，干净简洁，"
    "竖版构图，留出上方空间用于放标题文字。不要出现任何文字。"
)


async def main() -> int:
    settings = get_settings()
    print(f"模型：{settings.qwen_image_model}")
    print(f"端点：{settings.image_gen_endpoint}")
    print(f"尺寸：{settings.image_gen_size}")
    print()
    # **按 qq_bot 里一模一样的调用方式走一遍。**
    # 上一版只测了"直接给提示词"，没测 image_prompt_from 这一步，结果线上因为
    # 参数传法不对（settings 是关键字专用）而 TypeError。教训：
    # **测试要覆盖"代码实际会走的那条路"，而不是我方便构造的那条。**
    from app.services.ai.agent import image_prompt_from

    material = " ".join(sys.argv[1:]) or (
        "准大一新生开学前怎么选电子设备：手机看内存和续航，平板用来上课记笔记，"
        "电脑文科选轻薄本、工科要跑模型。"
    )
    print(f"素材：{material[:60]}…")
    prompt = await image_prompt_from(material, settings=settings)
    print(f"生成提示词：{prompt[:110]}")
    print("不带任何参考图，直接生成…")

    from app.services.ai.image_gen import download_generated

    client = QwenImageClient(settings)
    try:
        result = await client.edit_image(prompt=prompt, reference_paths=[])
    finally:
        await client.aclose()

    print()
    # 字段名照 dataclass 来（elapsed_ms / estimated_cny），别猜——上一版猜
    # elapsed_seconds 直接崩了，而且崩在下载之前，等于白花一次钱。
    print(f"ok={result.ok}  错误={result.error or '无'}")
    print(f"耗时 {result.elapsed_ms / 1000:.1f}s  预估 ¥{result.estimated_cny:.3f}")
    print(f"服务端返回 {len(result.urls)} 个地址")

    local_paths = result.local_paths
    if result.urls and not local_paths:
        # **必须立刻下载**：结果地址只活 24 小时，不下载就是"付了钱但图不存在"。
        print("正在下载到本地素材库…")
        local_paths, errors = await download_generated(result.urls, settings=settings)
        for error in errors:
            print(f"  下载失败：{error}")

    root = Path(settings.media_root_path).parent
    for path in local_paths:
        full = root / path
        size = full.stat().st_size // 1024 if full.is_file() else -1
        print(f"  ✓ {path}  ({size} KB)")
    if local_paths:
        print()
        print("→ 现有模型支持纯文生图，不需要接新模型。")
    else:
        print("→ 没拿到图。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

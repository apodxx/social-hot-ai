"""试拼图：把一批图拼成网格，看尺寸与体积是否合理。

不调用任何模型，**免费**。用之前从小红书抓到的那 7 张图做样本。

    python scripts/try_stitch.py
    python scripts/try_stitch.py <图片目录>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.image_stitch import grid_shape, stitch_images  # noqa: E402


def main() -> int:
    settings = get_settings()
    root = Path(settings.media_root_path).parent
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(settings.media_root_path) / "from_url"
    files = sorted(p for p in folder.glob("*.jpg"))
    if not files:
        print(f"{folder} 里没有 jpg，先用 scripts/try_xhs_link.py 抓一批")
        return 1

    limit = 7
    picked = [str(p.relative_to(root)).replace("\\", "/") for p in files[:limit]]
    print(f"用 {len(picked)} 张：")
    for path in picked:
        full = root / path
        print(f"  {path}  ({full.stat().st_size // 1024} KB)")

    print()
    print("网格布局（按数量自动选列数，目标接近方形）：")
    for count in (1, 2, 3, 4, 6, 7, 9):
        cols, rows = grid_shape(count)
        print(f"  {count} 张 -> {cols} 列 × {rows} 行")

    print()
    result = stitch_images(picked, settings=settings)
    if not result.ok:
        print(f"拼接失败：{result.error}")
        return 1
    full = root / result.path
    print(f"拼好了：{result.path}")
    print(f"  尺寸 {result.width}×{result.height}  体积 {full.stat().st_size // 1024} KB  含 {result.count} 张")
    print()
    print(f"对比：单张约 900×1200、130KB；7 张分开是 7 条消息，拼起来只占 1 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

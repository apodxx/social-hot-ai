"""把多张图拼成一张（网格），用于 OCR 与发送。

**为什么用网格而不是竖着拼一条：** 视觉模型会按长边缩放输入。7 张图竖排会变成一条
2000×9000 的细长图，缩放后每张图的高度只剩原有的几分之一，**文字糊到读不出来**。
拼成接近方形的网格（如 2 列 × 4 行）能保住每格的有效分辨率。

拼图两个用途：
  * **OCR** —— 一次识别整张，且能确认"到底读了几张"；
  * **发送** —— QQ 被动回复一次只有 3-4 条额度，7 张发不完；拼成 1 张就只占 1 条。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 每格的宽度上限。太大既慢又没必要（OCR 用不到那么宽）。
CELL_WIDTH = 900
#: 整张图的高度上限。视觉模型对超长图会降采样，超过就白拼了。
MAX_TOTAL_HEIGHT = 6000
#: 网格线宽度与颜色，让相邻两格不要糊在一起。
GUTTER = 12
BACKGROUND = (255, 255, 255)


@dataclass
class StitchResult:
    """拼图结果。"""

    path: str = ""
    count: int = 0
    width: int = 0
    height: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.path) and not self.error


def grid_shape(count: int) -> tuple[int, int]:
    """按数量选列数：目标是接近方形，避免细长条。"""
    if count <= 1:
        return 1, 1
    columns = max(1, math.ceil(math.sqrt(count)))
    # 每列最多 3 行，行多了就加列。
    while math.ceil(count / columns) > 3 and columns < 4:
        columns += 1
    rows = math.ceil(count / columns)
    return columns, rows


def stitch_images(
    paths: list[str],
    *,
    settings: object,
    columns: int | None = None,
    cell_width: int = CELL_WIDTH,
) -> StitchResult:
    """把多张本地图片拼成一张网格图，返回**素材库相对路径**。

    只接受本地路径：平台链接会过期，而且拼图本来就要先把图下载下来。
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow 是既有依赖
        return StitchResult(error=f"Pillow 不可用：{exc}")

    if not paths:
        return StitchResult(error="没有可拼接的图片")

    root = Path(getattr(settings, "media_root_path")).parent
    opened: list[Image.Image] = []
    try:
        for item in paths:
            local = Path(item)
            if not local.is_absolute():
                local = root / item
            if not local.is_file():
                continue
            try:
                image = Image.open(local)
                image.load()
                opened.append(image.convert("RGB"))
            except Exception as exc:  # noqa: BLE001 - 单张坏图不该让整批失败
                logger.warning("stitch: skip %s (%s)", local.name, exc)
        if not opened:
            return StitchResult(error="没有一张图能打开")

        count = len(opened)
        cols, rows = (columns, math.ceil(count / columns)) if columns else grid_shape(count)

        # 每格按统一宽度等比缩放；整张太高就整体再缩一次。
        scaled: list[Image.Image] = []
        for image in opened:
            ratio = cell_width / image.width
            scaled.append(image.resize((cell_width, max(1, int(image.height * ratio)))))
        cell_height = max(image.height for image in scaled)
        total_height = rows * cell_height + (rows + 1) * GUTTER
        total_width = cols * cell_width + (cols + 1) * GUTTER

        scale = min(1.0, MAX_TOTAL_HEIGHT / total_height)
        if scale < 1.0:
            cell_width = max(200, int(cell_width * scale))
            cell_height = max(100, int(cell_height * scale))
            scaled = [image.resize((cell_width, cell_height)) for image in scaled]
            logger.info("stitch: downscaled to %.2f to fit height limit", scale)

        canvas = Image.new(
            "RGB",
            (cols * cell_width + (cols + 1) * GUTTER, rows * cell_height + (rows + 1) * GUTTER),
            BACKGROUND,
        )
        for index, image in enumerate(scaled):
            row, column = divmod(index, cols)
            canvas.paste(
                image,
                (
                    GUTTER + column * (cell_width + GUTTER),
                    GUTTER + row * (cell_height + GUTTER),
                ),
            )

        target_dir = Path(getattr(settings, "media_root_path")) / "stitched"
        target_dir.mkdir(parents=True, exist_ok=True)
        # 文件名带上来源图数量与尺寸，重复拼接同一批不会产生多份。
        name = f"grid-{count}-{canvas.width}x{canvas.height}.jpg"
        target = target_dir / name
        canvas.save(target, "JPEG", quality=88, optimize=True)
        relative = str(target.relative_to(root)).replace("\\", "/")
        logger.info(
            "stitched %d images -> %s (%dx%d, %dKB)",
            count,
            relative,
            canvas.width,
            canvas.height,
            target.stat().st_size // 1024,
        )
        return StitchResult(
            path=relative, count=count, width=canvas.width, height=canvas.height
        )
    except Exception as exc:  # noqa: BLE001
        return StitchResult(error=f"{type(exc).__name__}: {exc}")
    finally:
        for image in opened:
            try:
                image.close()
            except Exception:  # noqa: BLE001
                pass

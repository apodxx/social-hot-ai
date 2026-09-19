"""诊断：为什么一张合法的 PNG 被 QQ 判为「不支持的文件格式」(850019)。

只做**上传**（upload_prepare → PUT → part_finish → merge），不发消息，所以不占用
主动/被动消息额度，也不花钱。逐一对比不同来源的图片，定位是"文件本身"还是"上传流程"。

    python scripts/diagnose_qq_upload.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.qq_media import (  # noqa: E402
    CONVERTED_SUBDIR,
    ensure_sendable,
    upload_group_image,
)

HEADERS = {"User-Agent": "SocialHotAI/1.0 (QQBot)"}


def _describe(path: Path) -> str:
    """真实的字节事实：magic bytes、尺寸、位深、色彩类型。"""
    data = path.read_bytes()
    magic = data[:8].hex()
    detail = ""
    try:
        from PIL import Image

        with Image.open(path) as image:
            detail = f"{image.format} {image.mode} {image.size[0]}x{image.size[1]}"
    except Exception as exc:  # noqa: BLE001
        detail = f"PIL 读不了：{exc}"
    return f"{path.name}  {len(data)}B  magic={magic}  {detail}"


async def _token() -> str:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.post(
            "https://bots.qq.com/app/getAppAccessToken",
            json={"appId": settings.qq_app_id, "clientSecret": settings.qq_app_secret},
        )
        return str(response.json()["access_token"])


async def main() -> int:
    settings = get_settings()
    if not settings.qq_group_openid:
        print("QQ_GROUP_OPENID 未配置")
        return 1

    root = settings.media_root_path
    candidates: list[Path] = []

    generated = sorted((root / "generated").glob("*.png"))
    if generated:
        candidates.append(generated[0])
    # 一张下载来的 jpg（如果还没转换过就先转一份）
    jpgs = [p for p in root.rglob("*.jpg") if CONVERTED_SUBDIR not in p.parts]
    if jpgs:
        candidates.append(jpgs[0])
    webps = [p for p in root.rglob("*.webp")][:1]
    for webp in webps:
        candidates.append(ensure_sendable(webp, cache_dir=root / CONVERTED_SUBDIR))
    # 一张程序生成的最小 PNG，作为"绝对正常"的对照
    control = root / CONVERTED_SUBDIR / "control.png"
    control.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        Image.new("RGB", (512, 512), (30, 120, 200)).save(control, "PNG")
        candidates.append(control)
    except Exception as exc:  # noqa: BLE001
        print(f"无法生成对照图：{exc}")

    token = await _token()
    headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}

    print(f"目标群：{settings.qq_group_openid[:8]}…")
    print()
    async with httpx.AsyncClient(
        base_url="https://api.sgroup.qq.com", timeout=120.0, trust_env=False
    ) as client:
        for path in candidates:
            print("─" * 70)
            print(_describe(path))
            result = await upload_group_image(
                client, group_openid=settings.qq_group_openid, path=path, headers=headers
            )
            print(f"  -> ok={result.ok} parts={result.parts_uploaded}")
            if result.error:
                print(f"     error: {result.error}")
            if result.ok:
                print(f"     file_info={result.file_info[:40]}… ttl={result.ttl}")
    print("─" * 70)
    print("上传不消耗消息额度；本次没有发送任何消息。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

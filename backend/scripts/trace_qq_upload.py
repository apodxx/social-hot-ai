"""逐字打印 QQ 富媒体分片上传的每一步，定位 850019 到底出在哪。

只上传，不发消息。把 ``upload_prepare`` / ``PUT`` / ``upload_part_finish`` / ``files``
的原始状态码与响应体全部打出来，并额外对照：

* ``file_type=4``（文件）——若它能成功而图片不能，说明是图片权限而不是流程；
* URL 上传（传一个公网可访问的图片）——若它能成功而分片不能，说明是分片流程。

    python scripts/trace_qq_upload.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.notification.qq_media import content_type_for, digest_file  # noqa: E402

BASE = "https://api.sgroup.qq.com"
HEADERS = {"User-Agent": "SocialHotAI/1.0 (QQBot)"}


def _show(label: str, response: httpx.Response) -> None:
    body = response.text[:400]
    print(f"  {label}: HTTP {response.status_code} {body}")


async def _token() -> str:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.post(
            "https://bots.qq.com/app/getAppAccessToken",
            json={"appId": settings.qq_app_id, "clientSecret": settings.qq_app_secret},
        )
        return str(response.json()["access_token"])


async def trace_chunked(client: httpx.AsyncClient, headers: dict, group: str, path: Path, file_type: int) -> None:
    print(f"--- 分片上传 file_type={file_type}：{path.name} ---")
    digests = digest_file(path)
    prepare = await client.post(
        f"/v2/groups/{group}/upload_prepare",
        json={
            "file_type": file_type,
            "file_size": str(digests.size),
            "file_name": path.name,
            "md5": digests.md5,
            "sha1": digests.sha1,
            "md5_10m": digests.md5_10m,
        },
        headers=headers,
    )
    _show("upload_prepare", prepare)
    if prepare.status_code >= 400:
        return
    plan = prepare.json()
    print(f"    upload_id={plan.get('upload_id')} block_size={plan.get('block_size')} "
          f"parts={len(plan.get('parts') or [])} config={plan.get('upload_config')}")
    upload_id = plan.get("upload_id")
    data = path.read_bytes()

    # 与 qq_media.upload_group_image 相同的偏移量算法：**不要用 index * block_size**，
    # 实测返回的是 1 基序号，那样第一片会从文件末尾开始、PUT 出 0 字节。
    ordered = sorted(plan.get("parts") or [], key=lambda item: int(item.get("index") or 0))
    offset = 0
    for position, part in enumerate(ordered):
        remaining = len(data) - offset
        size = int(part.get("block_size") or 0) or remaining
        chunk = data[offset : offset + size]
        offset += len(chunk)
        index = int(part.get("index") or position)
        put = await client.put(
            part["presigned_url"], content=chunk,
            headers={"Content-Type": content_type_for(path)},
        )
        _show(f"PUT part{index} ({len(chunk)}B)", put)
        finish = await client.post(
            f"/v2/groups/{group}/upload_part_finish",
            json={
                "upload_id": upload_id,
                "part_index": index,
                "block_size": str(len(chunk)),
                "md5": hashlib.md5(chunk).hexdigest(),
            },
            headers=headers,
        )
        _show(f"upload_part_finish part{index}", finish)

    for label, body in (
        ("merge(file_type)", {"file_type": file_type, "srv_send_msg": False,
                              "file_name": path.name, "upload_id": upload_id}),
        ("merge(no file_type)", {"srv_send_msg": False, "file_name": path.name,
                                 "upload_id": upload_id}),
    ):
        merge = await client.post(f"/v2/groups/{group}/files", json=body, headers=headers)
        _show(label, merge)


async def trace_url(client: httpx.AsyncClient, headers: dict, group: str, url: str) -> None:
    print(f"--- URL 上传：{url[:60]} ---")
    response = await client.post(
        f"/v2/groups/{group}/files",
        json={"file_type": 1, "url": url, "srv_send_msg": False},
        headers=headers,
    )
    _show("url upload", response)


async def main() -> int:
    settings = get_settings()
    group = settings.qq_group_openid
    token = await _token()
    headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}

    control = settings.media_root_path / "converted" / "control.png"
    if not control.is_file():
        from PIL import Image

        control.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (512, 512), (30, 120, 200)).save(control, "PNG")

    print(f"目标群 {group[:8]}…  对照图 {control.name} ({control.stat().st_size}B)")
    print()
    async with httpx.AsyncClient(base_url=BASE, timeout=120.0, trust_env=False) as client:
        await trace_chunked(client, headers, group, control, 1)
        print()
        await trace_chunked(client, headers, group, control, 4)
        print()
        # 一个公开可访问的小 PNG，用来判断是"分片流程"还是"图片权限"
        await trace_url(
            client, headers, group,
            "https://www.python.org/static/img/python-logo.png",
        )
    print()
    print("未发送任何消息。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

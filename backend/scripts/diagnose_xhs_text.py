"""看小红书笔记详情里**正文与标题**挂在哪个字段（现在只取了图片，没取文字）。"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.tikhub.note_link import NOTE_DETAIL_ENDPOINT, resolve_note_link  # noqa: E402

URL = "https://www.xiaohongshu.com/explore/68c15f20000000001d02ad18"


def walk(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


async def main() -> int:
    settings = get_settings()
    link = await resolve_note_link(URL)
    print(f"note_id={link.note_id}  token={'有' if link.xsec_token else '无'}  err={link.error or '无'}")
    if not link.ok:
        return 1

    params = {"note_id": link.note_id}
    if link.xsec_token:
        params["xsec_token"] = link.xsec_token
    async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
        response = await client.get(
            settings.tikhub_base_url.rstrip("/") + NOTE_DETAIL_ENDPOINT,
            params=params,
            headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
        )
    payload = response.json()

    # 找"像正文"的长字符串（含中文、长度 > 20）
    print()
    print("像正文的长文本（路径 → 前 80 字）：")
    for path, value in walk(payload):
        if len(value) < 20:
            continue
        if not re.search(r"[\u4e00-\u9fa5]", value):
            continue
        if value.startswith("http"):
            continue
        short = re.sub(r"\[\d+\]", "[]", path)
        print(f"  {short}")
        print(f"     {value[:80]}")

    # 含 title / desc / content 的字段
    print()
    print("含 title / desc / content 的字段：")
    seen: set[str] = set()
    for path, value in walk(payload):
        lowered = path.lower()
        if any(h in lowered for h in ("title", "desc", "content", "tag")):
            field = re.sub(r"\[\d+\]", "[]", path)
            if field not in seen:
                seen.add(field)
                print(f"  {field} = {value[:60]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

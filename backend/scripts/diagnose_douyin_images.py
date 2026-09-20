"""诊断：抖音图文笔记的**真正内容图**在返回的哪个字段里。

背景：实测抓到的图是作者头像（同一张动漫图重复 5 次），而不是笔记里的笔记本推荐卡片。
说明我的通用遍历抓错了地方。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

URL = "https://www.iesdouyin.com/share/note/7608902395444099953/"


def walk_strings(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk_strings(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


async def main() -> int:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
        response = await client.get(
            settings.tikhub_base_url.rstrip("/") + "/api/v1/douyin/app/v3/fetch_one_video_by_share_url",
            params={"share_url": URL},
            headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
        )
    payload = response.json()

    # 1) 所有像图片的地址，按"路径字段名"归组 —— 看它们在哪个字段下。
    groups: dict[str, list[str]] = {}
    for path, value in walk_strings(payload):
        if not value.startswith("http"):
            continue
        path_only = value.split("?", 1)[0]
        if not path_only.lower().endswith((".webp", ".jpg", ".jpeg", ".png")):
            continue
        field = re.sub(r"\[\d+\]", "[]", path)
        groups.setdefault(field, []).append(value)

    print("图片地址分布在哪些字段下（字段 → 数量）：")
    for field, urls in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:14]:
        # 同一张图的变体用 ~ 前的 ID 归并，看真实张数
        ids = {u.split("?", 1)[0].split("~", 1)[0] for u in urls}
        print(f"  {len(urls):3} 个地址 / {len(ids):2} 张不同图   {field}")

    # 2) 找 image_list / images 这类明确的数组字段
    print()
    print("含 image / pic 的字段路径（去重后）：")
    seen: set[str] = set()
    for path, _value in walk_strings(payload):
        lowered = path.lower()
        if any(h in lowered for h in ("image", "pic", "cover", "avatar")):
            field = re.sub(r"\[\d+\]", "[]", path)
            if field not in seen:
                seen.add(field)
                print(f"  {field}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""诊断：一条小红书笔记里到底有多少张图，以及我的收集器漏了什么。

重点看：
  * TikHub 返回里**所有** http 地址按域名分组（图片未必都在 xhscdn 上）；
  * 有没有 ``image_list`` / ``images`` 这类明显的数组字段被我漏掉。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.tikhub.note_link import (  # noqa: E402
    collect_image_urls,
    fetch_note_payload,
)

URL = sys.argv[1] if len(sys.argv) > 1 else "https://xhslink.cn/o/8GVbJQYwbGs"


def all_strings(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from all_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from all_strings(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


async def main() -> int:
    settings = get_settings()
    payload, error = await fetch_note_payload(URL, settings=settings)
    if error:
        print(f"取详情失败：{error}")
        return 1
    print(f"返回总长 {len(json.dumps(payload, ensure_ascii=False))}")

    # 1) 所有 http 地址按域名分组——图片未必只在 xhscdn。
    hosts: Counter[str] = Counter()
    http_urls: list[tuple[str, str]] = []
    for path, value in all_strings(payload):
        if value.startswith("http"):
            match = re.match(r"https?://([^/]+)", value)
            hosts[match.group(1) if match else "?"] += 1
            http_urls.append((path, value))
    print()
    print("所有 http 地址按域名：")
    for host, count in hosts.most_common(12):
        print(f"  {count:3}  {host}")

    # 2) 我的收集器拿到几张。
    mine = collect_image_urls(payload, limit=30)
    print()
    print(f"collect_image_urls 拿到 {len(mine)} 张：")
    for one in mine:
        print(f"  {one[:110]}")

    # 3) 看有没有明显的图片数组字段。
    print()
    print("路径里含 image / pic / cover / gallery 的字段（看结构，不看值）：")
    seen: set[str] = set()
    for path, _value in all_strings(payload):
        lowered = path.lower()
        if any(hint in lowered for hint in ("image", "pic", "cover", "gallery")):
            field = re.sub(r"\[\d+\]", "[]", path)
            if field not in seen:
                seen.add(field)
                print(f"  {field}")
    # 4) 疑似图片但域名不是 xhscdn 的（被我的过滤器漏掉的）。
    print()
    print("疑似图片但域名不是 xhscdn（可能被漏掉）：")
    leaked = 0
    for path, value in http_urls:
        if "xhscdn" in value:
            continue
        lowered = value.lower()
        if any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp", "image")):
            leaked += 1
            print(f"  {path} -> {value[:100]}")
    if not leaked:
        print("  （没有）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

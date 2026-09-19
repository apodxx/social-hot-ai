"""Turn the three captured search responses into the exact extraction paths.

Run after ``scripts/discover_search.py``. It reads the saved fixtures only — no network,
no cost — and prints, per platform, the field paths an adapter must use, including
where the images and videos actually live.

    python scripts/describe_search_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "raw"


def load(name: str) -> dict[str, Any] | None:
    path = FIXTURE_DIR / name
    if not path.exists():
        print(f"{name}: MISSING (run discover_search.py --yes)")
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def dig(node: Any, path: str, default: Any = None) -> Any:
    """Walk a dotted path with list indices, e.g. ``a.b[0].c``."""
    current = node
    for part in path.replace("]", "").split("."):
        if "[" in part:
            name, index = part.split("[")
            if name:
                if not isinstance(current, dict):
                    return default
                current = current.get(name)
            if not isinstance(current, list):
                return default
            position = int(index)
            if position >= len(current):
                return default
            current = current[position]
        else:
            if not isinstance(current, dict):
                return default
            current = current.get(part)
        if current is None:
            return default
    return current


def report_xiaohongshu(payload: dict[str, Any]) -> None:
    items = dig(payload, "data.data.items", []) or []
    print(f"  items: {len(items)}  (path data.data.items)")
    kinds: dict[str, int] = {}
    with_images = 0
    image_counts: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        note = item.get("note") if isinstance(item.get("note"), dict) else {}
        kind = str(note.get("type") or item.get("model_type") or "?")
        kinds[kind] = kinds.get(kind, 0) + 1
        images = note.get("image_list")
        if isinstance(images, list) and images:
            with_images += 1
            image_counts.append(len(images))
    print(f"  note.type values: {kinds}")
    print(f"  items carrying note.image_list: {with_images}/{len(items)}")
    if image_counts:
        print(f"  images per note: min={min(image_counts)} max={max(image_counts)}")
    first = items[0] if items else {}
    note = first.get("note") if isinstance(first.get("note"), dict) else {}
    print("  field paths on items[0].note:")
    for path in (
        "id",
        "type",
        "title",
        "desc",
        "cover_image_index",
        "image_list[0].url_default",
        "image_list[0].info_list[0].url",
        "image_list[0].width",
        "image_list[0].height",
        "video.media.stream.h264[0].master_url",
        "user.nickname",
        "user.user_id",
        "interact_info.liked_count",
        "interact_info.collected_count",
        "interact_info.comment_count",
        "tag_list[0].name",
    ):
        value = dig(note, path)
        if value is not None:
            shown = value if not isinstance(value, str) else value[:70]
            print(f"    note.{path} = {json.dumps(shown, ensure_ascii=True)}")
    print("    item-level: note_id =", json.dumps(str(dig(first, 'id'))[:40]))
    print("    xsec_token present:", bool(dig(first, "xsec_token")))


def report_douyin(payload: dict[str, Any]) -> None:
    entries = dig(payload, "data.business_data", []) or []
    print(f"  entries: {len(entries)}  (path data.business_data)")
    shapes: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        keys = ",".join(sorted(inner.keys()))[:60]
        shapes[keys] = shapes.get(keys, 0) + 1
    print("  entry.data key shapes:")
    for shape, count in shapes.items():
        print(f"    {count:>3}x  {shape}")

    awemes = [e for e in entries if isinstance(e, dict) and isinstance(e.get("data"), dict) and "aweme_info" in e["data"]]
    print(f"  entries with data.aweme_info: {len(awemes)}")
    if awemes:
        info = awemes[0]["data"]["aweme_info"]
        print("  field paths on aweme_info:")
        for path in (
            "aweme_id",
            "aweme_type",
            "desc",
            "create_time",
            "author.nickname",
            "author.uid",
            "statistics.digg_count",
            "statistics.comment_count",
            "statistics.share_count",
            "video.cover.url_list[0]",
            "video.play_addr.url_list[0]",
            "video.duration",
            "images[0].url_list[0]",
            "image_post_info.images[0].display_image.url_list[0]",
            "share_url",
        ):
            value = dig(info, path)
            if value is not None:
                shown = value if not isinstance(value, str) else value[:70]
                print(f"    aweme_info.{path} = {json.dumps(shown, ensure_ascii=True)}")
        kinds: dict[str, int] = {}
        for entry in awemes:
            info = entry["data"]["aweme_info"]
            kind = str(info.get("aweme_type"))
            kinds[kind] = kinds.get(kind, 0) + 1
        print(f"  aweme_type values (0=video, 68/other=image post): {kinds}")
        with_images = sum(1 for e in awemes if dig(e["data"]["aweme_info"], "images"))
        print(f"  awemes carrying a top-level images[]: {with_images}/{len(awemes)}")


def report_weibo(payload: dict[str, Any]) -> None:
    pics = dig(payload, "data.pic_list", []) or []
    print(f"  entries: {len(pics)}  (path data.pic_list)  total_number={dig(payload, 'data.total_number')}")
    if not pics:
        return
    entry = pics[0]
    print("  field paths on pic_list[0]:")
    for path in (
        "mid",
        "text",
        "original_pic",
        "url",
        "created_at",
        "is_forward",
        "sub_name",
        "sub_text",
        "user.name",
        "user.id",
    ):
        value = dig(entry, path)
        if value is not None:
            shown = value if not isinstance(value, str) else value[:80]
            print(f"    pic_list[0].{path} = {json.dumps(shown, ensure_ascii=True)}")
    with_pic = sum(1 for p in pics if isinstance(p, dict) and p.get("original_pic"))
    print(f"  entries with original_pic (the image URL): {with_pic}/{len(pics)}")


def main() -> int:
    print("=== xiaohongshu search_notes (GET, keyword)")
    payload = load("search_xiaohongshu.json")
    if payload:
        report_xiaohongshu(payload)

    print("\n=== douyin fetch_general_search_v2 (POST, json body)")
    payload = load("search_douyin.json")
    if payload:
        report_douyin(payload)

    print("\n=== weibo fetch_pic_search (GET, query)")
    payload = load("search_weibo.json")
    if payload:
        report_weibo(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

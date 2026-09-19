"""Capture one real **keyword search** response per platform (Phase 9, question 1).

Why this exists: the user asked to search topics they care about, and to have images
and video present. The current collection path cannot do either — it reads fixed
ranking/feed endpoints. The search endpoints are the way in, and TikHub publishes no
response schemas, so the field names cannot be derived from documentation.

**This spends money**: one billed call per platform. It refuses to run without
``--yes``. Fixtures are saved so the adapters can be written and tested offline.

The three endpoints differ in ways that decide the implementation:

* Xiaohongshu ``app_v2/search_notes`` — GET, ``keyword``; has a ``note_type`` filter
  (不限 / 视频笔记 / 普通笔记) which is exactly the image-vs-video split.
* Douyin ``search/fetch_general_search_v2`` — **POST with a JSON body**; has a
  ``content_type`` filter (0=all 1=video 2=image 3=article). V3 has no such filter,
  which is why V2 is used here.
* Weibo ``web_v2/fetch_pic_search`` — GET, ``query``; the weibo search endpoints we
  already call return only a ``has_image`` boolean and no image URLs, so picture
  search is probed for a real image source.

Usage::

    python scripts/discover_search.py --yes
    python scripts/discover_search.py --yes --keyword 美食 --platform douyin
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.logger import setup_logging  # noqa: E402
from app.db.database import dispose_engine, session_scope  # noqa: E402
from app.db.repository import recent_rows  # noqa: E402
from app.services.tikhub.client import TikHubClient, TikHubError  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "raw"

XIAOHONGSHU_PATH = "/api/v1/xiaohongshu/app_v2/search_notes"
DOUYIN_PATH = "/api/v1/douyin/search/fetch_general_search_v2"
WEIBO_PIC_PATH = "/api/v1/weibo/web_v2/fetch_pic_search"

#: Field-name fragments that decide whether an adapter can find media at all.
MEDIA_TOKENS = ("image", "pic", "cover", "video", "thumb", "url", "type", "title", "desc")


def walk(node: Any, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    """Every leaf with its path, depth-limited."""
    if depth > 6:
        return []
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                found.extend(walk(value, path, depth + 1))
            else:
                found.append((path, value))
    elif isinstance(node, list):
        for index, value in enumerate(node[:2]):
            if isinstance(value, (dict, list)):
                found.extend(walk(value, f"{prefix}[{index}]", depth + 1))
            else:
                found.append((f"{prefix}[{index}]", value))
    return found


def describe(payload: dict[str, Any]) -> None:
    """Print what an extractor needs: where the items are and what they carry."""
    print(f"  top-level keys: {sorted(payload.keys())}")
    data = payload.get("data")
    if isinstance(data, dict):
        print(f"  data keys     : {sorted(data.keys())}")
        for key, value in data.items():
            if isinstance(value, list) and value:
                print(f"  candidate list: data.{key} ({len(value)} entries)")
    elif isinstance(data, list):
        print(f"  data          : list of {len(data)}")

    leaves = walk(payload)
    print(f"  leaf fields   : {len(leaves)}")
    counts: dict[str, int] = {}
    for path, _value in leaves:
        counts[path] = counts.get(path, 0) + 1
    interesting = [
        (path, count)
        for path, count in counts.items()
        if any(token in path.lower() for token in MEDIA_TOKENS)
    ]
    print("  media/text fields (this is what decides extraction):")
    for path, count in sorted(interesting, key=lambda item: (-item[1], item[0]))[:26]:
        print(f"    {count:>4}x  {path}")

    # A couple of real values, so the shape is unambiguous.
    print("  sample values:")
    for path, _count in sorted(interesting, key=lambda item: -item[1])[:8]:
        sample = next((value for p, value in leaves if p == path), None)
        if isinstance(sample, str):
            print(f"    {path} = {json.dumps(sample[:90], ensure_ascii=True)}")
        elif sample is not None:
            print(f"    {path} = {sample!r}")


async def capture(platform: str, keyword: str, client: TikHubClient) -> None:
    """Fetch one search response and save it verbatim."""
    print(f"\n=== {platform}: keyword={json.dumps(keyword, ensure_ascii=False)}")
    try:
        if platform == "xiaohongshu":
            payload = await client.get_json(
                XIAOHONGSHU_PATH,
                {"keyword": keyword, "page": 1, "sort_type": "general", "note_type": "不限"},
            )
        elif platform == "douyin":
            payload = await client.post_json(
                DOUYIN_PATH,
                {"keyword": keyword, "cursor": 0, "sort_type": "0", "publish_time": "0", "content_type": "0"},
            )
        elif platform == "weibo":
            payload = await client.get_json(WEIBO_PIC_PATH, {"query": keyword, "page": 1})
        else:  # pragma: no cover - argparse restricts this
            print(f"    unknown platform {platform}")
            return
    except TikHubError as exc:
        print(f"    FAILED: {exc}")
        return

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    target = FIXTURE_DIR / f"search_{platform}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    saved -> {target.name} ({target.stat().st_size} bytes)")
    describe(payload)


async def default_keyword() -> str:
    """A real stored hot word, so the search returns something representative."""
    async with session_scope() as session:
        rows = await recent_rows(session, hours=72, limit=200)
    for row in rows:
        if row.platform == "weibo" and row.title and len(row.title) <= 12:
            return row.title
    return "美食"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm the billed calls")
    parser.add_argument("--keyword", default=None, help="search keyword (default: a stored hot word)")
    parser.add_argument(
        "--platform",
        choices=["xiaohongshu", "douyin", "weibo"],
        action="append",
        help="limit to one or more platforms (default: all three)",
    )
    args = parser.parse_args()

    platforms = args.platform or ["xiaohongshu", "douyin", "weibo"]
    if not args.yes:
        print(
            "Refusing to run: this makes "
            f"{len(platforms)} billed TikHub search call(s). Re-run with --yes."
        )
        return 2

    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.tikhub_configured:
        print("TIKHUB_API_KEY is not set.")
        return 1

    keyword = args.keyword or await default_keyword()
    print(f"keyword: {json.dumps(keyword, ensure_ascii=False)}")
    print(f"platforms: {platforms} ({len(platforms)} billed call(s))")

    async with TikHubClient(settings) as client:
        for platform in platforms:
            await capture(platform, keyword, client)
    await dispose_engine()
    print("\nDone. Write the search adapters against these fixtures; no further calls needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

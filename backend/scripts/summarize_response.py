"""Summarise a captured ``/api/hot?include_raw=true`` response, offline.

Turns one paid run into a reusable report: per-platform counts, how many items
carry each field (which answers "which fields can we actually use"), and the
provider key inventory behind them. Costs nothing — it only reads a saved file.

Usage::

    python scripts/summarize_response.py tests/fixtures/raw/api_hot_response.json
    python scripts/summarize_response.py tests/fixtures/raw/api_hot_response.json --extract
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path
from typing import Any

# Chinese titles crash a GBK console otherwise.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

FIELDS = (
    "title",
    "description",
    "author",
    "author_id",
    "url",
    "publish_time",
    "rank",
    "hot_value",
    "likes",
    "comments",
    "shares",
    "collects",
    "content_type",
    "cover_url",
    "video_url",
)


def collect_keys(node: Any, into: collections.Counter, prefix: str = "") -> None:
    """Count every key path that appears anywhere in one raw item."""
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            into[path] += 1
            collect_keys(value, into, path)
    elif isinstance(node, list):
        for value in node[:2]:  # two samples per list is enough for a key census
            collect_keys(value, into, f"{prefix}[]")


def extract_fixtures(payload: dict[str, Any], directory: Path) -> None:
    """Write each platform's ``raw_data`` back out as a fixture file.

    The fixture is shaped like a provider payload so the adapter's
    :func:`~app.services.tikhub.base.find_item_list` finds the items, which lets
    the normalisers be validated against genuine data without another call.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for platform, items in (payload.get("data") or {}).items():
        raw_items = [item.get("raw_data") for item in items if item.get("raw_data")]
        if not raw_items:
            print(f"  {platform}: no raw_data captured, skipping fixture")
            continue
        target = directory / f"{platform}.json"
        target.write_text(
            json.dumps({"code": 200, "data": {"items": raw_items}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  {platform}: wrote {len(raw_items)} raw items -> {target.name}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) != 1:
        print(__doc__)
        return 2
    payload_path = Path(args[0])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    if "--extract" in flags:
        print("extracting fixtures from raw_data:")
        extract_fixtures(payload, payload_path.parent)

    print(f"success={payload.get('success')} counts={payload.get('counts')}")
    print(f"limit_per_platform={payload.get('limit_per_platform')} fetched_at={payload.get('fetched_at')}")
    if payload.get("errors"):
        print(f"errors={payload['errors']}")

    for platform, items in (payload.get("data") or {}).items():
        print(f"\n=== {platform}: {len(items)} items")
        if not items:
            print("  (no items — nothing to analyse; the raw payload was never returned)")
            continue
        for field in FIELDS:
            present = sum(1 for item in items if item.get(field) not in (None, "", [], {}))
            print(f"  {field:<14} {present:>3}/{len(items)}")

        key_counter: collections.Counter = collections.Counter()
        for item in items[:20]:
            collect_keys(item.get("raw_data") or {}, key_counter)
        top = ", ".join(f"{key}({count})" for key, count in key_counter.most_common(25))
        print(f"  raw key paths: {top}")

        print("  first 3 items:")
        for item in items[:3]:
            print(
                "    - rank={rank} hot={hot} title={title!r} type={ctype} author={author!r}".format(
                    rank=item.get("rank"),
                    hot=item.get("hot_value"),
                    title=(item.get("title") or "")[:40],
                    ctype=item.get("content_type"),
                    author=item.get("author"),
                )
            )
        print(f"  raw_data of item 1: {json.dumps((items[0].get('raw_data') or {}), ensure_ascii=False)[:600]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Capture one real detail response per platform (spec section 十六).

Section 十六 is the step between selection and rewriting, and its three endpoints
have three different meanings:

* Xiaohongshu — a real note detail; we hold both ``note_id`` and ``xsec_token``.
* Douyin — ``index/fetch_hot_detail`` takes a *topic name*, so it returns topic
  index detail rather than a post body.
* Weibo — a hot word has no post id, so the only route is a realtime **search**
  for the keyword, which yields posts to use as source material.

**This script spends money**: one billed call per platform. It refuses to run
without ``--yes``, and it saves the raw response so the extractors can be written
and verified offline afterwards.

Usage::

    python scripts/discover_detail.py --yes
    python scripts/discover_detail.py --yes --platform weibo
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
from app.services.tikhub.details import build_detail_request  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "raw"

#: Longest string field worth printing as a candidate body.
SAMPLE_CHARS = 120


def walk_strings(node: Any, path: str = "", depth: int = 0) -> list[tuple[str, str]]:
    """Every string in the response with its path, ordered by discovery."""
    if depth > 6:
        return []
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(walk_strings(value, f"{path}.{key}" if path else str(key), depth + 1))
    elif isinstance(node, list):
        for index, value in enumerate(node[:3]):
            found.extend(walk_strings(value, f"{path}[{index}]", depth + 1))
    elif isinstance(node, str) and node.strip():
        found.append((path, node))
    return found


def describe(payload: dict[str, Any]) -> None:
    """Print the structure facts needed to write an extractor."""
    print(f"  top-level keys: {sorted(payload.keys())}")
    data = payload.get("data")
    if isinstance(data, dict):
        print(f"  data keys     : {sorted(data.keys())}")
    elif isinstance(data, list):
        print(f"  data          : list of {len(data)}")
    strings = walk_strings(payload)
    print(f"  string fields : {len(strings)}")
    # Long strings first: a body is long, a status code is not.
    for path, value in sorted(strings, key=lambda item: -len(item[1]))[:12]:
        sample = value.replace("\n", "⏎")[:SAMPLE_CHARS]
        print(f"    {len(value):>6}  {path} = {json.dumps(sample, ensure_ascii=True)}")


async def capture(platform: str, client: TikHubClient) -> None:
    """Fetch one detail response for a real stored item and save it."""
    async with session_scope() as session:
        rows = await recent_rows(session, hours=48, limit=400)
    candidates = [row for row in rows if row.platform == platform]
    if not candidates:
        print(f"\n=== {platform}: no stored item to use as a subject, skipped")
        return
    # Prefer a grouped/analysed item: that is what the pipeline would fetch.
    subject = max(
        candidates,
        key=lambda row: (row.topic_group_id is not None, row.hot_value or 0),
    )

    request = build_detail_request(subject)
    if request is None:
        print(f"\n=== {platform}: no detail route for this item, skipped")
        return
    path, params = request
    print(f"\n=== {platform}: GET {path}")
    print(f"    subject: id={subject.id} title={json.dumps(subject.title[:40], ensure_ascii=True)}")
    print(f"    params : { {k: (v[:24] + '…' if isinstance(v, str) and len(v) > 24 else v) for k, v in params.items()} }")
    try:
        payload = await client.get_json(path, params)
    except TikHubError as exc:
        print(f"    FAILED: {exc}")
        return
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    target = FIXTURE_DIR / f"detail_{platform}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    saved -> {target.name} ({target.stat().st_size} bytes)")
    describe(payload)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm the billed calls")
    parser.add_argument(
        "--platform",
        choices=["xiaohongshu", "douyin", "weibo"],
        action="append",
        help="limit to one or more platforms (default: all)",
    )
    args = parser.parse_args()

    if not args.yes:
        print("Refusing to run: this makes billed TikHub calls. Re-run with --yes.")
        return 2

    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.tikhub_configured:
        print("TIKHUB_API_KEY is not set.")
        return 1

    platforms = args.platform or ["xiaohongshu", "douyin", "weibo"]
    async with TikHubClient(settings) as client:
        for platform in platforms:
            await capture(platform, client)
    await dispose_engine()
    print("\nDone. Write the extractors against these fixtures; no further calls needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

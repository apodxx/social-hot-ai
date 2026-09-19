"""Capture one real response per platform into ``tests/fixtures/raw/``.

Why this exists: TikHub publishes no response schema or example for any
endpoint (every 200 is a generic ``ResponseModel``), so the platform payload
shapes cannot be read off the documentation. Rather than invent field names,
the adapters are written against candidate paths and then narrowed using one
captured response per platform. Everything afterwards — normalisers, tests,
``GET /api/hot`` — is verified against those fixtures with **no further billed
calls**.

**This script spends money.** Each platform costs one billed endpoint call
(xiaohongshu may need a second, because its ``num`` is capped at 40), so it
refuses to run without ``--yes``.

Usage::

    python scripts/discover_raw.py --yes
    python scripts/discover_raw.py --yes --platform weibo
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
from app.services.tikhub import ADAPTER_CLASSES  # noqa: E402
from app.services.tikhub.client import TikHubClient, TikHubError  # noqa: E402
from app.services.tikhub.base import find_item_list  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "raw"

#: The request each adapter makes, kept next to the script for transparency.
REQUESTS: dict[str, tuple[str, dict[str, Any]]] = {
    "xiaohongshu": ("/api/v1/xiaohongshu/web_v3/fetch_homefeed",
                    {"num": 40, "category": "homefeed_recommend"}),
    "weibo": ("/api/v1/weibo/web_v2/fetch_hot_search_summary", {}),
    "douyin": ("/api/v1/douyin/app/v3/fetch_hot_search_list", {"board_type": "0"}),
}


def describe(payload: dict[str, Any]) -> None:
    """Print the structure facts needed to finish the normalisers."""
    print(f"  top-level keys : {sorted(payload.keys())}")
    data = payload.get("data")
    if isinstance(data, dict):
        print(f"  data keys      : {sorted(data.keys())}")
    items = find_item_list(payload)
    print(f"  items detected : {len(items)}")
    if items:
        print(f"  first item keys: {sorted(items[0].keys())}")
        preview = json.dumps(items[0], ensure_ascii=False, indent=2)
        print("  first item:")
        for line in preview.splitlines()[:40]:
            print(f"    {line}")
        if len(preview.splitlines()) > 40:
            print("    ... (truncated; see the saved fixture)")


async def capture(platform: str, client: TikHubClient) -> None:
    """Call one endpoint, save the raw body, and describe its shape."""
    path, params = REQUESTS[platform]
    print(f"\n=== {platform}: GET {path} params={params or '{}'}")
    try:
        payload = await client.get_json(path, params)
    except TikHubError as exc:
        print(f"  FAILED: {exc}")
        return
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    target = FIXTURE_DIR / f"{platform}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  saved -> {target.relative_to(FIXTURE_DIR.parents[2])} ({target.stat().st_size} bytes)")
    describe(payload)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm the billed calls")
    parser.add_argument(
        "--platform",
        choices=[cls.platform.value for cls in ADAPTER_CLASSES],
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
        print("TIKHUB_API_KEY is not set (see .env.example).")
        return 1

    platforms = args.platform or [cls.platform.value for cls in ADAPTER_CLASSES]
    async with TikHubClient(settings) as client:
        for platform in platforms:
            await capture(platform, client)
    print("\nDone. Normalisers and tests now run against these fixtures offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

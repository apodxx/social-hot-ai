"""Run a keyword search from the command line (Phase 9).

**This spends money**: one billed TikHub search per platform. It refuses to run without
``--yes`` and prints the cost first.

    python scripts/run_search.py --yes --keyword 露营装备
    python scripts/run_search.py --yes --keyword 露营装备 --platforms xiaohongshu
    python scripts/run_search.py --yes --keyword 露营装备 --limit 10 --no-download

Same code path as ``POST /api/hot/search`` and the MCP ``search_topic`` tool: it calls
``collect_by_keyword`` directly, so a result here means the API and MCP surfaces behave
identically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.core.logger import setup_logging  # noqa: E402
from app.db.database import dispose_engine, session_scope  # noqa: E402
from app.db.repository import list_admin_hot_contents  # noqa: E402
from app.services.media.store import storage_summary  # noqa: E402
from app.services.pipeline.search_pipeline import collect_by_keyword  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm the billed calls")
    parser.add_argument("--keyword", required=False, help="what to search for")
    parser.add_argument(
        "--platforms",
        default=None,
        help="comma-separated subset (default: SEARCH_PLATFORMS, i.e. all three)",
    )
    parser.add_argument("--limit", type=int, default=10, help="items kept per platform")
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="skip the image download step (links only; they will expire)",
    )
    args = parser.parse_args()

    if not args.yes:
        print("Refusing to run: this makes billed TikHub search calls. Re-run with --yes.")
        return 2
    if not args.keyword:
        print("--keyword is required (an empty search would still be billed).")
        return 2

    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.tikhub_configured:
        print("TIKHUB_API_KEY is not set.")
        return 1

    platforms = (
        [part.strip() for part in args.platforms.split(",") if part.strip()]
        if args.platforms
        else settings.search_platform_list
    )
    download = not args.no_download and settings.media_download_enabled

    print(f"keyword      : {json.dumps(args.keyword, ensure_ascii=False)}")
    print(f"platforms    : {platforms}")
    print(f"billed calls : {len(platforms)} (one search per platform)")
    print(f"per platform : {args.limit} items kept (does not change the cost)")
    print(f"download     : {'yes -> ' + str(settings.media_root_path) if download else 'no'}")
    print()

    result = await collect_by_keyword(
        args.keyword,
        platforms=platforms,
        limit=args.limit,
        download_media=download,
        settings=settings,
    )

    print("--- run report ---")
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))

    # Read back what is now stored, so this is evidence rather than a claim. Filtered
    # by the keyword the row was collected under, not by title text.
    async with session_scope(settings) as session:
        rows, total = await list_admin_hot_contents(
            session, source_keyword=args.keyword, limit=200
        )

    print("\n--- stored for this keyword ---")
    print(f"rows matching the keyword: {total}")
    for item, _analysis, _rewrite in rows:
        media = item.media or {}
        images = media.get("images") or []
        downloaded = sum(1 for image in images if image.get("local_path"))
        print(
            f"  [{item.platform:<11}] {item.content_type:<6} images={len(images)}"
            f" downloaded={downloaded} {item.title[:38]!r}"
        )

    summary = storage_summary(settings)
    megabytes = summary["bytes"] / 1024 / 1024
    print(
        f"\nmaterial library: {summary['files']} files, {megabytes:.1f} MB at {summary['root']}"
    )

    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

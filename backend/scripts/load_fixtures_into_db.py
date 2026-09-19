"""Load the captured fixtures into the database — Phase 2 without spending money.

The fixtures in ``tests/fixtures/raw/`` are genuine TikHub responses, so running
them through the real normalisers, the real four-layer dedup and the real
repository exercises the entire Phase 2 path at **zero cost**. That matters here
because the alternative (re-running ``/api/hot``) is billed per call.

Usage::

    python scripts/load_fixtures_into_db.py            # all captured platforms
    python scripts/load_fixtures_into_db.py --reset    # drop and recreate tables
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
from app.db.database import create_all, dispose_engine, drop_all  # noqa: E402
from app.models.hot_content import HotContent  # noqa: E402
from app.services.pipeline.hot_pipeline import store_items  # noqa: E402
from app.services.tikhub.base import find_item_list  # noqa: E402
from app.services.tikhub.client import TikHubClient  # noqa: E402
from app.services.tikhub.douyin import DouyinAdapter  # noqa: E402
from app.services.tikhub.weibo import WeiboAdapter  # noqa: E402
from app.services.tikhub.xiaohongshu import XiaohongshuAdapter  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "raw"

ADAPTERS = {
    "weibo": WeiboAdapter,
    "douyin": DouyinAdapter,
    "xiaohongshu": XiaohongshuAdapter,
}


def load_platform(platform: str, client: TikHubClient) -> list[HotContent]:
    """Normalise one captured fixture exactly as the live path would."""
    path = FIXTURE_DIR / f"{platform}.json"
    if not path.exists():
        print(f"  {platform}: no fixture, skipped")
        return []
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    items = find_item_list(payload)
    adapter = ADAPTERS[platform](client)
    normalized = adapter.normalize(items)
    print(f"  {platform}: {len(items)} raw -> {len(normalized)} normalised")
    return normalized


def inject_demo_twins(items: list[HotContent]) -> list[HotContent]:
    """Add SYNTHETIC cross-platform twins of two real titles.

    Purpose: the captured snapshot happens to contain no genuine cross-platform
    overlap (the three hot lists covered different events), so without this the
    ``topic_groups`` table would be legitimately empty and layer 4 would be
    invisible. The injected rows reuse **real titles** and are marked
    ``raw_data.demo = True`` so they can never be mistaken for provider data.
    """
    from app.models.hot_content import ContentType, Platform, HotContent as Model

    twins: list[Model] = []
    sources = [
        ("weibo", "douyin"),
        ("xiaohongshu", "weibo"),
    ]
    for source_platform, twin_platform in sources:
        source = next((item for item in items if item.platform.value == source_platform), None)
        if source is None:
            continue
        twins.append(
            Model(
                id=Model.make_id(Platform(twin_platform), f"demo-{source.platform_content_id}"),
                platform=Platform(twin_platform),
                platform_content_id=f"demo-{source.platform_content_id}",
                title=source.title,
                description="",
                hot_value=1,
                rank=1,
                content_type=ContentType.TOPIC,
                raw_data={
                    "demo": True,
                    "note": "synthetic twin of a real title; not provider data",
                    "source_platform": source.platform.value,
                    "source_content_id": source.platform_content_id,
                },
            )
        )
    if twins:
        print(
            f"\n  !! injected {len(twins)} SYNTHETIC demo twins "
            f"(marked raw_data.demo=true) to exercise layer 4"
        )
    return twins


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop and recreate the tables")
    parser.add_argument("--platform", action="append", help="limit to one or more platforms")
    parser.add_argument(
        "--demo-cross-platform",
        action="store_true",
        help=(
            "also inject SYNTHETIC twins of real titles on another platform, so layer 4 "
            "(cross-platform aggregation) has something to aggregate. Off by default: "
            "these rows are not provider data."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)
    print(f"database: {settings.database_url}")

    if args.reset:
        print("dropping all tables")
        await drop_all(settings)
    await create_all(settings)

    platforms = args.platform or list(ADAPTERS)
    client = TikHubClient(settings)  # never used for network I/O in this script
    collected: list[HotContent] = []
    for platform in platforms:
        collected.extend(load_platform(platform, client))

    if args.demo_cross_platform:
        collected.extend(inject_demo_twins(collected))

    print(f"\nstoring {len(collected)} items through the real pipeline")
    result = await store_items(collected, settings=settings)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))

    from app.db.database import session_scope
    from app.db.repository import database_stats, list_topic_groups

    async with session_scope(settings) as session:
        stats = await database_stats(session)
        print("\ndatabase stats:", json.dumps(stats, ensure_ascii=False))
        groups, total = await list_topic_groups(session, limit=10)
        print(f"\ntopic groups: {total} total, showing {len(groups)}")
        for group, members in groups:
            print(f"  [{group.id}] {group.topic[:38]!r} platforms={group.platforms} members={members}")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

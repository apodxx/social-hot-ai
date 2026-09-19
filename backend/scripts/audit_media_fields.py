"""What do we actually store today, and what is in the provider payloads?

Answers three questions with evidence rather than assumption:

1. Are ``cover_url`` / ``video_url`` / ``content_type`` populated, per platform?
2. Do the raw payloads contain image lists that the adapter simply does not read yet?
3. What *kind* of thing is a weibo/douyin "hot search" entry — a post, or a keyword?
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.database import session_scope  # noqa: E402
from app.models.hot_content import HotContentRecord  # noqa: E402


async def main() -> int:
    settings = get_settings()
    async with session_scope(settings) as session:
        rows = (await session.execute(select(HotContentRecord))).scalars().all()

    print(f"total rows: {len(rows)}\n")
    print(f"{'platform':<14} {'rows':>5} {'cover':>6} {'video':>6}  content_type")
    for platform in ("weibo", "douyin", "xiaohongshu"):
        subset = [row for row in rows if row.platform == platform]
        if not subset:
            continue
        cover = sum(1 for row in subset if row.cover_url)
        video = sum(1 for row in subset if row.video_url)
        kinds = Counter(row.content_type for row in subset)
        print(
            f"{platform:<14} {len(subset):>5} {cover:>6} {video:>6}  {dict(kinds)}"
        )

    # What does a stored description look like? Is it a body, or just a headline?
    print("\nsample titles and whether a body was fetched (detail stage):")
    for platform in ("weibo", "douyin", "xiaohongshu"):
        subset = [row for row in rows if row.platform == platform]
        if not subset:
            continue
        with_body = sum(1 for row in subset if len(row.description or "") > 50)
        sample = subset[0]
        print(
            f"  {platform:<14} titles={sample.title[:26]!r} "
            f"desc_len={len(sample.description or '')} rows_with_body>50chars={with_body}"
        )

    # --- what image/video fields exist in the raw payloads? -------------------
    print("\nimage/video-ish keys present in raw_data (this is what we could read):")
    for platform in ("xiaohongshu", "weibo", "douyin"):
        subset = [row for row in rows if row.platform == platform]
        if not subset:
            continue
        keys: Counter[str] = Counter()
        for row in subset[:400]:
            for key in _flatten_keys(row.raw_data or {}):
                if any(
                    token in key.lower()
                    for token in ("image", "cover", "video", "pic", "thumb", "url_list")
                ):
                    keys[key] += 1
        top = keys.most_common(12)
        print(f"  {platform}: {'no such keys' if not top else ''}")
        for key, count in top:
            print(f"      {key}  (in {count}/{min(len(subset), 400)} rows)")

    return 0


def _flatten_keys(payload: object, prefix: str = "") -> list[str]:
    """Every key path in a nested structure, e.g. ``note_card.image_list``."""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.append(path)
            if isinstance(value, (dict, list)):
                found.extend(_flatten_keys(value, path))
    elif isinstance(payload, list):
        for entry in payload[:2]:
            if isinstance(entry, (dict, list)):
                found.extend(_flatten_keys(entry, f"{prefix}[]"))
    return found


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

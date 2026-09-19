"""Answer "why is there no body / no analysis?" with numbers instead of guesses.

Prints, per platform and origin:

* how many stored rows carry a body (``description``) and how long those bodies are;
* how many have an AI analysis at all, and whether the analysed ones have empty fields;
* where the downloaded images live and how much space they take.

    python scripts/diagnose_content.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.database import session_scope  # noqa: E402
from app.models.ai_analysis import AiAnalysisRecord  # noqa: E402
from app.models.hot_content import HotContentRecord  # noqa: E402
from app.services.media.store import storage_summary  # noqa: E402


async def main() -> int:
    settings = get_settings()
    async with session_scope(settings) as session:
        rows = (await session.execute(select(HotContentRecord))).scalars().all()
        analyses = (await session.execute(select(AiAnalysisRecord))).scalars().all()

    analysed_ids = {a.hot_content_id for a in analyses}
    by_platform_origin: dict[tuple[str, str], list[HotContentRecord]] = defaultdict(list)
    for row in rows:
        by_platform_origin[(row.platform, row.origin)].append(row)

    print(f"stored rows: {len(rows)}   analyses: {len(analyses)}\n")
    print(f"{'platform':<12} {'origin':<7} {'rows':>5} {'with body':>10} {'avg body':>9} "
          f"{'>=100 chars':>12} {'analysed':>9} {'with images':>12}")
    for (platform, origin), subset in sorted(by_platform_origin.items()):
        bodies = [len(r.description or "") for r in subset]
        with_body = sum(1 for length in bodies if length > 0)
        substantial = sum(1 for length in bodies if length >= 100)
        analysed = sum(1 for r in subset if r.id in analysed_ids)
        images = sum(1 for r in subset if (r.image_count or 0) > 0)
        average = int(sum(bodies) / len(bodies)) if bodies else 0
        print(f"{platform:<12} {origin:<7} {len(subset):>5} {with_body:>10} {average:>9} "
              f"{substantial:>12} {analysed:>9} {images:>12}")

    # --- what the analysed rows actually contain -----------------------------
    print("\nanalysis fields on the rows that DO have one:")
    empty_topic = sum(1 for a in analyses if not (a.topic or "").strip())
    empty_summary = sum(1 for a in analyses if not (a.summary or "").strip())
    print(f"  analyses with an empty topic  : {empty_topic}/{len(analyses)}")
    print(f"  analyses with an empty summary: {empty_summary}/{len(analyses)}")
    selected = sum(1 for a in analyses if a.selected)
    recommended = sum(1 for a in analyses if a.recommended)
    print(f"  recommended: {recommended}, selected for rewriting: {selected}")

    # --- where the images are -------------------------------------------------
    summary = storage_summary(settings)
    megabytes = summary["bytes"] / 1024 / 1024
    print("\nmedia library:")
    print(f"  root : {summary['root']}")
    print(f"  files: {summary['files']}   size: {megabytes:.1f} MB   exists: {summary['exists']}")

    variants: dict[str, int] = defaultdict(int)
    root = Path(str(summary["root"]))
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and path.suffix != ".part":
                variants[path.suffix.lower()] += 1
        print(f"  by extension: {dict(sorted(variants.items(), key=lambda kv: -kv[1]))}")
        example = next((p for p in root.rglob("*") if p.is_file()), None)
        if example:
            print(f"  example path: {example}")
            print(f"  served at   : /media/{example.relative_to(root).as_posix()}")

    rows_with_local = sum(
        1
        for row in rows
        for image in ((row.media or {}).get("images") or [])
        if isinstance(image, dict) and image.get("local_path")
    )
    print(f"  images recorded on rows with a local file: {rows_with_local}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

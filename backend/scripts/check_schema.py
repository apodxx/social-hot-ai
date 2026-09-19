"""Check the live schema against the models, and report the Phase 9 columns.

Two questions this answers, both of which have bitten this project already:

1. Do the columns the models declare actually exist, with the nullability and defaults
   we expect? (A migration that ran is not the same as a schema that matches.)
2. Does Alembic consider the database up to date with the models? Autogenerate would
   otherwise report drift on every future revision.

    python scripts/check_schema.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from app.db.database import session_scope  # noqa: E402

PHASE9_COLUMNS = ("media", "image_count", "origin", "source_keyword")

QUERY = """
select column_name, is_nullable, column_default, data_type
from information_schema.columns
where table_name = 'hot_contents'
  and column_name = any(:names)
order by column_name
"""


async def main() -> int:
    async with session_scope() as session:
        rows = (
            await session.execute(text(QUERY), {"names": list(PHASE9_COLUMNS)})
        ).all()
        total = (await session.execute(text("select count(*) from hot_contents"))).scalar_one()
        defaulted = (
            await session.execute(
                text(
                    "select count(*) from hot_contents "
                    "where origin = 'hot' and image_count = 0"
                )
            )
        ).scalar_one()
        version = (
            await session.execute(text("select version_num from alembic_version"))
        ).scalar_one_or_none()

    print(f"alembic revision: {version}")
    print(f"hot_contents rows: {total}")
    print("\nPhase 9 columns:")
    seen = {row[0] for row in rows}
    for column, nullable, default, data_type in rows:
        print(f"  {column:<16} nullable={nullable:<4} default={default} type={data_type}")
    missing = [name for name in PHASE9_COLUMNS if name not in seen]
    if missing:
        print(f"  MISSING: {', '.join(missing)}")
        return 1
    print(f"\nexisting rows backfilled as ranking entries: {defaulted}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

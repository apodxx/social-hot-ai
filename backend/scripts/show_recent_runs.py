"""查看定时管线最近几次运行的结果（读了什么、推没推成功）。只读。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402


async def main() -> int:
    from sqlalchemy import select

    from app.db.database import session_scope
    from app.models.task import PipelineTaskRecord

    settings = get_settings()
    async with session_scope(settings) as session:
        rows = (
            await session.execute(
                select(PipelineTaskRecord).order_by(PipelineTaskRecord.id.desc()).limit(6)
            )
        ).scalars().all()

    if not rows:
        print("还没有任何管线运行记录")
        return 0

    print(f"最近 {len(rows)} 次运行：")
    for record in rows:
        created = record.created_at.astimezone() if record.created_at else None
        stamp = created.strftime("%m-%d %H:%M:%S") if created else "-"
        print(f"  [{stamp}] {record.task_type or '?'}  trigger={getattr(record, 'trigger', '?')}  "
              f"status={record.status}")
        steps = getattr(record, "steps", None) or getattr(record, "detail", None)
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                name = step.get("name") or step.get("stage") or "?"
                status = step.get("status")
                detail = step.get("detail") or {}
                extra = ""
                if name == "notify":
                    extra = (f"enabled={detail.get('enabled')} ok={detail.get('ok')} "
                             f"sent={detail.get('messages_sent')} "
                             f"err={str(detail.get('error') or '')[:50]}")
                elif name in ("fetch", "watch"):
                    extra = f"items={detail.get('items') or detail.get('stored') or ''}"
                elif name == "rewrite":
                    extra = f"rewritten={detail.get('rewritten') or detail.get('rewritten_ids')}"
                print(f"      {name:8} {status:8} {extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Record a standalone billed operation as a pipeline task (Phase 10).

Why this exists: ``POST /api/hot/search`` and ``POST /api/hot/watch`` spend real money
but created **no task row**, so those calls were invisible on the 任务记录 page — the one
place an operator looks to answer "what has this thing been spending?". A run that cost
ten billed calls could only be inferred from the balance.

``run_pipeline`` records its own tasks inline; this is the same discipline extracted for
one-shot operations, with the same guarantees:

* the row is created before the work starts, so a crash leaves evidence;
* it is always closed (``finally``), so a failure cannot leave a task stuck in
  ``running`` and block the next pipeline run;
* the work's own report becomes both the step detail and the task summary.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.db.repository import create_task, finish_task
from app.models.task import TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class RecordedRun:
    """The work's report plus where it was recorded."""

    task_id: int | None = None
    status: str = TaskStatus.SUCCESS.value
    duration_ms: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error or None,
        }


async def run_as_task(
    *,
    task_type: str,
    step_name: str,
    work: Callable[[], Awaitable[dict[str, Any]]],
    settings: Settings | None = None,
    billed: bool = True,
) -> RecordedRun:
    """Run ``work`` and record it as a task. Re-raises whatever ``work`` raised.

    The exception is re-raised on purpose: the API must still answer with the failure,
    while the task row keeps a permanent record of it.
    """
    resolved = settings or get_settings()
    run = RecordedRun()
    started = time.perf_counter()

    async with session_scope(resolved) as session:
        task = await create_task(session, task_type)
        run.task_id = task.id

    try:
        result = await work()
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        run.status = TaskStatus.FAILED.value
        run.error = f"{type(exc).__name__}: {exc}"
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        await _close(resolved, run, step_name=step_name, billed=billed, detail={})
        raise

    run.duration_ms = int((time.perf_counter() - started) * 1000)
    run.result = result
    # A partial failure (one platform of a search, say) is reported by the work itself.
    if result.get("errors"):
        run.status = TaskStatus.PARTIAL.value
    await _close(resolved, run, step_name=step_name, billed=billed, detail=result)
    return run


async def _close(
    settings: Settings,
    run: RecordedRun,
    *,
    step_name: str,
    billed: bool,
    detail: dict[str, Any],
) -> None:
    """Write the closing row, never raising: the work already happened."""
    try:
        async with session_scope(settings) as session:
            from app.models.task import PipelineTaskRecord

            task = await session.get(PipelineTaskRecord, run.task_id)
            if task is None:  # pragma: no cover - deleted mid-run
                logger.warning("task %s vanished before it could be closed", run.task_id)
                return
            await finish_task(
                session,
                task,
                status=TaskStatus(run.status),
                steps=[
                    {
                        "name": step_name,
                        "status": run.status,
                        "billed": billed,
                        "duration_ms": run.duration_ms,
                        "detail": detail,
                        "error": run.error or None,
                    }
                ],
                summary=detail,
                error_message=run.error or None,
            )
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not mask the real result
        logger.error("could not record task %s: %s", run.task_id, exc)


__all__ = ["RecordedRun", "run_as_task"]

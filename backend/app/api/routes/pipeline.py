"""``/api/pipeline`` — Phase 5: manual execution and task history.

``POST /api/pipeline/run`` is the spec's section 二十三 development trigger. It
**can spend money**: ``fetch`` costs TikHub calls and ``analyze``/``rewrite`` cost
DeepSeek tokens, so the endpoint documents that and lets the caller pick stages.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.config import get_settings
from app.db.database import session_scope
from app.db.repository import list_tasks
from app.models.task import PipelineTaskRecord, TaskStatus
from app.services.pipeline.runner import (
    ALL_STAGES,
    BILLED_STAGES,
    describe_schedule,
    resolve_stages,
    run_pipeline,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _task_to_dict(task: PipelineTaskRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "duration_ms": task.duration_ms,
        "error_message": task.error_message,
        "steps": task.steps or [],
        "summary": task.summary or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


@router.post(
    "/pipeline/run",
    summary="Run the pipeline now (development trigger)",
    description=(
        "Runs the stages in order and records a task row. **Billed stages:** "
        f"`{'`, `'.join(sorted(BILLED_STAGES))}` — `fetch` costs TikHub calls, "
        "`analyze`/`rewrite` cost DeepSeek tokens. `detail` and `notify` are "
        "reported as skipped because they are not implemented yet. One stage "
        "failing marks the task `partial` and keeps the work already done. A run "
        "in progress blocks a second one."
    ),
)
async def run_pipeline_endpoint(
    stages: str | None = Query(
        default=None,
        description=f"Comma-separated subset of: {', '.join(ALL_STAGES)} (default: all)",
    ),
    trigger: str = Query(default="manual", description="Recorded as the task type."),
) -> dict[str, Any]:
    """Execute the requested stages and return the run report."""
    settings = get_settings()
    if not settings.tikhub_configured and settings.deepseek_configured is False:
        raise HTTPException(
            status_code=503,
            detail="Neither TIKHUB_API_KEY nor DEEPSEEK_API_KEY is configured; nothing can run.",
        )
    try:
        selected = resolve_stages(stages)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    billed = sorted(set(selected) & BILLED_STAGES)
    if billed:
        logger.warning(
            "POST /api/pipeline/run stages=%s — billed stages selected: %s",
            selected,
            billed,
        )

    result = await run_pipeline(trigger=trigger, stages=selected, settings=settings)
    return {
        "success": result.status != TaskStatus.FAILED.value,
        "billed_stages_selected": billed,
        "run": result.as_dict(),
    }


@router.get("/pipeline/tasks", summary="Task history (free)")
async def get_tasks(
    status: str | None = Query(
        default=None, description=f"Filter by status: {[item.value for item in TaskStatus]}"
    ),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Recorded pipeline runs, newest first."""
    if status and status not in {item.value for item in TaskStatus}:
        raise HTTPException(
            status_code=422,
            detail=f"unknown status {status!r}; expected {[item.value for item in TaskStatus]}",
        )
    settings = get_settings()
    async with session_scope(settings) as session:
        rows, total = await list_tasks(session, status=status, limit=limit, offset=offset)
    return {
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_task_to_dict(task) for task in rows],
    }


@router.get("/pipeline/schedule", summary="Configured schedule and registered jobs (free)")
async def get_schedule(request: Request) -> dict[str, Any]:
    """What the scheduler will run, including next fire times when it is live."""
    settings = get_settings()
    scheduler = getattr(request.app.state, "scheduler", None)
    payload = describe_schedule(settings)
    payload["running"] = bool(scheduler and scheduler.running)
    payload["jobs"] = scheduler.jobs() if scheduler is not None else []
    return {"success": True, "schedule": payload}

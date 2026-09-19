"""The whole pipeline, one stage at a time (Phase 5, spec sections 廿二/廿三).

Stage order follows the spec's flow diagram::

    fetch -> analyze -> detail -> rewrite -> notify

All five stages are implemented as of Phase 6. Each receives the accumulated
summary of the stages already run, so a later stage can act on what an earlier one
produced (``notify`` uses the rewrite stage's content ids).

Design decisions worth stating:

* **One short session per stage, not one long transaction.** The spec requires
  that a step failing must not destroy the work already done, so each stage
  commits its own results; a later failure marks the task ``partial``.
* **The task row is the log.** Every stage records status, duration, whether it
  bills, and its own numbers, so "which step failed and what did it cost" is
  answerable from the database alone.
* **A running task blocks new runs**, with a staleness timeout, so a crash cannot
  either double-run the pipeline or silently freeze the scheduler forever.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.db.repository import (
    create_task,
    fail_stale_tasks,
    finish_task,
    has_running_task,
)
from app.models.task import TaskStatus

logger = logging.getLogger(__name__)

#: Every stage, in execution order.
#:
#: ``watch`` sits between ``fetch`` and ``analyze`` because it *supplies candidates*:
#: the boards rarely contain technology content, so the watched keywords are searched
#: before anything is analysed or rewritten.
ALL_STAGES: tuple[str, ...] = ("fetch", "watch", "analyze", "detail", "rewrite", "notify")

#: Stages that cost money. The manual endpoint and the docs surface this.
BILLED_STAGES: frozenset[str] = frozenset({"fetch", "watch", "detail", "analyze", "rewrite"})

#: Stages that exist but are not implemented yet, with the reason shown to callers.
#: Empty since Phase 6; the mechanism stays so a future stage can be declared
#: honestly instead of silently doing nothing.
UNIMPLEMENTED_STAGES: dict[str, str] = {}


@dataclass
class StepReport:
    """What one stage did."""

    name: str
    status: str  # success | failed | skipped
    billed: bool = False
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "billed": self.billed,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class PipelineRun:
    """The outcome of one pipeline execution."""

    task_id: int | None = None
    task_type: str = "manual"
    status: str = TaskStatus.SUCCESS.value
    steps: list[StepReport] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "steps": [step.as_dict() for step in self.steps],
            "summary": self.summary,
        }


def resolve_stages(stages: str | Sequence[str] | None) -> list[str]:
    """Normalise and validate a stage selection."""
    if stages is None or stages == "":
        return list(ALL_STAGES)
    if isinstance(stages, str):
        requested = [part.strip().lower() for part in stages.split(",") if part.strip()]
    else:
        requested = [str(part).strip().lower() for part in stages if str(part).strip()]
    unknown = [name for name in requested if name not in ALL_STAGES]
    if unknown:
        raise ValueError(
            f"unknown stage(s): {', '.join(unknown)}; known stages: {', '.join(ALL_STAGES)}"
        )
    ordered = [name for name in ALL_STAGES if name in requested]
    return ordered


async def _run_fetch(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    """Fetch from TikHub, deduplicate, store, aggregate topics."""
    from app.services.pipeline.hot_pipeline import collect_and_store

    result = await collect_and_store(settings.hot_limit_per_platform, settings=settings)
    return result.as_dict()


async def _run_watch(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    """Search the configured domain keywords (billed per keyword per platform)."""
    from app.services.pipeline.watch_stage import run_watch_stage

    return (await run_watch_stage(settings)).as_dict()


async def _run_analyze(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    """Rule filter, DeepSeek analysis, selection, topic summaries."""
    from app.services.ai.analyzer import run_analysis

    return (await run_analysis(settings=settings)).as_dict()


async def _run_detail(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    """Fetch the body behind each selected item (billed per item, opt-in)."""
    from app.services.pipeline.detail_stage import run_detail_stage

    return (await run_detail_stage(settings)).as_dict()


async def _run_rewrite(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the selected topics into three platform versions."""
    from app.services.ai.rewriter import run_rewriting

    return (await run_rewriting(settings=settings)).as_dict()


async def _run_notify(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    """Send the review digest (§25).

    Uses the rewrite stage's own list of ids so the notification covers what *this*
    run produced; a manual ``notify`` run without that context falls back to the
    most recent rewrites, which the manager logs.
    """
    from app.services.notification.manager import NotificationManager

    manager = NotificationManager(settings)
    rewrite_report = context.get("rewrite") or {}
    content_ids = rewrite_report.get("rewritten_ids") or []
    outcome = await manager.notify_rewrites(content_ids=content_ids)
    return outcome.as_dict()


#: Stages receive the accumulated summary of the stages already run.
STAGE_RUNNERS: dict[str, Callable[[Settings, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "fetch": _run_fetch,
    "watch": _run_watch,
    "analyze": _run_analyze,
    "detail": _run_detail,
    "rewrite": _run_rewrite,
    "notify": _run_notify,
}


async def run_pipeline(
    *,
    trigger: str = "manual",
    stages: str | Sequence[str] | None = None,
    settings: Settings | None = None,
) -> PipelineRun:
    """Run the pipeline stages in order, recording a task row.

    **Some stages spend money** — see :data:`BILLED_STAGES`. Every stage failure is
    captured and the remaining stages are skipped; nothing raises out of here, so
    the scheduler can never be taken down by a bad run.
    """
    resolved = settings or get_settings()
    try:
        selected_stages = resolve_stages(stages)
    except ValueError as exc:
        return PipelineRun(
            task_type=trigger, status=TaskStatus.SKIPPED.value, error=str(exc)
        )

    run = PipelineRun(task_type=trigger)

    # --- claim the run ------------------------------------------------------
    async with session_scope(resolved) as session:
        await fail_stale_tasks(session, stale_minutes=resolved.task_stale_minutes)
        running = await has_running_task(session)
        if running is not None:
            reason = (
                f"task {running.id} ({running.task_type}) is already running since "
                f"{running.started_at}; refusing to run twice"
            )
            logger.warning("pipeline skipped: %s", reason)
            run.status = TaskStatus.SKIPPED.value
            run.skipped_reason = reason
            return run
        task = await create_task(session, trigger)
        run.task_id = task.id

    # --- run the stages ----------------------------------------------------
    failed = False
    for name in selected_stages:
        if failed:
            run.steps.append(
                StepReport(
                    name=name,
                    status="skipped",
                    billed=name in BILLED_STAGES,
                    error="an earlier stage failed",
                )
            )
            continue

        if name in UNIMPLEMENTED_STAGES:
            run.steps.append(
                StepReport(
                    name=name,
                    status="skipped",
                    billed=name in BILLED_STAGES,
                    detail={"reason": UNIMPLEMENTED_STAGES[name]},
                )
            )
            continue

        runner = STAGE_RUNNERS.get(name)
        if runner is None:  # pragma: no cover - resolve_stages guards this
            run.steps.append(StepReport(name=name, status="skipped", error="no runner"))
            continue

        logger.info("pipeline stage %s starting (task %s)", name, run.task_id)
        started = time.perf_counter()
        try:
            detail = await runner(resolved, run.summary)
        except Exception as exc:  # noqa: BLE001 - a bad stage must not crash the run
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.error("pipeline stage %s failed: %s", name, exc)
            run.steps.append(
                StepReport(
                    name=name,
                    status="failed",
                    billed=name in BILLED_STAGES,
                    duration_ms=duration_ms,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            run.error = f"{name}: {type(exc).__name__}: {exc}"
            failed = True
            continue

        run.steps.append(
            StepReport(
                name=name,
                status="success",
                billed=name in BILLED_STAGES,
                duration_ms=int((time.perf_counter() - started) * 1000),
                detail=detail,
            )
        )
        run.summary[name] = detail

    # --- close the task ----------------------------------------------------
    executed = [step for step in run.steps if step.status == "success"]
    if failed:
        run.status = (
            TaskStatus.PARTIAL.value if executed else TaskStatus.FAILED.value
        )
    elif not executed:
        run.status = TaskStatus.SKIPPED.value
        run.skipped_reason = "no implemented stage was selected"
    else:
        run.status = TaskStatus.SUCCESS.value

    async with session_scope(resolved) as session:
        from app.models.task import PipelineTaskRecord

        task = await session.get(PipelineTaskRecord, run.task_id)
        if task is not None:
            await finish_task(
                session,
                task,
                status=TaskStatus(run.status),
                steps=[step.as_dict() for step in run.steps],
                summary=run.summary,
                error_message=run.error,
            )

    logger.info(
        "pipeline run %s: %s (%s)",
        run.task_id,
        run.status,
        {step.name: step.status for step in run.steps},
    )
    return run


def describe_schedule(settings: Settings) -> dict[str, Any]:
    """What the scheduler will do, for the status endpoint and the README."""
    return {
        "enabled": settings.scheduler_enabled,
        "times": [f"{hour:02d}:{minute:02d}" for hour, minute in settings.hot_fetch_time_pairs],
        "timezone": settings.scheduler_timezone,
        "stages": list(ALL_STAGES),
        "billed_stages": sorted(BILLED_STAGES),
        "unimplemented_stages": sorted(UNIMPLEMENTED_STAGES),
        # The watch stage's cost is keywords x platforms, so a caller can see the
        # recurring charge rather than discovering it in a bill.
        "watch_billed_calls_per_run": settings.watch_billed_calls_per_run,
        "watch_keywords": settings.watch_keyword_list,
        "watch_platforms": settings.watch_platform_list,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

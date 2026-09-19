"""Pipeline runner and scheduler tests (Phase 5).

Every stage is replaced by a fake, so the whole orchestration — task rows, step
reports, failure isolation, duplicate prevention — is verified without spending
anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.scheduler import PipelineScheduler
from app.db.database import session_scope
from app.db.repository import create_task, list_tasks
from app.models.task import PipelineTaskRecord, TaskStatus
from app.services.pipeline import runner as runner_module
from app.services.pipeline.runner import (
    ALL_STAGES,
    BILLED_STAGES,
    describe_schedule,
    resolve_stages,
    run_pipeline,
)


# --------------------------------------------------------------- stage list
def test_resolve_stages_defaults_to_every_stage():
    assert resolve_stages(None) == list(ALL_STAGES)
    assert resolve_stages("") == list(ALL_STAGES)


def test_resolve_stages_keeps_execution_order():
    assert resolve_stages("rewrite,fetch") == ["fetch", "rewrite"]
    assert resolve_stages(["notify", "analyze"]) == ["analyze", "notify"]


def test_resolve_stages_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stages("fetch,nonsense")


def test_billed_stages_are_flagged():
    assert {"fetch", "watch", "analyze", "rewrite"} <= BILLED_STAGES
    assert "notify" not in BILLED_STAGES


def test_describe_schedule_reports_times_and_billing(settings):
    described = describe_schedule(settings)
    assert described["times"] == ["08:00", "12:00", "18:00"]
    assert described["stages"] == list(ALL_STAGES)
    # ``watch`` is billed: it searches the configured domain keywords, one call per
    # keyword per platform. It is listed here so a caller can see the cost before
    # selecting stages.
    assert described["billed_stages"] == ["analyze", "detail", "fetch", "rewrite", "watch"]
    # Every stage is implemented as of Phase 6; the mechanism stays for future ones.
    assert described["unimplemented_stages"] == []
    assert described["watch_billed_calls_per_run"] == settings.watch_billed_calls_per_run


# ----------------------------------------------------------------- runner
@pytest.mark.asyncio
async def test_run_pipeline_records_a_successful_task(sqlite_db, settings, monkeypatch):
    async def ok(_settings, _context):
        return {"ok": True}

    for name in ALL_STAGES:
        monkeypatch.setitem(runner_module.STAGE_RUNNERS, name, ok)

    result = await run_pipeline(trigger="manual", settings=settings)
    assert result.status == TaskStatus.SUCCESS.value
    assert result.task_id is not None
    assert [step.name for step in result.steps] == list(ALL_STAGES)
    assert all(step.status == "success" for step in result.steps)
    assert next(step for step in result.steps if step.name == "fetch").billed is True
    assert next(step for step in result.steps if step.name == "detail").billed is True
    assert next(step for step in result.steps if step.name == "notify").billed is False

    async with session_scope(settings) as session:
        rows, total = await list_tasks(session)
        assert total == 1
        task = rows[0]
        assert task.status == TaskStatus.SUCCESS.value
        assert task.task_type == "manual"
        assert task.duration_ms is not None and task.duration_ms >= 0
        assert len(task.steps) == len(ALL_STAGES)
        assert task.summary["fetch"] == {"ok": True}


@pytest.mark.asyncio
async def test_a_stage_receives_the_summary_of_earlier_stages(sqlite_db, settings, monkeypatch):
    seen: dict[str, dict] = {}

    async def first(_settings, _context):
        return {"stage": "first"}

    async def second(_settings, context):
        seen.update(context)
        return {"stage": "second"}

    monkeypatch.setitem(runner_module.STAGE_RUNNERS, "fetch", first)
    monkeypatch.setitem(runner_module.STAGE_RUNNERS, "analyze", second)

    await run_pipeline(stages="fetch,analyze", settings=settings)
    assert seen == {"fetch": {"stage": "first"}}, "a later stage sees what earlier ones produced"


@pytest.mark.asyncio
async def test_a_failing_stage_keeps_earlier_work_and_is_recorded(
    sqlite_db, settings, monkeypatch
):
    order: list[str] = []

    async def ok(_settings, _context):
        order.append("ok")
        return {"ok": True}

    async def boom(_settings, _context):
        order.append("boom")
        raise RuntimeError("analyze exploded")

    monkeypatch.setitem(runner_module.STAGE_RUNNERS, "fetch", ok)
    monkeypatch.setitem(runner_module.STAGE_RUNNERS, "analyze", boom)

    result = await run_pipeline(trigger="scheduled", settings=settings)

    assert order == ["ok", "boom"], "stages run in order and stop at the failure"
    assert result.status == TaskStatus.PARTIAL.value, "successful work is kept"
    assert result.error and "analyze exploded" in result.error
    statuses = {step.name: step.status for step in result.steps}
    assert statuses["fetch"] == "success"
    assert statuses["analyze"] == "failed"
    assert statuses["rewrite"] == "skipped"
    rewrite_step = next(step for step in result.steps if step.name == "rewrite")
    assert rewrite_step.error == "an earlier stage failed"

    async with session_scope(settings) as session:
        rows, _total = await list_tasks(session)
        assert rows[0].status == TaskStatus.PARTIAL.value
        assert "analyze exploded" in (rows[0].error_message or "")


@pytest.mark.asyncio
async def test_first_stage_failing_marks_the_task_failed(sqlite_db, settings, monkeypatch):
    async def boom(_settings, _context):
        raise RuntimeError("tikhub down")

    monkeypatch.setitem(runner_module.STAGE_RUNNERS, "fetch", boom)
    result = await run_pipeline(settings=settings)
    assert result.status == TaskStatus.FAILED.value
    async with session_scope(settings) as session:
        rows, _total = await list_tasks(session)
        assert rows[0].status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_a_declared_unimplemented_stage_is_reported_as_skipped(sqlite_db, settings, monkeypatch):
    """The mechanism stays for future stages; today nothing uses it."""
    monkeypatch.setitem(
        runner_module.UNIMPLEMENTED_STAGES, "notify", "pretend this is not built yet"
    )
    result = await run_pipeline(stages="notify", settings=settings)
    step = result.steps[0]
    assert step.status == "skipped"
    assert step.detail["reason"] == "pretend this is not built yet"
    assert result.status == TaskStatus.SKIPPED.value


@pytest.mark.asyncio
async def test_the_real_notify_stage_runs_and_reports_being_disabled(sqlite_db, settings):
    """Running `notify` for real, with notifications off: the stage succeeds and says why."""
    result = await run_pipeline(stages="notify", settings=settings)
    step = result.steps[0]
    assert step.status == "success"
    assert step.detail["enabled"] is False
    assert "NOTIFICATION_ENABLED" in (step.detail["error"] or "")


@pytest.mark.asyncio
async def test_the_real_detail_stage_runs_and_reports_being_disabled(sqlite_db, settings):
    result = await run_pipeline(stages="detail", settings=settings)
    step = result.steps[0]
    assert step.status == "success"
    assert step.detail["billed_calls"] == 0
    assert any("DETAIL_FETCH_ENABLED" in error for error in step.detail["errors"])


@pytest.mark.asyncio
async def test_a_running_task_blocks_a_second_run(sqlite_db, settings):
    async with session_scope(settings) as session:
        existing = await create_task(session, "scheduled")

    result = await run_pipeline(trigger="manual", settings=settings)
    assert result.status == TaskStatus.SKIPPED.value
    assert result.skipped_reason and f"task {existing.id}" in result.skipped_reason

    async with session_scope(settings) as session:
        _rows, total = await list_tasks(session)
        assert total == 1, "the blocked run must not create a second task row"


@pytest.mark.asyncio
async def test_a_stale_running_task_is_failed_and_does_not_block(sqlite_db, settings, monkeypatch):
    async with session_scope(settings) as session:
        stale = await create_task(session, "scheduled")
        stale.started_at = datetime.now(timezone.utc) - timedelta(hours=3)

    async def ok(_settings, _context):
        return {"ok": True}

    monkeypatch.setitem(runner_module.STAGE_RUNNERS, "fetch", ok)

    result = await run_pipeline(stages="fetch", settings=settings)
    assert result.status == TaskStatus.SUCCESS.value, "a crashed run must not freeze the pipeline"

    async with session_scope(settings) as session:
        rows, total = await list_tasks(session)
        assert total == 2
        by_id = {row.id: row for row in rows}
        assert by_id[stale.id].status == TaskStatus.FAILED.value
        assert "stale" in (by_id[stale.id].error_message or "")


@pytest.mark.asyncio
async def test_unknown_stage_returns_a_skipped_run_without_a_task(sqlite_db, settings):
    result = await run_pipeline(stages="nope", settings=settings)
    assert result.status == TaskStatus.SKIPPED.value
    assert result.task_id is None
    assert result.error and "unknown stage" in result.error


# ---------------------------------------------------------------- scheduler
@pytest.mark.asyncio
async def test_scheduler_registers_one_job_per_configured_time(settings):
    scheduler = PipelineScheduler(settings)
    scheduler.start()
    try:
        jobs = scheduler.jobs()
        assert len(jobs) == 3
        assert {job["id"] for job in jobs} == {
            "hot-pipeline-08:00",
            "hot-pipeline-12:00",
            "hot-pipeline-18:00",
        }
        for job in jobs:
            assert job["next_run_time"] is not None
            assert "cron" in job["trigger"]
            assert "hour=" in job["trigger"]
        assert scheduler.running is True
    finally:
        await scheduler.shutdown()
    assert scheduler.running is False
    assert scheduler.jobs() == []


@pytest.mark.asyncio
async def test_scheduler_start_is_idempotent(settings):
    scheduler = PipelineScheduler(settings)
    scheduler.start()
    scheduler.start()
    try:
        assert len(scheduler.jobs()) == 3
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_with_no_times_warns_and_registers_nothing(sqlite_db):
    from app.core.config import Settings

    scheduler = PipelineScheduler(Settings(hot_fetch_times="", database_url=""))
    scheduler.start()
    assert scheduler.jobs() == []
    assert scheduler.running is False

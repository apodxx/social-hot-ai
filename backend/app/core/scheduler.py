"""APScheduler wiring (Phase 5, spec section 廿二).

One cron job per entry in ``HOT_FETCH_TIMES`` (default 08:00/12:00/18:00, local
wall-clock in ``SCHEDULER_TIMEZONE``). Each job calls the same
:func:`~app.services.pipeline.runner.run_pipeline` the manual endpoint uses, so a
scheduled run and a manual run behave identically and are distinguished only by
``task_type``.

Three settings exist because unattended execution needs them:

* ``max_instances=1`` — a slow run is never overlapped by the next trigger.
* ``coalesce=True`` — if the process was down across a trigger time, the missed
  run happens once, not once per missed slot.
* ``misfire_grace_time`` — a trigger missed by less than five minutes still runs.

The runner itself also refuses to start while another task is ``running``, so the
two mechanisms agree rather than relying on either alone.
"""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings, get_settings
from app.services.pipeline.runner import run_pipeline

logger = logging.getLogger(__name__)

MISFIRE_GRACE_SECONDS = 300


class PipelineScheduler:
    """Owns the APScheduler instance for the hot-content pipeline."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._scheduler: AsyncIOScheduler | None = None

    @property
    def running(self) -> bool:
        """True when the scheduler was started."""
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        """Register one job per configured time and start the scheduler."""
        if self.running:
            logger.info("scheduler already running")
            return

        times = self._settings.hot_fetch_time_pairs
        if not times:
            logger.warning("HOT_FETCH_TIMES is empty; the scheduler has nothing to run")
            return

        timezone = ZoneInfo(self._settings.scheduler_timezone)
        scheduler = AsyncIOScheduler(timezone=timezone)
        for hour, minute in times:
            scheduler.add_job(
                self._scheduled_run,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
                id=f"hot-pipeline-{hour:02d}:{minute:02d}",
                name=f"hot content pipeline at {hour:02d}:{minute:02d}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=MISFIRE_GRACE_SECONDS,
                replace_existing=True,
            )
        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "scheduler started: %s (%s)",
            [f"{hour:02d}:{minute:02d}" for hour, minute in times],
            self._settings.scheduler_timezone,
        )

    async def shutdown(self) -> None:
        """Stop the scheduler without waiting for running jobs."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001 - shutdown must never fail the app
            logger.warning("scheduler shutdown raised: %s", exc)
        self._scheduler = None
        logger.info("scheduler stopped")

    async def _scheduled_run(self) -> None:
        """Job body. ``run_pipeline`` never raises, but the guard stays."""
        try:
            result = await run_pipeline(trigger="scheduled", settings=self._settings)
            logger.info(
                "scheduled pipeline finished: task=%s status=%s",
                result.task_id,
                result.status,
            )
        except Exception as exc:  # noqa: BLE001 - a job must not kill the scheduler
            logger.error("scheduled pipeline raised unexpectedly: %s", exc)

    def jobs(self) -> list[dict[str, Any]]:
        """Registered jobs with their next fire time (diagnostics)."""
        if self._scheduler is None:
            return []
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in self._scheduler.get_jobs()
        ]


def create_scheduler(settings: Settings | None = None) -> PipelineScheduler:
    """Build a scheduler from settings (does not start it)."""
    return PipelineScheduler(settings or get_settings())

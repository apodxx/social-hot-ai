"""``tasks``: one row per pipeline execution (Phase 5, spec section 二十一).

The spec's columns are honoured — ``id``, ``task_type``, ``status``,
``started_at``, ``finished_at``, ``error_message``, ``created_at`` — with four
additions that the spec's own requirements need:

* ``trigger`` — ``scheduled`` or ``manual``; section 二十三's manual endpoint
  exists for development, and mixing the two in one history with no way to tell
  them apart would make the log useless.
* ``steps`` — the per-step report (status, duration, billing, error). Section 廿二
  demands each step be logged and that one failing step never crash the
  scheduler, so the report is the evidence for both.
* ``summary`` — the run's own numbers (fetched/stored/analysed/rewritten).
* ``duration_ms`` — wall-clock total, computed once at the end.

``status`` uses :class:`TaskStatus`; ``partial`` means some steps succeeded and a
later one failed, which is the normal shape of a run that keeps its partial work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def utcnow() -> datetime:
    """Timezone-aware current time."""
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    """Lifecycle of one pipeline execution."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineTaskRecord(Base):
    """One pipeline run, scheduled or manual."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    #: ``scheduled`` or ``manual``.
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskStatus.RUNNING.value
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Per-step status, duration, billing flag, and error.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: The run's own counts, for the dashboard and the notification.
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_started", "started_at"),
        Index("ix_tasks_type", "task_type"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PipelineTaskRecord id={self.id} {self.task_type} status={self.status}>"

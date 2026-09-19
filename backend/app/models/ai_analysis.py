"""``ai_analyses``: one DeepSeek analysis per hot item (Phase 3).

The spec's column list is honoured in full, plus four additions that exist for
measurable reasons:

* ``discussion_points`` / ``account_fit`` / ``is_duplicate`` — section 十四 asks the
  model for exactly these judgements, and dropping them would leave the spec's own
  questions unanswered.
* ``model`` / ``prompt_tokens`` / ``completion_tokens`` / ``raw_response`` — AI
  spend must be auditable per row, and a wrong judgement must be debuggable
  against the answer that produced it.

``hot_content_id`` is unique: re-analysing an item updates its row rather than
accumulating versions, and Phase 4 reads the latest. The row also carries
``selected``, which is the output of the 5-10 item selection step.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def utcnow() -> datetime:
    """Timezone-aware current time."""
    return datetime.now(timezone.utc)


class AiAnalysisRecord(Base):
    """The DeepSeek judgement for one hot item."""

    __tablename__ = "ai_analyses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    hot_content_id: Mapped[int] = mapped_column(
        ForeignKey("hot_contents.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    topic_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("topic_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- the model's answer ---------------------------------------------------
    topic: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    why_hot: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_angle: Mapped[str] = mapped_column(Text, nullable=False, default="")
    discussion_points: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    account_fit: Mapped[str] = mapped_column(Text, nullable=False, default="")

    recommended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    needs_verification: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Set by the selection step: this item is one of the 5-10 that go to Phase 4.
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- provenance and cost --------------------------------------------------
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_ai_analyses_recommended", "recommended", "confidence"),
        Index("ix_ai_analyses_created", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<AiAnalysisRecord hot_content_id={self.hot_content_id} "
            f"recommended={self.recommended} confidence={self.confidence:.2f}>"
        )

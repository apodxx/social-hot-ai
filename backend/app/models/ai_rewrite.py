"""``ai_rewrites``: the three platform versions of one selected topic (Phase 4).

The spec's column list is honoured, with additions that the spec's own section 十七
makes necessary or that exist for auditability:

* ``xiaohongshu_ending`` — §17 requires 结尾互动 for the Xiaohongshu version.
* ``weibo_opening`` — §17 requires 开头 for the Weibo version.
* ``douyin_subtitles`` — §17 requires 字幕, which the column list omits.
* ``status`` / ``risk_flags`` — §20 requires ``NEEDS_REVIEW`` versus
  ``READY_TO_PUBLISH``; the status is computed in code, never taken on trust from
  the model.
* ``needs_verification`` / ``verification_note`` — §19's "原内容声称 / 目前可确认".
* ``model`` / tokens / ``raw_response`` — per-row cost auditing, as in Phase 3.
* ``attempts`` / ``copy_similarity`` — evidence for the §18 anti-copy check.

Source attribution (``source_url`` / ``source_platform`` / ``source_author``) is
**not** duplicated here: ``hot_content_id`` is a cascading foreign key, so the
source is always resolvable from ``hot_contents``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
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


class RewriteStatus(str, Enum):
    """Publication readiness. Only a human may act on the ready state."""

    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class AiRewriteRecord(Base):
    """One rewritten topic, in all three platform formats."""

    __tablename__ = "ai_rewrites"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    hot_content_id: Mapped[int] = mapped_column(
        ForeignKey("hot_contents.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    topic_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("topic_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- §17.1-17.3: the shared parts ----------------------------------------
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    why_hot: Mapped[str] = mapped_column(Text, nullable=False, default="")
    angle: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- §17.4: Xiaohongshu ---------------------------------------------------
    xiaohongshu_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    xiaohongshu_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    xiaohongshu_ending: Mapped[str] = mapped_column(Text, nullable=False, default="")
    xiaohongshu_hashtags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # --- §17.5: Weibo ---------------------------------------------------------
    weibo_opening: Mapped[str] = mapped_column(Text, nullable=False, default="")
    weibo_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    weibo_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    weibo_hashtags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # --- §17.6: Douyin --------------------------------------------------------
    douyin_hook: Mapped[str] = mapped_column(Text, nullable=False, default="")
    douyin_script: Mapped[str] = mapped_column(Text, nullable=False, default="")
    douyin_scene_suggestions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    douyin_subtitles: Mapped[str] = mapped_column(Text, nullable=False, default="")
    douyin_cta: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- §19/§20: verification and risk --------------------------------------
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RewriteStatus.NEEDS_REVIEW.value)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    needs_verification: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verification_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- provenance and cost --------------------------------------------------
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Highest similarity measured against the source title/body (the §18 check).
    copy_similarity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: The 图文 layout plan (Phase 9): which picture leads, what text to overlay, how
    #: the body maps onto the frames. Stored rather than derived because it is the
    #: model's output, and the images it refers to are indexed by position.
    layout: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_ai_rewrites_status", "status"),
        Index("ix_ai_rewrites_created", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<AiRewriteRecord hot_content_id={self.hot_content_id} "
            f"status={self.status} flags={self.risk_flags}>"
        )

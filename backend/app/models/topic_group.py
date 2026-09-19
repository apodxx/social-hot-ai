"""``topic_groups``: one row per cross-platform hot topic (dedup layer 4).

A topic group is the answer to "these three items are the same event on three
platforms". ``platforms`` holds the distinct platform names, and membership is
expressed by ``hot_contents.topic_group_id`` — so the member count the spec
displays as ``related_contents`` is derived by counting members rather than
stored, which cannot drift out of sync.

``summary`` stays empty in Phase 2: it is filled by the Phase 3 DeepSeek
analysis, and inventing text here would put unverified prose in the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def utcnow() -> datetime:
    """Timezone-aware current time."""
    return datetime.now(timezone.utc)


class TopicGroupRecord(Base):
    """A cluster of hot items that describe the same topic."""

    __tablename__ = "topic_groups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    #: Representative (human-readable) title of the cluster.
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    #: Normalised form used for matching; indexed for lookups.
    topic_normalized: Mapped[str] = mapped_column(String(512), nullable=False)

    #: Phase 3 fills this; empty in Phase 2 by design.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Distinct platform names present in the cluster, e.g. ["weibo", "douyin"].
    platforms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    members: Mapped[list["Any"]] = relationship(
        "HotContentRecord",
        back_populates="topic_group",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_topic_groups_normalized", "topic_normalized"),
        Index("ix_topic_groups_created", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TopicGroupRecord id={self.id} topic={self.topic[:30]!r} platforms={self.platforms}>"

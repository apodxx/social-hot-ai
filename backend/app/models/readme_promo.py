"""``readme_promos``: generated promotion copy for a project README (Phase 10).

Why its own table rather than a row in ``ai_rewrites``: a promotion is not a rewrite of
a hot item. It has no ``hot_content_id``, no source ranking, no §18 anti-copy check
against a platform post, and it is keyed by *content fingerprint* instead. Forcing it
into the rewrite table would mean a nullable source id and a meaning for every column
that does not apply.

``readme_sha256`` is stored so re-generating the same README is recognisable, and
``readme_chars`` / ``sent_chars`` record how much was actually sent — the difference
between the two is the truncation, and it should be visible in the row rather than
buried in a log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReadmePromoRecord(Base):
    """One generated set of promotion copy."""

    __tablename__ = "readme_promos"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    #: Where the README came from: a name, a file path, or a URL. Provenance, not identity.
    source_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    #: Content hash of the README text, so the same document is recognisable.
    readme_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    readme_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: How many characters were actually sent to the model (truncation is visible here).
    sent_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    project_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    one_liner: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cover_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: The three platform versions exactly as generated.
    versions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_points: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    unknowns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    image_ideas: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: The structured digest that was sent, kept so a wrong output can be traced.
    digest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: The Xiaohongshu note used as a style reference, if any: its shape (title length,
    #: paragraph count, hashtags) plus its id and author, so a reviewer can see what the
    #: copy learned from — and check that it was not copied.
    style_reference: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ReadmePromoRecord id={self.id} project={self.project_name[:24]!r}>"

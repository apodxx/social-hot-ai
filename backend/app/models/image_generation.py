"""``image_generations``: one row per paid image generation (Phase 11).

Its own table because an image generation is not a rewrite: it has its own price model
(**per image**, not per token), its own provider, its own failure modes, and it may be
run repeatedly on the same item with different prompts. Folded into ``ai_rewrites`` it
would need a second meaning for every column.

The row keeps the prompt, the reference images used, the provider's metering fields and
the local path of the result — so a surprising image can always be traced to the exact
instruction and reference that produced it, and the spend is auditable per image.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ImageGenerationRecord(Base):
    """One image generation (or edit) call."""

    __tablename__ = "image_generations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    #: The hot item the image was made for. Nullable: a standalone generation with no
    #: source item is legitimate (e.g. a cover made from scratch).
    hot_content_id: Mapped[int | None] = mapped_column(
        ForeignKey("hot_contents.id", ondelete="SET NULL"), nullable=True, index=True
    )

    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Reference images as ``media/...`` relative paths (what was actually sent).
    reference_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Provider result URLs. These expire in 24h, which is why the local copy exists.
    result_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Local copies under ``media/generated/`` — the durable artefact.
    local_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    size: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    requested_n: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: The provider's metering fields (image counts and billing tiers, not tokens).
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    estimated_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ImageGenerationRecord id={self.id} hot_content_id={self.hot_content_id} "
            f"status={self.status} images={len(self.local_paths or [])}>"
        )

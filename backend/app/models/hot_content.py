"""The unified domain model every platform adapter must produce.

This is the boundary that keeps TikHub's raw shapes out of the rest of the
system: adapters map their platform's JSON into :class:`HotContent`, and the AI
layer (Phase 3+) only ever sees this model. Swapping the data provider later
therefore touches only the adapters.

``raw_data`` keeps the provider's original item so a wrong mapping can always be
debugged, and so Phase 3 can re-read fields this phase does not surface yet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Platform(str, Enum):
    """Supported content platforms (Phase 1)."""

    XIAOHONGSHU = "xiaohongshu"
    WEIBO = "weibo"
    DOUYIN = "douyin"


class ContentType(str, Enum):
    """Coarse content kind, normalised across platforms."""

    NOTE = "note"
    VIDEO = "video"
    TEXT = "text"
    TOPIC = "topic"
    UNKNOWN = "unknown"


class ContentOrigin(str, Enum):
    """How an item entered the database.

    The distinction matters because a ranking entry and a search hit are different
    things: a hot word has no author and no media, a search hit is a real post. Mixing
    them in one list without saying which is which would make "why is this here"
    unanswerable, and the search results are the only place images and video come from.
    """

    HOT = "hot"
    SEARCH = "search"


def utcnow() -> datetime:
    """Timezone-aware current time."""
    return datetime.now(timezone.utc)


class MediaImage(BaseModel):
    """One image belonging to an item.

    ``local_path`` is filled in by the media library once the file has been fetched.
    It is stored rather than derived because the provider URLs are **signed and
    expire** (verified against a real capture: ``…?sign=…&t=…``), so the download is
    the only durable copy.
    """

    url: str
    #: Larger variant when the provider offers one (Xiaohongshu's 1440w image).
    url_large: str | None = None
    width: int | None = None
    height: int | None = None
    local_path: str | None = None
    bytes: int | None = None
    sha256: str | None = None


class MediaVideo(BaseModel):
    """Video material.

    Phase 9 stores the video's location and cover and does not download or process it
    — that was the explicit scope decision.
    """

    url: str | None = None
    cover_url: str | None = None
    duration_ms: int | None = None


class MediaBundle(BaseModel):
    """Everything visual an item carries, normalised across platforms."""

    images: list[MediaImage] = Field(default_factory=list)
    video: MediaVideo | None = None

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def has_image(self) -> bool:
        return bool(self.images)

    @property
    def has_video(self) -> bool:
        return bool(self.video and self.video.url)


def merge_media_downloads(previous: Any, incoming: MediaBundle) -> dict[str, Any]:
    """Carry downloaded-file facts forward when an item is refreshed.

    A scheduled run re-reads the same note and produces a media bundle with **no**
    local paths. Writing that over the stored one would forget every file already
    downloaded and leave those files orphaned on disk — while the material library is
    the only durable copy, because the provider URLs are signed and expire.

    Matching is by URL: if the provider still serves the same image, the download
    stays attached; a genuinely different image legitimately starts fresh.
    """
    stored = previous if isinstance(previous, dict) else {}
    carried: dict[str, dict[str, Any]] = {}
    for image in stored.get("images") or []:
        if isinstance(image, dict) and image.get("local_path") and image.get("url"):
            carried[str(image["url"])] = {
                key: image.get(key)
                for key in ("local_path", "bytes", "sha256")
                if image.get(key) is not None
            }

    payload = incoming.model_dump(mode="json")
    for image in payload.get("images") or []:
        kept = carried.get(str(image.get("url")))
        if kept:
            image.update(kept)
    return payload


class HotContent(BaseModel):
    """One hot item, platform-independent.

    Counters stay ``None`` when the provider does not supply them: an absent
    number is not zero, and Phase 3 must be able to tell the difference.
    """

    id: str
    platform: Platform
    platform_content_id: str

    title: str = ""
    description: str = ""

    author: str | None = None
    author_id: str | None = None

    url: str | None = None
    publish_time: datetime | None = None

    rank: int | None = None
    hot_value: int | None = None

    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    collects: int | None = None

    content_type: ContentType = ContentType.UNKNOWN

    cover_url: str | None = None
    video_url: str | None = None

    #: Images and video material (Phase 9). A hot *word* carries none of this; a
    #: search hit generally does.
    media: MediaBundle = Field(default_factory=MediaBundle)

    #: ``hot`` for a ranking/feed entry, ``search`` for a keyword hit.
    origin: ContentOrigin = ContentOrigin.HOT
    #: The keyword that produced this row, when ``origin`` is ``search``.
    source_keyword: str | None = None

    #: Provider's original item, kept verbatim for debugging and later phases.
    raw_data: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime | None = None

    @classmethod
    def make_id(cls, platform: Platform, platform_content_id: str) -> str:
        """Stable identity without a database: ``<platform>:<content id>``."""
        return f"{platform.value}:{platform_content_id}"


class HotContentRecord(Base):
    """The ``hot_contents`` table — one row per hot item.

    ``title_normalized`` and ``url_normalized`` are *derived* dedup keys, stored
    so deduplication layers 2 and 3 are expressible as SQL rather than needing
    every historical row in memory. They are recomputed on every upsert, so they
    can never drift from ``title``/``url``.
    """

    __tablename__ = "hot_contents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_content_id: Mapped[str] = mapped_column(String(128), nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hot_value: Mapped[int | None] = mapped_column(Integer, nullable=True)

    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collects: Mapped[int | None] = mapped_column(Integer, nullable=True)

    content_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")

    cover_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Phase 9: media and provenance --------------------------------------
    #: Normalised images/video as JSON (portable across PostgreSQL and SQLite, as
    #: every other structured column here is). The local paths recorded inside are
    #: what make an item usable after the provider's signed URLs expire.
    media: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: Derived from ``media`` so "show me items with pictures" is a SQL filter rather
    #: than a JSON traversal.
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="hot", index=True)
    source_keyword: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # --- derived dedup keys (layers 2 and 3) ---------------------------------
    title_normalized: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    url_normalized: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # --- section 十六 provenance --------------------------------------------
    # The fetched body itself lives in ``description`` (that is what the column is
    # for); these two record where and when it came from, so a body can always be
    # traced to the endpoint that returned it.
    detail_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    detail_endpoint: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- cross-platform aggregation (layer 4) --------------------------------
    topic_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("topic_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    topic_group: Mapped[Any | None] = relationship(
        "TopicGroupRecord", back_populates="members"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # Layer 1: the same platform can never contribute the same item twice.
        UniqueConstraint(
            "platform", "platform_content_id", name="uq_hot_contents_platform_content"
        ),
        Index("ix_hot_contents_platform_rank", "platform", "rank"),
        Index("ix_hot_contents_created", "created_at"),
        Index("ix_hot_contents_title_norm", "title_normalized"),
        Index("ix_hot_contents_url_norm", "url_normalized"),
    )

    @classmethod
    def from_domain(
        cls,
        item: HotContent,
        *,
        title_normalized: str,
        url_normalized: str | None,
    ) -> "HotContentRecord":
        """Build a row from the adapter-facing domain model."""
        return cls(
            platform=item.platform.value,
            platform_content_id=item.platform_content_id,
            title=item.title,
            description=item.description,
            author=item.author,
            author_id=item.author_id,
            url=item.url,
            publish_time=item.publish_time,
            rank=item.rank,
            hot_value=item.hot_value,
            likes=item.likes,
            comments=item.comments,
            shares=item.shares,
            collects=item.collects,
            content_type=item.content_type.value,
            cover_url=item.cover_url,
            video_url=item.video_url,
            media=item.media.model_dump(mode="json"),
            image_count=item.media.image_count,
            origin=item.origin.value,
            source_keyword=item.source_keyword,
            raw_data=item.raw_data,
            title_normalized=title_normalized,
            url_normalized=url_normalized,
        )

    def to_domain(self) -> HotContent:
        """Convert back to the adapter-facing model (used by the dedup window)."""
        return HotContent(
            id=HotContent.make_id(Platform(self.platform), self.platform_content_id),
            platform=Platform(self.platform),
            platform_content_id=self.platform_content_id,
            title=self.title or "",
            description=self.description or "",
            author=self.author,
            author_id=self.author_id,
            url=self.url,
            publish_time=self.publish_time,
            rank=self.rank,
            hot_value=self.hot_value,
            likes=self.likes,
            comments=self.comments,
            shares=self.shares,
            collects=self.collects,
            content_type=ContentType(self.content_type or ContentType.UNKNOWN.value),
            cover_url=self.cover_url,
            video_url=self.video_url,
            media=MediaBundle.model_validate(self.media or {}),
            origin=ContentOrigin(self.origin or ContentOrigin.HOT.value),
            source_keyword=self.source_keyword,
            raw_data=self.raw_data or {},
            created_at=self.created_at or utcnow(),
            updated_at=self.updated_at,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<HotContentRecord id={self.id} {self.platform}:{self.platform_content_id} "
            f"rank={self.rank} title={self.title[:24]!r}>"
        )

"""API response schemas for the Phase 1 endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HotContentOut(BaseModel):
    """Public projection of :class:`~app.models.hot_content.HotContent`.

    ``raw_data`` is deliberately absent; ``GET /api/hot?include_raw=true``
    returns it as an unmodelled passthrough instead.
    """

    id: str
    platform: str
    platform_content_id: str

    title: str
    description: str

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

    content_type: str
    cover_url: str | None = None
    video_url: str | None = None


class HotData(BaseModel):
    """Hot items keyed by platform, as the acceptance criteria require."""

    xiaohongshu: list[HotContentOut] = Field(default_factory=list)
    weibo: list[HotContentOut] = Field(default_factory=list)
    douyin: list[HotContentOut] = Field(default_factory=list)


class HotResponse(BaseModel):
    """``GET /api/hot`` envelope.

    ``success`` is true when at least one platform produced data; per-platform
    failures land in ``errors`` instead of failing the whole request, because a
    single dead platform must not hide the other two.
    """

    success: bool
    data: HotData
    errors: dict[str, str] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    limit_per_platform: int
    fetched_at: datetime


class ComponentStatus(BaseModel):
    """One dependency's health.

    ``skipped`` is distinct from ``not_configured`` on purpose: a key that is present
    but was deliberately not probed is *not* unconfigured, and reporting it that way
    told the operator to go configure something that was already set up.
    """

    status: Literal["connected", "error", "not_configured", "skipped"]
    detail: str | None = None
    latency_ms: float | None = None


class SystemStatusResponse(BaseModel):
    """``GET /api/system/status`` envelope."""

    success: bool
    phase: int
    components: dict[str, ComponentStatus]
    checked_at: datetime

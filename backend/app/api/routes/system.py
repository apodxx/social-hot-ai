"""``GET /api/system/status`` — dependency health.

TikHub is probed with a real authenticated request, because "the key works" is
the only useful answer and a purely local check would report ``connected`` for a
revoked key. The probe is a **metadata** call (account info), not a data
endpoint, and can be skipped with ``?probe=false`` for a zero-cost check.

PostgreSQL is probed with ``SELECT 1`` plus the row counts; the probe is skipped
when ``DATABASE_URL`` is empty.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.core.config import get_settings
from app.db.database import session_scope
from app.db.repository import dashboard_stats, database_stats
from app.schemas.hot import ComponentStatus, SystemStatusResponse
from app.services.ai.deepseek import DeepSeekClient, DeepSeekError
from app.services.tikhub import TikHubClient, TikHubError

logger = logging.getLogger(__name__)
router = APIRouter()

#: Account metadata endpoint used as the authenticated connectivity probe.
PROBE_PATH = "/api/v1/tikhub/user/get_user_info"


@router.get(
    "/system/status",
    response_model=SystemStatusResponse,
    summary="Dependency status",
)
async def system_status(
    probe: bool = Query(
        default=True,
        description="Actually call TikHub. Set false for a local-only check.",
    ),
) -> SystemStatusResponse:
    """Report TikHub and database status."""
    settings = get_settings()
    components: dict[str, ComponentStatus] = {}

    if not settings.tikhub_configured:
        components["tikhub"] = ComponentStatus(
            status="not_configured",
            detail="TIKHUB_API_KEY is empty or still the placeholder",
        )
    elif not probe:
        components["tikhub"] = ComponentStatus(
            status="skipped",
            detail="key present; probe skipped (?probe=false)",
        )
    else:
        started = time.perf_counter()
        try:
            async with TikHubClient(settings) as client:
                await client.get_json(PROBE_PATH)
        except TikHubError as exc:
            components["tikhub"] = ComponentStatus(
                status="error",
                detail=str(exc),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        else:
            components["tikhub"] = ComponentStatus(
                status="connected",
                detail=f"{settings.tikhub_base_url} ({PROBE_PATH})",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )

    components["database"] = await _database_status(settings)
    components["deepseek"] = await _deepseek_status(settings, probe=probe)

    healthy = components["tikhub"].status == "connected"
    return SystemStatusResponse(
        success=healthy,
        phase=10,
        components=components,
        checked_at=datetime.now(timezone.utc),
    )


@router.get("/system/stats", summary="Dashboard statistics (free)")
async def get_stats(
    today: bool = Query(
        default=True,
        description="Count items collected since local midnight; false counts everything.",
    ),
) -> dict[str, Any]:
    """The numbers the dashboard shows (§29), in one round trip."""
    settings = get_settings()
    since = None
    if today:
        # Local midnight in the configured scheduler timezone: "today" for the
        # operator, not for UTC.
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(settings.scheduler_timezone)
        except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
            zone = timezone.utc
        now = datetime.now(zone)
        since = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    async with session_scope(settings) as session:
        stats = await dashboard_stats(session, since=since)
    return {"success": True, "stats": stats}


async def _deepseek_status(settings, *, probe: bool) -> ComponentStatus:
    if not settings.deepseek_configured:
        return ComponentStatus(
            status="not_configured",
            detail="DEEPSEEK_API_KEY or DEEPSEEK_MODEL is empty",
        )
    if not probe:
        return ComponentStatus(
            status="skipped",
            detail=f"model={settings.deepseek_model}; probe skipped (?probe=false)",
        )
    started = time.perf_counter()
    async with DeepSeekClient(settings) as client:
        try:
            models = await client.list_models()
        except DeepSeekError as exc:
            return ComponentStatus(
                status="error",
                detail=str(exc),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
    configured = settings.deepseek_model
    detail = f"model={configured}; available={models}"
    if configured not in models:
        return ComponentStatus(
            status="error",
            detail=f"{detail} — configured model is not in the account's model list",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    return ComponentStatus(
        status="connected",
        detail=detail,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
    )


async def _database_status(settings) -> ComponentStatus:
    """Probe PostgreSQL and report row counts.

    Phase 1 reported ``not_configured`` unconditionally; Phase 2 has real tables,
    so the status is now measured: a ``SELECT 1`` plus the row counts, or the
    error that stopped it.
    """
    if not settings.database_url:
        return ComponentStatus(
            status="not_configured", detail="DATABASE_URL is empty"
        )
    started = time.perf_counter()
    try:
        async with session_scope(settings) as session:
            await session.execute(text("SELECT 1"))
            stats = await database_stats(session)
    except Exception as exc:  # noqa: BLE001 - any failure is reported, not raised
        return ComponentStatus(
            status="error",
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    return ComponentStatus(
        status="connected",
        detail=(
            f"{stats['hot_contents']} hot_contents, {stats['topic_groups']} topic_groups "
            f"(by platform: {stats['by_platform']})"
        ),
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
    )

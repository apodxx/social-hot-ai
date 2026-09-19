"""FastAPI application entry point.

Run it with the command the acceptance criteria name::

    cd backend && python -m app.main

The admin UI is served by this same process from ``frontend/dist``, so there is one
URL (http://127.0.0.1:8000/) and no CORS configuration.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException

from app.api.routes import (
    analysis,
    hot,
    image,
    knowledge,
    notification,
    pipeline,
    promo,
    rewrite,
    settings as settings_route,
    system,
)
from app.core.config import PROJECT_ROOT, get_settings
from app.core.logger import setup_logging
from app.core.scheduler import create_scheduler

logger = logging.getLogger(__name__)

DESCRIPTION = """
**SocialHot AI**: collect hot content from Xiaohongshu, Weibo and Douyin through the
TikHub API, deduplicate it, analyse and select it with DeepSeek, rewrite the selection
into three platform-specific drafts, and notify for review.

The system **never publishes anything by itself**: the pipeline ends at a draft plus a
notification, and an operator copies the text out to the platform.
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging, start the scheduler and the MCP session manager."""
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("SocialHot AI backend starting (phase 13)")
    if settings.tikhub_configured:
        logger.info(
            "TikHub configured: base_url=%s limit_per_platform=%d",
            settings.tikhub_base_url,
            settings.hot_limit_per_platform,
        )
    else:
        logger.warning(
            "TIKHUB_API_KEY is not set — /api/hot will return 503. Copy .env.example to .env."
        )
    if settings.deepseek_configured:
        logger.info("DeepSeek configured: model=%s", settings.deepseek_model)
    else:
        logger.warning("DEEPSEEK_API_KEY is not set — analysis and rewriting are unavailable")
    if not settings.database_url:
        logger.warning("DATABASE_URL is empty — the pipeline cannot persist anything")

    # A recurring charge the operator cannot see is one they cannot control, so the
    # watch stage's cost is stated at every start.
    if settings.watch_billed_calls_per_run:
        logger.warning(
            "domain watch searches enabled: %s on %s = %d billed TikHub call(s) per pipeline run "
            "(set WATCH_SEARCH_ENABLED=false to stop)",
            ", ".join(settings.watch_keyword_list),
            ", ".join(settings.watch_platform_list),
            settings.watch_billed_calls_per_run,
        )
    else:
        logger.info("domain watch searches disabled (no billed search calls per run)")
    logger.info(
        "domain interest keywords: %d configured, only-relevant analysed = %s",
        len(settings.interest_keyword_list),
        settings.interest_only,
    )

    scheduler = create_scheduler(settings)
    app.state.scheduler = scheduler
    if settings.scheduler_enabled:
        try:
            scheduler.start()
        except Exception as exc:  # noqa: BLE001 - a scheduler fault must not stop the API
            logger.error("scheduler failed to start: %s", exc)
    else:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=false)")
    try:
        # The MCP sub-app owns a lazily created session manager that only starts
        # inside its own lifespan. Mounting it without running this is the classic
        # failure that looks fine at startup and 500s on the first MCP request.
        mcp_app = getattr(app.state, "mcp_app", None)
        if mcp_app is None:
            yield
        else:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
    finally:
        await scheduler.shutdown()
        logger.info("SocialHot AI backend stopped")


def create_app() -> FastAPI:
    """Build the ASGI app (a factory so tests can build isolated instances)."""
    settings = get_settings()
    app = FastAPI(
        title="SocialHot AI",
        version="0.7.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.include_router(hot.router, prefix="/api", tags=["hot"])
    app.include_router(analysis.router, prefix="/api", tags=["analysis"])
    app.include_router(rewrite.router, prefix="/api", tags=["rewrite"])
    app.include_router(pipeline.router, prefix="/api", tags=["pipeline"])
    app.include_router(notification.router, prefix="/api", tags=["notification"])
    app.include_router(promo.router, prefix="/api", tags=["promo"])
    app.include_router(image.router, prefix="/api", tags=["image"])
    app.include_router(knowledge.router, prefix="/api", tags=["knowledge"])
    app.include_router(settings_route.router, prefix="/api", tags=["settings"])
    app.include_router(system.router, prefix="/api", tags=["system"])

    # MCP is mounted before the SPA catch-all: the frontend registers
    # ``GET /{full_path:path}``, and a route registered earlier wins, so mounting the
    # UI first would swallow ``GET /mcp/`` (the streamable-HTTP event stream) and
    # answer it with index.html. The media library has the same requirement.
    _mount_mcp(app)
    _mount_media(app)
    _mount_frontend(app)
    app.state.settings = settings
    return app


#: Built Vue app (Phase 7). Served by this same process so there is one URL and
#: no CORS: open http://127.0.0.1:8000/ and the admin UI is there.
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


def _mount_media(app: FastAPI) -> None:
    """Serve the downloaded material library at ``/media/`` (Phase 9).

    The admin UI has to display the images it downloaded, and the files live outside
    the frontend bundle, so they need their own mount. The directory is created on
    demand rather than at import: a deployment that never downloads anything should not
    get an empty folder it did not ask for.
    """
    from fastapi.staticfiles import StaticFiles

    settings = get_settings()
    root = settings.media_root_path
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - a read-only project directory
        logger.warning("media library not mounted: cannot create %s (%s)", root, exc)
        return
    app.mount("/media", StaticFiles(directory=root), name="media-library")
    logger.info("media library served from %s at /media/", root)


def _mount_frontend(app: FastAPI) -> None:
    """Serve ``frontend/dist`` when it exists, with SPA deep-link fallback."""
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    if not FRONTEND_DIST.is_dir():
        logger.info(
            "frontend build not found at %s — API only (run `npm run build` in frontend/)",
            FRONTEND_DIST,
        )
        return

    index_file = FRONTEND_DIST / "index.html"
    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():  # pragma: no cover - cosmetic
        candidate = FRONTEND_DIST / "favicon.ico"
        if candidate.is_file():
            return FileResponse(candidate)
        raise HTTPException(status_code=404, detail="no favicon")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """Serve a real file, else ``index.html`` so deep links survive a refresh."""
        if (
            full_path.startswith("api/")
            or full_path.startswith("assets/")
            or full_path.startswith("mcp/")
            or full_path.startswith("media/")
        ):
            raise HTTPException(status_code=404, detail="not found")
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index_file)

    logger.info("serving the admin UI from %s at /", FRONTEND_DIST)


def _mount_mcp(app: FastAPI) -> None:
    """Mount the MCP server at ``/mcp/`` (Phase 8).

    The client URL needs the trailing slash: Starlette redirects ``/mcp`` to
    ``/mcp/``, and a redirect in the middle of the streamable-http handshake is not
    something to rely on. The sub-app is served at its own root so the public path is
    exactly ``/mcp/``.
    """
    settings = get_settings()
    if not settings.mcp_enabled:
        logger.info("MCP server disabled (MCP_ENABLED=false)")
        return
    from app.mcp.server import BILLED_TOOL_NAMES, build_mcp_server, tool_names
    from mcp.server.transport_security import TransportSecuritySettings

    server = build_mcp_server(settings)
    # The streamable-HTTP transport validates the Host header (DNS-rebinding
    # protection) and answers anything unexpected with 421. Stated explicitly and
    # tied to the address we bind, rather than inheriting a library default that a
    # dependency upgrade could widen.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{settings.app_host}:*", "localhost:*", "[::1]:*"],
        allowed_origins=[
            f"http://{settings.app_host}:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )
    sub_app = server.streamable_http_app(streamable_http_path="/", transport_security=security)
    app.mount("/mcp", sub_app)
    app.state.mcp_app = sub_app
    names = tool_names(settings)
    logger.info(
        "MCP server mounted at /mcp/ with %d tools (billed: %s)",
        len(names),
        "enabled" if settings.mcp_allow_billed else "disabled by MCP_ALLOW_BILLED",
    )
    if settings.mcp_allow_billed:
        # Logged because the client-side approval gate matches these exact names.
        logger.info("MCP billed tools requiring approval: %s", ", ".join(BILLED_TOOL_NAMES))


app = create_app()


def main() -> None:
    """``python -m app.main`` — run the API with uvicorn."""
    import uvicorn

    settings = get_settings()
    setup_logging(settings.log_level)
    # ``log_config=None`` keeps uvicorn from installing its own handlers. Without it
    # every application line was emitted twice — once by our UTF-8 stdout handler and
    # once by uvicorn's stderr handler, where Chinese came out as replacement
    # characters. One readable copy beats two, one of them mangled.
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        log_level="info",
        log_config=None,
    )


if __name__ == "__main__":
    main()

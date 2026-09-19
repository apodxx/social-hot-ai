"""Phase 8: the MCP endpoint, exercised through the real MCP client protocol.

These tests do not poke at Python functions — they speak MCP to the server through the
SDK's own client. That is deliberate: the failure modes here live in the transport
(mount path, trailing slash, the sub-app's session-manager lifespan), and every one of
them passes a unit test of the tool functions while producing a server no real client
can use.

No test spends money: the billed tools are only *listed*, never called.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from app.core.config import Settings, get_settings
from app.main import create_app
from app.mcp.server import BILLED_TOOL_NAMES, build_mcp_server, tool_names

#: The read-only tools, which are always registered regardless of MCP_ALLOW_BILLED.
FREE_TOOL_NAMES = [
    "get_stats",
    "list_hot",
    "get_topics",
    "get_analysis",
    "get_rewrite",
    "list_rewrites",
    "list_tasks",
    "get_schedule",
    "get_interest_profile",
    "list_readme_promos",
    "get_readme_promo",
    "get_image_estimate",
    "list_generated_images",
    "list_knowledge_tags",
    "list_knowledge_articles",
    "get_knowledge_article",
]


@pytest.fixture
def mcp_settings(monkeypatch, sqlite_db):
    """Settings for an in-memory database with billing allowed (the default)."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    monkeypatch.delenv("MCP_ALLOW_BILLED", raising=False)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _client_session(settings: Settings):
    """A real MCP client bound to a freshly built server.

    ``mcp.client.Client`` accepts the server object directly, so these tests speak the
    protocol a remote client speaks without standing up a socket.
    """
    return Client(build_mcp_server(settings))


# ------------------------------------------------------------------ tool surface
@pytest.mark.asyncio
async def test_tools_list_exposes_free_and_billed_tools(mcp_settings):
    async with _client_session(mcp_settings) as session:
        listed = await session.list_tools()

    names = [tool.name for tool in listed.tools]
    for expected in FREE_TOOL_NAMES:
        assert expected in names, expected
    for billed in BILLED_TOOL_NAMES:
        assert billed in names, billed
    assert len(names) == len(set(names)), "no duplicate tool names"


@pytest.mark.asyncio
async def test_every_billed_tool_says_it_costs_money(mcp_settings):
    """The model reads the description before choosing; so does the approver."""
    async with _client_session(mcp_settings) as session:
        listed = await session.list_tools()

    by_name = {tool.name: tool for tool in listed.tools}
    for billed in BILLED_TOOL_NAMES:
        description = by_name[billed].description or ""
        assert "\u8ba1\u8d39" in description, f"{billed} must state that it spends money"


@pytest.mark.asyncio
async def test_billed_tools_are_absent_when_disallowed(monkeypatch, sqlite_db):
    """``MCP_ALLOW_BILLED=false`` removes them, so they cannot be called at all."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("MCP_ALLOW_BILLED", "false")
    get_settings.cache_clear()
    settings = get_settings()
    try:
        assert tool_names(settings) == FREE_TOOL_NAMES
        async with _client_session(settings) as session:
            listed = await session.list_tools()
        names = [tool.name for tool in listed.tools]
        for billed in BILLED_TOOL_NAMES:
            assert billed not in names, f"{billed} must not be registered"
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------- tool calls
@pytest.mark.asyncio
async def test_calling_a_tool_returns_json(mcp_settings):
    """A real call through the protocol, not a direct function invocation."""
    from app.db.database import session_scope
    from app.db.repository import upsert_items
    from app.models.hot_content import ContentType, HotContent, Platform

    async with session_scope(mcp_settings) as session:
        await upsert_items(
            session,
            [
                HotContent(
                    id=HotContent.make_id(Platform.WEIBO, "w1"),
                    platform=Platform.WEIBO,
                    platform_content_id="w1",
                    title="\u67d0\u4e2a\u70ed\u70b9",
                    url="https://s.weibo.com/weibo?q=x",
                    hot_value=12345,
                    rank=1,
                    content_type=ContentType.TOPIC,
                )
            ],
        )

    async with _client_session(mcp_settings) as session:
        result = await session.call_tool("list_hot", {"limit": 5})

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["total"] == 1
    assert payload["items"][0]["title"] == "\u67d0\u4e2a\u70ed\u70b9"
    assert payload["items"][0]["hot_value"] == 12345
    assert "raw_data" not in payload["items"][0], "raw payloads must not enter context"


@pytest.mark.asyncio
async def test_missing_analysis_reports_not_found_instead_of_failing(mcp_settings):
    async with _client_session(mcp_settings) as session:
        result = await session.call_tool("get_analysis", {"hot_content_id": 999999})
    assert result.is_error is False
    assert json.loads(result.content[0].text)["found"] is False


@pytest.mark.asyncio
async def test_stats_tool_returns_counters(mcp_settings):
    async with _client_session(mcp_settings) as session:
        result = await session.call_tool("get_stats", {"today": False})
    payload = json.loads(result.content[0].text)
    assert "total_items" in payload and "needs_review" in payload


# ----------------------------------------------------- the mounted HTTP endpoint
@pytest.fixture
def http_client(monkeypatch, sqlite_db, tmp_path):
    """A TestClient over the real app, so the mount path itself is under test.

    ``base_url`` is the deployment's own address on purpose: the transport validates
    the ``Host`` header and answers anything else with 421, so a client pointed at
    ``testserver`` would be testing an address no real client uses.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as client:
        yield client
    get_settings.cache_clear()


def _initialize(client: TestClient, *, host: str | None = None):
    headers = {"Accept": "application/json, text/event-stream"}
    if host:
        headers["Host"] = host
    return client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        headers=headers,
    )


def test_mcp_endpoint_is_mounted_and_answers_initialize(http_client):
    """The public path is ``/mcp/``; a client must be able to initialise there."""
    response = _initialize(http_client)
    assert response.status_code == 200, response.text
    assert "socialhot" in response.text


def test_mcp_rejects_a_foreign_host_header(http_client):
    """DNS-rebinding protection: only this machine's own names may reach the endpoint.

    If this ever starts passing with an arbitrary Host, the MCP endpoint has become
    reachable by name from outside — which on a laptop means any web page the browser
    loads could POST to it.
    """
    response = _initialize(http_client, host="evil.example.com")
    assert response.status_code == 421, response.text


def test_mcp_path_is_not_swallowed_by_the_spa_fallback(http_client):
    """The UI catch-all must never answer an MCP request with index.html."""
    response = http_client.get("/mcp/", headers={"Accept": "text/event-stream"})
    body = response.text
    if response.status_code == 200:
        assert "text/html" not in response.headers.get("content-type", ""), body[:200]
    else:
        # No session yet: a protocol error is fine, a web page is not.
        assert "<!doctype html>" not in body.lower()

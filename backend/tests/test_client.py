"""TikHub client tests: retries, the error taxonomy, and body-level failures.

Every test mocks the transport with ``respx``; nothing here costs money.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.tikhub.client import (
    TikHubAuthError,
    TikHubClient,
    TikHubNetworkError,
    TikHubNotFoundError,
    TikHubRateLimitError,
    TikHubResponseError,
    TikHubServerError,
)

URL = "https://api.tikhub.io/api/v1/example/endpoint"


@pytest.mark.asyncio
async def test_success_returns_body(settings):
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, json={"code": 200, "data": [1]}))
        async with TikHubClient(settings) as client:
            payload = await client.get_json("/api/v1/example/endpoint")
    assert payload["data"] == [1]


@pytest.mark.asyncio
async def test_auth_error_is_not_retried(settings):
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(401, json={"message": "bad key"}))
        async with TikHubClient(settings) as client:
            with pytest.raises(TikHubAuthError):
                await client.get_json("/api/v1/example/endpoint")
    assert route.call_count == 1, "a rejected key must not be retried"


@pytest.mark.asyncio
async def test_not_found_is_terminal(settings):
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(404, json={"detail": "nope"}))
        async with TikHubClient(settings) as client:
            with pytest.raises(TikHubNotFoundError):
                await client.get_json("/api/v1/example/endpoint")
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_rate_limit_then_success(settings):
    with respx.mock:
        route = respx.get(URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
                httpx.Response(200, json={"code": 200, "data": "ok"}),
            ]
        )
        async with TikHubClient(settings) as client:
            payload = await client.get_json("/api/v1/example/endpoint")
    assert payload["data"] == "ok"
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_rate_limit_retry_budget_is_bounded(settings):
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(429, json={}))
        async with TikHubClient(settings) as client:
            with pytest.raises(TikHubRateLimitError):
                await client.get_json("/api/v1/example/endpoint")
    # max_retries=2 -> at most 3 attempts, never unbounded.
    assert route.call_count == settings.tikhub_max_retries + 1


@pytest.mark.asyncio
async def test_server_error_then_success(settings):
    with respx.mock:
        route = respx.get(URL).mock(
            side_effect=[
                httpx.Response(503, json={}),
                httpx.Response(200, json={"code": 200, "data": "recovered"}),
            ]
        )
        async with TikHubClient(settings) as client:
            payload = await client.get_json("/api/v1/example/endpoint")
    assert payload["data"] == "recovered"
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_server_error_exhausts_budget(settings):
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(500, json={}))
        async with TikHubClient(settings) as client:
            with pytest.raises(TikHubServerError):
                await client.get_json("/api/v1/example/endpoint")
    assert route.call_count == settings.tikhub_max_retries + 1


@pytest.mark.asyncio
async def test_timeout_becomes_network_error(settings):
    with respx.mock:
        route = respx.get(URL).mock(side_effect=httpx.TimeoutException("too slow"))
        async with TikHubClient(settings) as client:
            with pytest.raises(TikHubNetworkError):
                await client.get_json("/api/v1/example/endpoint")
    assert route.call_count == settings.tikhub_max_retries + 1


@pytest.mark.asyncio
async def test_body_level_failure_code_is_rejected(settings):
    """A 200 carrying a >=400 ``code`` is a failure, not data."""
    with respx.mock:
        respx.get(URL).mock(
            return_value=httpx.Response(200, json={"code": 401, "message": "unauthorized"})
        )
        async with TikHubClient(settings) as client:
            with pytest.raises(TikHubResponseError):
                await client.get_json("/api/v1/example/endpoint")


@pytest.mark.asyncio
async def test_non_json_body_is_rejected(settings):
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
        async with TikHubClient(settings) as client:
            with pytest.raises(TikHubResponseError):
                await client.get_json("/api/v1/example/endpoint")


@pytest.mark.asyncio
async def test_none_params_are_dropped(settings):
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json={"code": 200}))
        async with TikHubClient(settings) as client:
            await client.get_json("/api/v1/example/endpoint", {"num": 40, "cursor_score": None})
    assert "cursor_score" not in str(route.calls[0].request.url)
    assert "num=40" in str(route.calls[0].request.url)

"""DeepSeek client tests: JSON recovery, retries, and token accounting.

The transport is mocked with respx, so nothing here contacts DeepSeek or spends
tokens.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.ai.deepseek import (
    DeepSeekAuthError,
    DeepSeekClient,
    DeepSeekJSONError,
    DeepSeekRateLimitError,
    DeepSeekResponseError,
    DeepSeekServerError,
    extract_json,
)

CHAT_URL = "https://api.deepseek.com/chat/completions"
MODELS_URL = "https://api.deepseek.com/models"


def _completion(content: str, *, prompt: int = 100, completion: int = 50) -> dict:
    return {
        "model": "deepseek-flash",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


# ------------------------------------------------------------- JSON recovery
@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Sure, here it is:\n{"a": 1}\nHope that helps.',
    ],
)
def test_extract_json_accepts_objects_with_noise(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_accepts_arrays():
    assert extract_json('[{"index": 0}]') == [{"index": 0}]


@pytest.mark.parametrize("text", ["", "   ", "no json here", "{broken", None])
def test_extract_json_rejects_unparseable_output(text):
    with pytest.raises(DeepSeekJSONError):
        extract_json(text)


# ------------------------------------------------------------------- chat
@pytest.mark.asyncio
async def test_chat_parses_content_and_usage(settings):
    with respx.mock:
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_completion('{"ok": true}')))
        async with DeepSeekClient(settings) as client:
            result = await client.chat([{"role": "user", "content": "hi"}])
    assert result.content == '{"ok": true}'
    assert result.usage.total_tokens == 150
    assert client.total_usage.total_tokens == 150
    assert client.call_count == 1
    assert result.usage.estimated_cny() > 0


@pytest.mark.asyncio
async def test_usage_accumulates_across_calls(settings):
    with respx.mock:
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=_completion('{"ok": true}'))
        )
        async with DeepSeekClient(settings) as client:
            await client.chat([{"role": "user", "content": "one"}])
            await client.chat([{"role": "user", "content": "two"}])
    assert client.call_count == 2
    assert client.total_usage.total_tokens == 300


@pytest.mark.asyncio
async def test_auth_error_is_terminal(settings):
    with respx.mock:
        route = respx.post(CHAT_URL).mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
        )
        async with DeepSeekClient(settings) as client:
            with pytest.raises(DeepSeekAuthError):
                await client.chat([{"role": "user", "content": "hi"}])
    assert route.call_count == 1, "a rejected key must not be retried"


@pytest.mark.asyncio
async def test_rate_limit_retries_then_succeeds(settings, monkeypatch):
    monkeypatch.setattr(DeepSeekClient, "_retry_delay", lambda self, attempt, response: 0.0)
    with respx.mock:
        route = respx.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(429, json={"error": {"message": "slow down"}}),
                httpx.Response(200, json=_completion('{"ok": true}')),
            ]
        )
        async with DeepSeekClient(settings) as client:
            result = await client.chat([{"role": "user", "content": "hi"}])
    assert result.content == '{"ok": true}'
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_rate_limit_budget_is_bounded(settings, monkeypatch):
    monkeypatch.setattr(DeepSeekClient, "_retry_delay", lambda self, attempt, response: 0.0)
    with respx.mock:
        route = respx.post(CHAT_URL).mock(return_value=httpx.Response(429, json={}))
        async with DeepSeekClient(settings) as client:
            with pytest.raises(DeepSeekRateLimitError):
                await client.chat([{"role": "user", "content": "hi"}])
    assert route.call_count == settings.deepseek_max_retries + 1


@pytest.mark.asyncio
async def test_server_error_exhausts_budget(settings, monkeypatch):
    monkeypatch.setattr(DeepSeekClient, "_retry_delay", lambda self, attempt, response: 0.0)
    with respx.mock:
        respx.post(CHAT_URL).mock(return_value=httpx.Response(503, json={}))
        async with DeepSeekClient(settings) as client:
            with pytest.raises(DeepSeekServerError):
                await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_body_without_choices_is_an_error(settings):
    with respx.mock:
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"choices": []}))
        async with DeepSeekClient(settings) as client:
            with pytest.raises(DeepSeekResponseError):
                await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_complete_json_parses_and_rejects_prose(settings):
    with respx.mock:
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=_completion('{"results": []}'))
        )
        async with DeepSeekClient(settings) as client:
            parsed, _result = await client.complete_json(system="s", user="u")
    assert parsed == {"results": []}

    with respx.mock:
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=_completion("当然可以！这是分析结果："))
        )
        async with DeepSeekClient(settings) as client:
            with pytest.raises(DeepSeekJSONError):
                await client.complete_json(system="s", user="u")


@pytest.mark.asyncio
async def test_list_models_costs_nothing_and_reads_ids(settings):
    with respx.mock:
        respx.get(MODELS_URL).mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]}
            )
        )
        async with DeepSeekClient(settings) as client:
            models = await client.list_models()
    assert models == ["deepseek-flash", "deepseek-v4-pro"]

"""DeepSeek chat client with strict JSON output and token accounting.

Same shape as the TikHub client (bounded retries, an error taxonomy, request
logging) plus the two things AI calls need that HTTP calls do not:

* **Strict JSON parsing.** The spec forbids "unparseable large blocks of text",
  so :meth:`DeepSeekClient.complete_json` accepts only a JSON object or array,
  tolerating a fenced code block or surrounding prose around it but never
  inventing structure that is not there.
* **Token accounting.** Every response's ``usage`` is recorded and summed, and
  logged per call, because the only way to keep AI spend honest is to measure it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

MAX_RETRY_DELAY_SECONDS = 20.0

#: ```json ... ``` or ``` ... ``` fences around a payload.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

#: CNY per 1M tokens. Used only to print an estimate; the authoritative number
#: is DeepSeek's own billing. Values are the published deepseek-chat tier and are
#: reported as an estimate, never as a fact.
ESTIMATED_CNY_PER_MILLION_PROMPT = 2.0
ESTIMATED_CNY_PER_MILLION_COMPLETION = 8.0


class DeepSeekError(RuntimeError):
    """Base class for every DeepSeek failure."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class DeepSeekAuthError(DeepSeekError):
    """401/403 — the key is missing, wrong, or out of credit."""


class DeepSeekRateLimitError(DeepSeekError):
    """429 — throttled, and the retry budget ran out."""


class DeepSeekServerError(DeepSeekError):
    """5xx — the provider is broken, and the retry budget ran out."""


class DeepSeekNetworkError(DeepSeekError):
    """Timeout or transport failure after the retry budget ran out."""


class DeepSeekResponseError(DeepSeekError):
    """A 2xx whose body carried no usable choice."""


class DeepSeekJSONError(DeepSeekError):
    """The model's answer was not parseable JSON (never stored as text)."""


class DeepSeekTruncatedError(DeepSeekJSONError):
    """The answer was cut off by ``max_tokens``, so its JSON is incomplete.

    A separate class because the two failures need different responses: a syntax error
    cannot be fixed by trying again the same way, while a truncation can — retry with a
    smaller ask. Without this distinction the caller only saw "not parseable as JSON"
    and could not tell which had happened.
    """

    def __init__(
        self, message: str, *, completion_tokens: int = 0, finish_reason: str = ""
    ) -> None:
        super().__init__(message)
        self.completion_tokens = completion_tokens
        self.finish_reason = finish_reason


@dataclass
class ChatUsage:
    """Token usage reported by the provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "ChatUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens

    def estimated_cny(self) -> float:
        """Rough cost estimate in CNY (see the constants' caveat)."""
        return (
            self.prompt_tokens / 1_000_000 * ESTIMATED_CNY_PER_MILLION_PROMPT
            + self.completion_tokens / 1_000_000 * ESTIMATED_CNY_PER_MILLION_COMPLETION
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ChatResult:
    """One completion plus what it cost."""

    content: str
    model: str
    usage: ChatUsage = field(default_factory=ChatUsage)
    latency_ms: float = 0.0
    finish_reason: str = ""
    #: Function calling 时模型要求调用的工具（``[{"name":..., "arguments": {...}}]``）。
    #: 普通对话为空列表。放在这里而不是另开一个返回类型，是为了让调用方只处理一种结果。
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def extract_json(text: str) -> Any:
    """Parse ``text`` as JSON, tolerating fences and surrounding prose.

    Raises :class:`DeepSeekJSONError` when no JSON value can be recovered — the
    caller must treat that as a failure, not as content.
    """
    if text is None:
        raise DeepSeekJSONError("empty response")
    stripped = text.strip()
    if not stripped:
        raise DeepSeekJSONError("empty response")

    candidates: list[str] = []
    fence = _FENCE_RE.search(stripped)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(stripped)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # 中文长文本里最常见的一种坏法：字符串内部出现**真实的换行/制表符**。
        # JSON 规范不允许，但模型经常这么写。``strict=False`` 正是为此存在
        # （放宽字符串内的控制字符），所以先免费试一次，省掉一次重试调用。
        try:
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            pass
        # Fall back to the outermost {...} or [...] region.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = candidate.find(opener)
            end = candidate.rfind(closer)
            if start != -1 and end > start:
                region = candidate[start : end + 1]
                try:
                    return json.loads(region)
                except json.JSONDecodeError:
                    pass
                try:
                    return json.loads(region, strict=False)
                except json.JSONDecodeError:
                    continue
    raise DeepSeekJSONError(
        f"not parseable as JSON: {stripped[:200]!r}"
        f"（响应共 {len(stripped)} 字符，结尾：{stripped[-120:]!r}）"
    )


class DeepSeekClient:
    """Async client for the DeepSeek chat-completions API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.deepseek_timeout_seconds),
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
        )
        #: Sum of every call made through this client (for the run report).
        self.total_usage = ChatUsage()
        self.call_count = 0

    async def __aenter__(self) -> "DeepSeekClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the pool when this client owns it."""
        if self._owns_client:
            await self._client.aclose()

    @property
    def model(self) -> str:
        """Configured model name."""
        return self._settings.deepseek_model

    async def list_models(self) -> list[str]:
        """Available model ids (free: no tokens are billed)."""
        response = await self._client.get("/models")
        if response.status_code >= 400:
            raise self._error_for(response)
        payload = response.json()
        return [item.get("id", "") for item in payload.get("data", [])]

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        allow_reasoning_fallback: bool = False,
    ) -> ChatResult:
        """One chat completion, with bounded retries.

        ``tools`` 传入 function calling 的工具定义（OpenAI 格式）。
        **传了 tools 就必须关掉 ``response_format=json_object``** —— 两者互斥，
        同时带会被服务端拒绝。

        ``allow_reasoning_fallback`` 默认 **False**。曾经默认开启，后果是
        **把模型的内心独白当成答复发给了用户**：路由调用返回空 content 时，
        兜底把 ``reasoning_content`` 填了进去，于是 QQ 群里出现了三大段
        "Hmm, ambiguous… I'll go with…" 这样的英文思考过程。
        **思考过程不是答复**，只在明确知道调用方需要"拿到点东西总比空着好"时才开。
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": (
                self._settings.deepseek_temperature if temperature is None else temperature
            ),
            "max_tokens": self._settings.deepseek_max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"
        elif json_mode:
            body["response_format"] = {"type": "json_object"}

        attempts = self._settings.deepseek_max_retries + 1
        last_error: DeepSeekError | None = None
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._client.post("/chat/completions", json=body)
            except httpx.TimeoutException as exc:
                last_error = DeepSeekNetworkError(f"timeout: {exc}")
            except httpx.HTTPError as exc:
                last_error = DeepSeekNetworkError(f"transport failure: {exc}")
            else:
                elapsed_ms = (time.perf_counter() - started) * 1000
                if response.status_code < 400:
                    result = self._parse_completion(
                        response,
                        elapsed_ms,
                        allow_reasoning_fallback=allow_reasoning_fallback,
                    )
                    self.call_count += 1
                    self.total_usage.add(result.usage)
                    logger.info(
                        "deepseek %s -> %d tokens (prompt %d / completion %d, ~CNY %.4f) in %.0fms",
                        result.model,
                        result.usage.total_tokens,
                        result.usage.prompt_tokens,
                        result.usage.completion_tokens,
                        result.usage.estimated_cny(),
                        elapsed_ms,
                    )
                    return result

                last_error = self._error_for(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
                if attempt < attempts:
                    delay = self._retry_delay(attempt, response)
                    logger.warning(
                        "deepseek returned %d; retrying in %.1fs", response.status_code, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise last_error

            logger.warning("deepseek call failed (%s) attempt %d/%d", last_error, attempt, attempts)
            if attempt < attempts:
                await asyncio.sleep(self._retry_delay(attempt, None))
                continue
            raise last_error

        raise last_error or DeepSeekError("request failed without a recorded error")

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[Any, ChatResult]:
        """A chat completion parsed as JSON.

        Returns ``(parsed, result)``; raises :class:`DeepSeekJSONError` when the
        answer cannot be parsed, so no caller can accidentally store prose.

        Raises :class:`DeepSeekTruncatedError` (a subclass) when the answer was cut off
        by ``max_tokens`` — the caller can then retry with a smaller ask instead of
        giving up on a run that was already paid for.
        """
        result = await self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            return extract_json(result.content), result
        except DeepSeekJSONError as exc:
            if result.finish_reason == "length":
                raise DeepSeekTruncatedError(
                    f"输出在 {result.usage.completion_tokens} tokens 处被截断"
                    f"（finish_reason=length），JSON 不完整；响应共 {len(result.content)} 字符。"
                    f"结尾：{result.content[-120:]!r}",
                    completion_tokens=result.usage.completion_tokens,
                    finish_reason=result.finish_reason,
                ) from exc
            raise

    # ----------------------------------------------------------------- private
    def _parse_completion(
        self,
        response: httpx.Response,
        elapsed_ms: float,
        *,
        allow_reasoning_fallback: bool = False,
    ) -> ChatResult:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeepSeekResponseError(f"response was not JSON: {exc}") from exc
        choices = payload.get("choices") or []
        if not choices:
            raise DeepSeekResponseError("response contained no choices", body=payload)
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        # **空 content 时回退到 reasoning_content。** ``deepseek-flash`` 是推理模型，
        # 实测出现过 finish_reason='stop'、content 为空、而思考内容在
        # ``reasoning_content`` 里的情况——那时调用方只会看到"模型没有输出内容"，
        # 完全查不出原因（用户实际遇到的就是这个）。宁可把思考内容交出去并标注，
        # 也不要返回空。
        # **空 content 时是否回退到 reasoning_content —— 默认不回退。**
        # 曾经默认回退，后果是把模型的内心独白当成答复发给了用户
        # （QQ 群里出现了三段 "Hmm, ambiguous… I'll go with…" 的英文思考过程）。
        # 思考过程不是答复；只有在调用方明确接受"拿到点东西总比空着好"时才开。
        # ``finish_reason='tool_calls'`` 时 content 为空本就是正常的，任何情况都不回退。
        finish_reason = choice.get("finish_reason") or ""
        if (
            allow_reasoning_fallback
            and not content.strip()
            and finish_reason != "tool_calls"
        ):
            reasoning = (message.get("reasoning_content") or "").strip()
            if reasoning:
                logger.warning(
                    "deepseek returned empty content with finish_reason=%r; "
                    "falling back to reasoning_content (%d chars)",
                    finish_reason,
                    len(reasoning),
                )
                content = reasoning
        usage_payload = payload.get("usage") or {}
        usage = ChatUsage(
            prompt_tokens=int(usage_payload.get("prompt_tokens") or 0),
            completion_tokens=int(usage_payload.get("completion_tokens") or 0),
            total_tokens=int(usage_payload.get("total_tokens") or 0),
        )
        # function calling：把模型要求的工具调用解出来。arguments 是 JSON **字符串**，
        # 解析失败时保留原文并置空参数字典——让调用方能报出"模型给了非法参数"，
        # 而不是在这里抛异常把整轮对话打断。
        tool_calls: list[dict[str, Any]] = []
        for raw in message.get("tool_calls") or []:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function") or {}
            raw_args = function.get("arguments")
            arguments: dict[str, Any] = {}
            parse_error = ""
            if isinstance(raw_args, dict):
                arguments = raw_args
            elif isinstance(raw_args, str) and raw_args.strip():
                try:
                    parsed = json.loads(raw_args, strict=False)
                    arguments = parsed if isinstance(parsed, dict) else {"value": parsed}
                except ValueError as exc:
                    parse_error = f"{exc}"
            tool_calls.append(
                {
                    "id": raw.get("id") or "",
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                    "raw_arguments": raw_args if isinstance(raw_args, str) else "",
                    "parse_error": parse_error,
                }
            )
        return ChatResult(
            content=content,
            model=payload.get("model") or self.model,
            usage=usage,
            latency_ms=round(elapsed_ms, 1),
            finish_reason=choice.get("finish_reason") or "",
            tool_calls=tool_calls,
        )

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    return min(float(header), MAX_RETRY_DELAY_SECONDS)
                except ValueError:
                    pass
        base = 1.0 * (2 ** (attempt - 1))
        return min(base * (1 + random.random() * 0.25), MAX_RETRY_DELAY_SECONDS)

    def _error_for(self, response: httpx.Response) -> DeepSeekError:
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text[:500]
        message = ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
            elif isinstance(error, str):
                message = error
            message = message or str(body.get("message") or "")
        message = message or f"HTTP {response.status_code}"
        if response.status_code in (401, 403):
            return DeepSeekAuthError(
                f"DeepSeek rejected the key (or the account is out of credit): {message}",
                status=response.status_code,
                body=body,
            )
        if response.status_code == 429:
            return DeepSeekRateLimitError(f"rate limited: {message}", status=429, body=body)
        if response.status_code >= 500:
            return DeepSeekServerError(f"provider error: {message}", status=response.status_code, body=body)
        return DeepSeekError(f"request failed: {message}", status=response.status_code, body=body)


def default_client(settings: Settings | None = None) -> DeepSeekClient:
    """Build a client from settings."""
    return DeepSeekClient(settings or get_settings())
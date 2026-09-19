"""TikHub HTTP client.

One place owns everything about talking to TikHub: the base URL, the bearer
header, timeouts, bounded retries, the error taxonomy, and request logging.
Adapters only ever see a parsed JSON body or a :class:`TikHubError`.

Retry policy (see :data:`RETRYABLE_STATUS`): 429 and 5xx plus transport
failures are retried with exponential backoff and jitter, honouring
``Retry-After`` when the server sends it. 4xx other than 429 are terminal —
retrying a bad request or a rejected key only wastes time and quota. The retry
budget is bounded by ``TIKHUB_MAX_RETRIES`` so no request can loop forever.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

#: Statuses worth another attempt: throttling and server-side breakage.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: Cap on a server-provided ``Retry-After`` so one slow endpoint cannot stall a run.
MAX_RETRY_AFTER_SECONDS = 30.0


class TikHubError(RuntimeError):
    """Base class for every TikHub failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        path: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        parts = [super().__str__()]
        if self.path:
            parts.append(f"path={self.path}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        return " | ".join(parts)


class TikHubAuthError(TikHubError):
    """401/403 — the key is missing, wrong, or lacks the endpoint's scope."""


class TikHubNotFoundError(TikHubError):
    """404 — the endpoint does not exist (a wrong path is a code bug)."""


class TikHubRateLimitError(TikHubError):
    """429 — throttled, and the retry budget ran out."""


class TikHubServerError(TikHubError):
    """5xx — the provider is broken, and the retry budget ran out."""


class TikHubNetworkError(TikHubError):
    """Timeout or transport failure, after the retry budget ran out."""


class TikHubResponseError(TikHubError):
    """HTTP 200 whose body reports a failure code."""


class TikHubClient:
    """Async TikHub REST client.

    Use as an async context manager so the connection pool is always closed::

        async with TikHubClient(settings) as client:
            payload = await client.get_json("/api/v1/weibo/web_v2/fetch_hot_search")
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.tikhub_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.tikhub_timeout_seconds),
            headers={
                "Authorization": f"Bearer {settings.tikhub_api_key}",
                "Accept": "application/json",
                "User-Agent": "social-hot-ai/0.1 (+phase1)",
            },
        )

    async def __aenter__(self) -> TikHubClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying pool when this client owns it."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ public
    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``path`` and return the parsed body.

        Raises a :class:`TikHubError` subclass on any failure; never returns a
        partial or fabricated payload.
        """
        return await self._request_json("GET", path, params=params)

    async def post_json(
        self,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST ``path`` with a JSON body and return the parsed body.

        Part of the provider's search family is POST-only (the Douyin search
        endpoints declare a request body and no query parameters), so the same retry
        and error taxonomy has to be available for both verbs. Sharing one
        implementation rather than copying it is what keeps the two from drifting.
        """
        return await self._request_json("POST", path, params=params, json_body=json_body)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue one request with bounded retries and a typed failure taxonomy."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = self._settings.tikhub_max_retries + 1
        last_error: TikHubError | None = None

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                if method == "POST":
                    response = await self._client.post(path, params=clean or None, json=json_body)
                else:
                    response = await self._client.get(path, params=clean)
            except httpx.TimeoutException as exc:
                last_error = TikHubNetworkError(
                    f"timeout after {self._settings.tikhub_timeout_seconds}s: {exc}",
                    path=path,
                )
            except httpx.HTTPError as exc:
                last_error = TikHubNetworkError(f"transport failure: {exc}", path=path)
            else:
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.info(
                    "%s %s -> %s in %.0fms (attempt %d/%d)",
                    method,
                    path,
                    response.status_code,
                    elapsed_ms,
                    attempt,
                    attempts,
                )
                if response.status_code < 400:
                    return self._parse_body(path, response)

                last_error = self._error_for_status(path, response)

                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error

                if attempt < attempts:
                    delay = self._retry_delay(attempt, response)
                    logger.warning(
                        "%s returned %d; retrying in %.2fs",
                        path,
                        response.status_code,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise last_error

            # transport-level failure path
            logger.warning("%s failed (%s) on attempt %d/%d", path, last_error, attempt, attempts)
            if attempt < attempts:
                delay = self._retry_delay(attempt, None)
                await asyncio.sleep(delay)
                continue
            raise last_error

        # Unreachable: the loop either returns or raises.
        raise last_error or TikHubError("request failed without a recorded error", path=path)

    # ----------------------------------------------------------------- private
    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        """Exponential backoff with jitter, honouring ``Retry-After``."""
        if response is not None:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    return min(float(header), MAX_RETRY_AFTER_SECONDS)
                except ValueError:
                    pass  # HTTP-date form is not worth parsing here
        base = self._settings.tikhub_backoff_base_seconds * (2 ** (attempt - 1))
        return min(base * (1 + random.random() * 0.25), MAX_RETRY_AFTER_SECONDS)

    def _error_for_status(self, path: str, response: httpx.Response) -> TikHubError:
        """Map an HTTP failure onto the error taxonomy."""
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text[:500]
        message = self._message_from_body(body) or f"HTTP {response.status_code}"
        kwargs = {"status": response.status_code, "path": path, "body": body}
        if response.status_code in (401, 403):
            return TikHubAuthError(
                f"TikHub rejected the API key ({message}). Check TIKHUB_API_KEY.",
                **kwargs,
            )
        if response.status_code == 404:
            return TikHubNotFoundError(f"endpoint not found: {message}", **kwargs)
        if response.status_code == 429:
            return TikHubRateLimitError(f"rate limited: {message}", **kwargs)
        if response.status_code >= 500:
            return TikHubServerError(f"provider error: {message}", **kwargs)
        return TikHubError(f"request failed: {message}", **kwargs)

    @staticmethod
    def _message_from_body(body: Any) -> str | None:
        if isinstance(body, dict):
            for key in ("message_zh", "message", "detail", "error"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _parse_body(self, path: str, response: httpx.Response) -> dict[str, Any]:
        """Decode a 2xx body and reject a body-level failure envelope."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise TikHubResponseError(
                f"response was not JSON: {exc}",
                status=response.status_code,
                path=path,
                body=response.text[:500],
            ) from exc
        if not isinstance(payload, dict):
            raise TikHubResponseError(
                "response body was not a JSON object",
                status=response.status_code,
                path=path,
                body=payload,
            )
        code = payload.get("code")
        if isinstance(code, int) and code >= 400:
            raise TikHubResponseError(
                f"body reported code {code}: {self._message_from_body(payload)}",
                status=response.status_code,
                path=path,
                body=payload,
            )
        return payload

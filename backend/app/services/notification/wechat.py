"""WeChat delivery through a group robot webhook (official API only).

This is the simplest legitimate WeChat route: a 企业微信 group robot webhook URL
(``https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…``). No personal account
automation, no protocol reverse engineering — the spec forbids both (§24).

Two provider details are handled rather than assumed:

* The robot replies ``{"errcode": 0, "errmsg": "ok"}`` with **HTTP 200 even on
  failure**, so the body's ``errcode`` — not the status code — decides success.
* Messages have a byte ceiling, so the content is split first (§28) and each part
  is sent separately; a later part failing is reported without hiding the earlier
  successes.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.services.notification.base import (
    DEFAULT_MAX_CHARS,
    NotificationChannel,
    SendResult,
    split_message,
)

logger = logging.getLogger(__name__)

#: WeChat group robots accept text and markdown; markdown renders the digest better.
SUPPORTED_MSGTYPES = ("markdown", "text")


class WeChatWebhookChannel(NotificationChannel):
    """Posts to a 企业微信 group robot webhook."""

    name = "wechat"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client
        self._max_chars = max_chars
        self._msgtype = (
            settings.wechat_msgtype
            if settings.wechat_msgtype in SUPPORTED_MSGTYPES
            else "markdown"
        )

    def configured(self) -> bool:
        return bool(self._settings.wechat_enabled and self._settings.wechat_webhook_url.strip())

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        described["msgtype"] = self._msgtype
        url = self._settings.wechat_webhook_url
        # Never echo the webhook key: it is the credential.
        described["webhook_host"] = url.split("?")[0] if url else ""
        return described

    def _body(self, content: str) -> dict[str, Any]:
        if self._msgtype == "markdown":
            return {"msgtype": "markdown", "markdown": {"content": content}}
        return {"msgtype": "text", "text": {"content": content}}

    async def send(self, title: str, content: str) -> SendResult:
        """Send the digest, splitting it when it is too long."""
        if not self.configured():
            return SendResult(
                channel=self.name,
                ok=False,
                error="wechat is not configured (WECHAT_ENABLED / WECHAT_WEBHOOK_URL)",
            )

        parts = split_message(f"{title}\n\n{content}", self._max_chars)
        if not parts:
            return SendResult(channel=self.name, ok=False, error="nothing to send")

        client = self._client or httpx.AsyncClient(timeout=20.0)
        sent = 0
        try:
            for index, part in enumerate(parts, start=1):
                try:
                    response = await client.post(
                        self._settings.wechat_webhook_url, json=self._body(part)
                    )
                except httpx.HTTPError as exc:
                    return SendResult(
                        channel=self.name,
                        ok=False,
                        parts=sent,
                        error=f"part {index}/{len(parts)} transport failure: {exc}",
                    )
                payload: Any
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                errcode = payload.get("errcode") if isinstance(payload, dict) else None
                if response.status_code >= 400 or errcode not in (0, None):
                    return SendResult(
                        channel=self.name,
                        ok=False,
                        parts=sent,
                        status_code=response.status_code,
                        error=(
                            f"part {index}/{len(parts)} rejected: errcode={errcode} "
                            f"errmsg={payload.get('errmsg') if isinstance(payload, dict) else response.text[:120]}"
                        ),
                    )
                sent += 1
        finally:
            if self._owns_client:
                await client.aclose()

        return SendResult(
            channel=self.name,
            ok=True,
            parts=sent,
            status_code=200,
            detail={"msgtype": self._msgtype},
        )


__all__ = ["SUPPORTED_MSGTYPES", "WeChatWebhookChannel"]

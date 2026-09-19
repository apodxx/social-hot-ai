"""QQ delivery through the official QQ Bot API (开放平台 v2).

Only the official API is used — an app id + client secret exchanged for an app
access token, then a message posted to a channel or a single user. No personal
account automation, no protocol reverse engineering (§24 forbids both).

**This channel is written but unverified.** Verifying it needs real QQ Bot
credentials (app id, secret, and a channel or an openid that has interacted with
the bot), which the project does not have. Everything below follows the published
API shape — ``POST /app/getAppAccessToken``, ``Authorization: QQBot <token>``,
``/v2/users/{openid}/messages`` and ``/channels/{channel_id}/messages`` — but until
it is exercised against the live API it must be treated as untested, and the
status endpoint reports it as configured-but-unverified rather than healthy.

Conservative choices where the published limits are uncertain:

* the per-message ceiling is 800 characters (well under the documented limit,
  because the exact bound varies by message kind);
* the access token is refreshed 60 seconds before its stated expiry;
* a failure in any part is reported without retrying, so a wrong credential
  cannot turn into a request storm.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.services.notification.base import NotificationChannel, SendResult, split_message
from app.services.notification.qq_media import MediaUploadResult

logger = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
PRODUCTION_BASE = "https://api.sgroup.qq.com"
SANDBOX_BASE = "https://sandbox.api.sgroup.qq.com"

#: Deliberately below the documented ceiling; see the module docstring.
QQ_MAX_CHARS = 800

#: Refresh the token this many seconds before it expires.
TOKEN_SAFETY_MARGIN_SECONDS = 60


class QQBotChannel(NotificationChannel):
    """Posts to a QQ channel or a single user through the official Bot API."""

    name = "qq"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------- config
    @property
    def base_url(self) -> str:
        return SANDBOX_BASE if self._settings.qq_sandbox else PRODUCTION_BASE

    @property
    def target(self) -> tuple[str, str] | None:
        """``(kind, id)`` — **group first**, then single user, then channel.

        Group comes first because it is the only scene the image path supports end to
        end, and because a channel cannot be posted to over plain HTTP at all (see
        :meth:`_endpoint`).
        """
        group = self._settings.qq_group_openid.strip()
        if group:
            return "group", group
        openid = self._settings.qq_target_openid.strip()
        if openid:
            return "user", openid
        channel = self._settings.qq_channel_id.strip()
        if channel:
            return "channel", channel
        return None

    def configured(self) -> bool:
        target = self.target
        return bool(
            self._settings.qq_enabled
            and self._settings.qq_app_id.strip()
            and self._settings.qq_app_secret.strip()
            and target is not None
        )

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        target = self.target
        described.update(
            {
                "base_url": self.base_url,
                "target_kind": target[0] if target else None,
                "verified": False,
                "note": (
                    "written against the published QQ Bot API but never exercised: "
                    "no credentials were available to verify it"
                ),
            }
        )
        return described

    # -------------------------------------------------------------- sending
    async def _access_token(self, client: httpx.AsyncClient) -> str:
        """Fetch (and cache) the app access token."""
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token
        response = await client.post(
            TOKEN_URL,
            json={
                "appId": self._settings.qq_app_id,
                "clientSecret": self._settings.qq_app_secret,
            },
        )
        payload: Any
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"token endpoint returned non-JSON: {exc}") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if response.status_code >= 400 or not token:
            raise RuntimeError(
                f"token request failed: HTTP {response.status_code} "
                f"{str(payload)[:160]}"
            )
        expires_in = float(payload.get("expires_in") or 7200)
        self._token = str(token)
        self._token_expires_at = now + max(60.0, expires_in - TOKEN_SAFETY_MARGIN_SECONDS)
        return self._token

    def _endpoint(self) -> str | None:
        target = self.target
        if target is None:
            return None
        kind, identifier = target
        if kind == "group":
            return f"{self.base_url}/v2/groups/{identifier}/messages"
        if kind == "user":
            return f"{self.base_url}/v2/users/{identifier}/messages"
        # 文字子频道：官方文档写明发消息要求机器人**保持 WebSocket 在线**，而这个通道
        # 只做 HTTP，所以这条路发不出去。返回 None 让调用方看到明确的失败，而不是
        # 发一个注定被拒的请求。
        return None

    async def send_rich(
        self,
        title: str,
        content: str,
        image_paths: list[str],
        *,
        passive_id: str = "",
        start_seq: int = 0,
        media_cache: dict[str, tuple[str, float]] | None = None,
    ) -> SendResult:
        """发送一条文本 + 若干张图片。

        **每张图是一条独立的 QQ 消息**：富媒体接口一次只能携带一个 ``file_info``。
        所以"一条推送"实际是 1 条文本 + N 条图片消息，返回里如实报告条数。

        ``passive_id`` 是**被动回复**用的 ``msg_id``（来自 ``GROUP_AT_MESSAGE_CREATE``）。
        这一步是必需的而不是优化：主动消息需要单独权限，没有权限时官方返回
        ``40034105 主动消息失败, 无权限``——而带上 ``msg_id`` 的回复杂用被动额度，
        群聊里 5 分钟内最多回 5 条。界面/脚本因此必须先收到一次 @。

        ``start_seq`` 是**已用掉的序号**。回复同一个 ``msg_id`` 的多条消息共享一个
        序号空间，所以第二部分的序号必须接着第一部分往下数——否则官方报
        ``40054005 消息被去重``（真实踩到过：第二部分又从 1 开始，撞上了第一部分的文本）。
        """
        if not self.configured():
            return await self.send(title, content)  # 复用同一套未配置报错
        target = self.target
        if target is None or target[0] != "group":
            return SendResult(
                channel=self.name,
                ok=False,
                error=(
                    "图片推送只支持 QQ 群："
                    "单聊富媒体和群聊的接口不互通，而文字子频道要求机器人常驻 WebSocket，"
                    "HTTP 方式发不出去。请配置 QQ_GROUP_OPENID。"
                ),
            )

        from pathlib import Path

        from app.services.notification.qq_media import check_image, upload_group_image

        group_openid = target[1]
        settings = self._settings
        root = settings.media_root_path.parent

        client = self._client or httpx.AsyncClient(
            base_url=self.base_url, timeout=settings.qq_upload_timeout_seconds
        )
        uploaded = 0
        images_sent = 0
        skipped: list[str] = []
        try:
            try:
                token = await self._access_token(client)
            except (httpx.HTTPError, RuntimeError) as exc:
                return SendResult(channel=self.name, ok=False, error=f"token: {exc}")

            headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}

            # --- 文本 ---
            endpoint = self._endpoint()
            assert endpoint is not None  # target[0] == "group" already checked
            text_result = await self._post_text(
                client,
                endpoint,
                headers,
                title,
                content,
                passive_id=passive_id,
                start_seq=start_seq,
            )
            if not text_result.ok:
                return text_result

            # --- 图片：一张一条消息，序号接着文本往下数 ---
            sequence = start_seq + text_result.parts
            for relative in image_paths:
                sequence += 1
                path = Path(relative)
                if not path.is_absolute():
                    path = root / relative
                from app.services.notification.qq_media import (
                    CONVERTED_SUBDIR,
                    QQMediaError,
                    check_image,
                    ensure_sendable,
                    upload_group_image,
                )

                try:
                    # webp -> jpg：不做这一步，素材库里 370/453 张图都发不出去。
                    sendable = ensure_sendable(
                        path, cache_dir=settings.media_root_path / CONVERTED_SUBDIR
                    )
                    check_image(sendable)
                except QQMediaError as exc:
                    # 一张图不合格不该让整条推送失败：记录原因，继续发其余部分。
                    skipped.append(f"{path.name}：{exc}")
                    continue

                media = None
                cached = (media_cache or {}).get(relative)
                if cached and cached[1] > time.time():
                    # ``file_info`` 在 ttl 内可重复用于多条消息，所以同一张图
                    # （三个平台都带）只上传一次。不然 4 部分 × 6 张 = 24 次上传。
                    media = MediaUploadResult(
                        ok=True, file_info=cached[0], file_name=path.name
                    )
                if media is None:
                    media = await upload_group_image(
                        client, group_openid=group_openid, path=sendable, headers=headers
                    )
                    if media.ok and media_cache is not None:
                        # 提前 30 秒过期，避免边界上用一个刚好失效的 file_info。
                        ttl = media.ttl or 300
                        media_cache[relative] = (media.file_info, time.time() + max(0, ttl - 30))
                if not media.ok:
                    skipped.append(f"{path.name}：{media.error}")
                    continue
                uploaded += 1

                try:
                    body: dict[str, Any] = {
                        "msg_type": 7,
                        "media": {"file_info": media.file_info},
                        "msg_seq": sequence,
                    }
                    if passive_id:
                        body["msg_id"] = passive_id
                    response = await client.post(endpoint, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    skipped.append(f"{path.name}：发送失败 {type(exc).__name__}: {exc}")
                    continue
                if response.status_code >= 400:
                    skipped.append(f"{path.name}：发送被拒 HTTP {response.status_code}")
                    continue
                images_sent += 1
        finally:
            if self._owns_client:
                await client.aclose()

        return SendResult(
            channel=self.name,
            ok=True,
            parts=1 + images_sent,
            status_code=200,
            detail={
                "target_kind": "group",
                "text_parts": 1,
                "images_uploaded": uploaded,
                "images_sent": images_sent,
                "skipped": skipped,
                "note": "每张图片是一条独立的 QQ 消息（富媒体接口一次只带一个 file_info）",
            },
        )

    async def _post_text(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        headers: dict[str, str],
        title: str,
        content: str,
        *,
        passive_id: str = "",
        start_seq: int = 0,
    ) -> SendResult:
        """发文本，必要时分条。抽出来是为了让 send() 与 send_rich() 共用一套错误处理。

        ``passive_id``（群消息的 ``msg_id``）一带上，这次发送就走**被动回复**额度，
        不再受"主动消息无权限"限制；代价是 5 分钟内最多 5 条。
        ``start_seq`` 让调用方把序号接着上一部分继续，避免"消息被去重"。
        """
        parts = split_message(f"{title}\n\n{content}", QQ_MAX_CHARS)
        if not parts:
            return SendResult(channel=self.name, ok=False, error="nothing to send")
        sent = 0
        for index, part in enumerate(parts, start=1):
            body: dict[str, Any] = {
                "content": part,
                "msg_type": 0,
                "msg_seq": start_seq + index,
            }
            if passive_id:
                body["msg_id"] = passive_id
            try:
                response = await client.post(endpoint, json=body, headers=headers)
            except httpx.HTTPError as exc:
                return SendResult(
                    channel=self.name,
                    ok=False,
                    parts=sent,
                    error=f"part {index}/{len(parts)} transport failure: {exc}",
                )
            if response.status_code >= 400:
                from app.services.notification.qq_media import _explain

                return SendResult(
                    channel=self.name,
                    ok=False,
                    parts=sent,
                    status_code=response.status_code,
                    error=f"part {index}/{len(parts)} rejected: {_explain(response)}",
                )
            sent += 1
        return SendResult(channel=self.name, ok=True, parts=sent, status_code=200)

    async def send(self, title: str, content: str) -> SendResult:
        """Send the digest, splitting it when it is too long."""
        if not self.configured():
            missing = []
            if not self._settings.qq_enabled:
                missing.append("QQ_ENABLED")
            if not self._settings.qq_app_id.strip():
                missing.append("QQ_APP_ID")
            if not self._settings.qq_app_secret.strip():
                missing.append("QQ_APP_SECRET")
            if self.target is None:
                missing.append("QQ_GROUP_OPENID 或 QQ_TARGET_OPENID")
            return SendResult(
                channel=self.name, ok=False, error=f"qq is not configured: missing {', '.join(missing)}"
            )

        endpoint = self._endpoint()
        if endpoint is None:
            # 只可能是配了 QQ_CHANNEL_ID：频道发消息要求 WebSocket 在线。
            return SendResult(
                channel=self.name,
                ok=False,
                error=(
                    "文字子频道无法用 HTTP 发送：官方要求机器人保持 WebSocket 在线。"
                    "请改用 QQ_GROUP_OPENID（群聊）或 QQ_TARGET_OPENID（单聊）。"
                ),
            )

        client = self._client or httpx.AsyncClient(
            base_url=self.base_url, timeout=self._settings.qq_upload_timeout_seconds
        )
        try:
            try:
                token = await self._access_token(client)
            except (httpx.HTTPError, RuntimeError) as exc:
                return SendResult(channel=self.name, ok=False, error=f"token: {exc}")
            headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
            return await self._post_text(client, endpoint, headers, title, content)
        finally:
            if self._owns_client:
                await client.aclose()


__all__ = ["PRODUCTION_BASE", "QQBotChannel", "QQ_MAX_CHARS", "SANDBOX_BASE", "TOKEN_URL"]

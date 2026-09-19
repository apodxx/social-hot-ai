"""``/api/notification`` — Phase 6: delivery status and the test message.

``POST /api/notification/test`` sends the spec's §27 test message. Nothing here
spends AI or TikHub money; it only sends a message through the configured channel.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.notification.manager import NotificationManager
from app.services.notification.push import push_item

logger = logging.getLogger(__name__)
router = APIRouter()


class PushItemRequest(BaseModel):
    """一条热点的图文推送请求。"""

    hot_content_id: int = Field(description="推哪一条热点")
    dry_run: bool = Field(
        default=True,
        description=(
            "**默认 true：只组装并返回将发送的内容，不发送、不花钱。** "
            "设为 false 才会真的发到 QQ 群。"
        ),
    )
    order: str = Field(
        default="rewrite_first",
        description="发送顺序：rewrite_first（二创在前，默认）或 original_first",
    )


@router.get("/notification/status", summary="Configured channels (free)")
async def get_notification_status() -> dict[str, Any]:
    """Channel configuration without echoing any credential."""
    settings = get_settings()
    manager = NotificationManager(settings)
    described = manager.describe()
    described["ready"] = manager.enabled and bool(manager.usable_channels())
    if not described["ready"]:
        reasons: list[str] = []
        if not settings.notification_enabled:
            reasons.append("NOTIFICATION_ENABLED is false")
        if not described["usable"]:
            reasons.append(
                "no channel is usable: configure WECHAT_WEBHOOK_URL or the QQ_* values, "
                "or use NOTIFICATION_CHANNEL=log to verify the path without credentials"
            )
        described["blocked_by"] = reasons
    return {"success": True, "notification": described}


@router.post(
    "/notification/push-item",
    summary="Push one item's original post and rewrite (text + images) to a QQ group",
    description=(
        "Sends **two** pieces: the rewrite's 图文 and the original post's 图文. "
        "`dry_run=true` (the default) only composes them and returns the exact text and "
        "image list, sending nothing. Set `dry_run=false` to actually deliver.\n\n"
        "Note what a 'piece' means: one rich-media message carries a single `file_info`, "
        "so each piece is one text message plus one message per image. Images must be "
        "**local png/jpg** — provider URLs expire, and QQ's rich media only accepts "
        "png/jpg, so downloaded webp files are listed as skipped with the reason."
    ),
)
async def push_item_endpoint(payload: PushItemRequest) -> dict[str, Any]:
    """Compose (and optionally send) the two QQ messages. Free unless dry_run=false."""
    settings = get_settings()
    if not payload.dry_run:
        logger.warning(
            "POST /api/notification/push-item item=%s — REAL send to QQ",
            payload.hot_content_id,
        )
    report = await push_item(
        payload.hot_content_id,
        settings=settings,
        dry_run=payload.dry_run,
        order=payload.order,
    )
    if report.error and not report.parts:
        raise HTTPException(status_code=422, detail=report.error)
    return {"success": report.ok, **report.as_dict()}


@router.post(
    "/notification/test",
    summary="Send the notification test message",
    description=(
        "Sends: `SocialHot AI 通知测试 / 如果你收到这条消息，说明通知模块配置成功。` "
        "It uses whatever channel `NOTIFICATION_CHANNEL` names; with `log` it only "
        "writes to the application log."
    ),
)
async def send_notification_test() -> dict[str, Any]:
    """Send the test message and report each channel's outcome."""
    settings = get_settings()
    if not settings.notification_enabled:
        raise HTTPException(
            status_code=503,
            detail="notifications are disabled: set NOTIFICATION_ENABLED=true",
        )
    manager = NotificationManager(settings)
    if not manager.usable_channels():
        raise HTTPException(
            status_code=503,
            detail=(
                "no configured channel can send: set WECHAT_WEBHOOK_URL (with WECHAT_ENABLED=true) "
                "or the QQ_* values (with QQ_ENABLED=true), or use NOTIFICATION_CHANNEL=log"
            ),
        )
    logger.info("POST /api/notification/test via %s", [c.name for c in manager.usable_channels()])
    outcome = await manager.send_test()
    return {"success": outcome.ok, "outcome": outcome.as_dict()}

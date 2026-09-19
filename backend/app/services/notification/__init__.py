"""Phase 6 notification channels.

Only official delivery APIs are implemented — a 企业微信 group robot webhook and the
QQ 开放平台 Bot API. :class:`~app.services.notification.base.LogChannel` exists so the
whole notification path (rendering, splitting, fallback, the pipeline stage) can be
verified without credentials and without touching any unofficial protocol.
"""

from app.services.notification.base import (
    LogChannel,
    NotificationChannel,
    SendResult,
    split_message,
)
from app.services.notification.manager import (
    NotificationManager,
    NotificationOutcome,
    format_digest,
)
from app.services.notification.qq import QQBotChannel
from app.services.notification.wechat import WeChatWebhookChannel

__all__ = [
    "LogChannel",
    "NotificationChannel",
    "NotificationManager",
    "NotificationOutcome",
    "QQBotChannel",
    "SendResult",
    "WeChatWebhookChannel",
    "format_digest",
    "split_message",
]

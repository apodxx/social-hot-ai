"""Notification primitives: the channel contract and message splitting.

The spec's requirements shape this module:

* §28 — a provider's message length limit must never fail the task, so
  :func:`split_message` cuts a long digest into numbered parts *before* sending.
* §24 — only official channels. :class:`LogChannel` exists so the whole
  notification path can be exercised and verified with **no credentials and no
  risk of hitting a non-official API**; the two real channels are
  :mod:`app.services.notification.wechat` and
  :mod:`app.services.notification.qq`.
* §26 — a send failure is recorded, never raised: the pipeline and the scheduler
  must keep running.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Default per-message ceiling. WeChat's group robot accepts 4096 bytes; staying
#: well under it leaves room for multi-byte Chinese characters.
DEFAULT_MAX_CHARS = 1500


@dataclass
class SendResult:
    """What one channel did with one message."""

    channel: str
    ok: bool
    parts: int = 0
    status_code: int | None = None
    error: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "ok": self.ok,
            "parts": self.parts,
            "status_code": self.status_code,
            "error": self.error,
            "detail": self.detail,
        }


def split_message(text: str, limit: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split ``text`` into parts no longer than ``limit`` characters.

    Paragraph boundaries are preferred; a paragraph that is itself too long is cut
    on the hard limit. Multi-part messages get a ``(i/n)`` marker so a reader can
    tell the notification was truncated rather than think it ended early.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    body = text.strip()
    if not body:
        return []
    if len(body) <= limit:
        return [body]

    # Reserve room for the "(i/n)" marker, whose width depends on the part count.
    marker_room = 12
    budget = max(1, limit - marker_room)

    parts: list[str] = []
    current = ""
    for paragraph in body.split("\n"):
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(paragraph) > budget:
            parts.append(paragraph[:budget])
            paragraph = paragraph[budget:]
        current = paragraph
    if current:
        parts.append(current)

    total = len(parts)
    return [f"（{index}/{total}）\n{part}" for index, part in enumerate(parts, start=1)]


class NotificationChannel(ABC):
    """One delivery target."""

    #: Stable name used in config and in :class:`SendResult`.
    name: str = "channel"

    @abstractmethod
    def configured(self) -> bool:
        """True when this channel has everything it needs to send."""

    @abstractmethod
    async def send(self, title: str, content: str) -> SendResult:
        """Deliver ``title`` + ``content``. Must never raise."""

    def describe(self) -> dict[str, object]:
        """Non-secret configuration summary for the status endpoint."""
        return {"name": self.name, "configured": self.configured()}


class LogChannel(NotificationChannel):
    """Writes the notification to the log.

    Not a stub: it is the channel used to verify the pipeline's ``notify`` stage,
    the message format, and the splitting behaviour without credentials. It
    deliberately does not pretend to deliver anywhere.
    """

    name = "log"

    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self._max_chars = max_chars

    def configured(self) -> bool:
        return True

    async def send(self, title: str, content: str) -> SendResult:
        parts = split_message(content, self._max_chars)
        for index, part in enumerate(parts, start=1):
            logger.info("[notification %d/%d] %s\n%s", index, len(parts), title, part)
        return SendResult(
            channel=self.name,
            ok=True,
            parts=len(parts),
            detail={"note": "logged only; no external delivery"},
        )


__all__ = [
    "DEFAULT_MAX_CHARS",
    "LogChannel",
    "NotificationChannel",
    "SendResult",
    "split_message",
]

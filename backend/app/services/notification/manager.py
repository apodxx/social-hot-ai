"""NotificationManager: pick channels, render the digest, never crash the pipeline.

Section 廿五 fixes the message layout; section 廿六 fixes the failure policy —
"try QQ when WeChat fails, record the error, never let it break the pipeline or
the scheduler". Both are implemented here, and :func:`format_digest` is separated
from sending so the layout can be tested without any channel at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.db.repository import list_rewrites
from app.models.ai_rewrite import RewriteStatus
from app.services.notification.base import LogChannel, NotificationChannel, SendResult
from app.services.notification.qq import QQBotChannel
from app.services.notification.wechat import WeChatWebhookChannel

logger = logging.getLogger(__name__)

RULE = "━" * 14

#: Circled digits for the digest; falls back to plain numbers beyond ten.
DIGITS = "①②③④⑤⑥⑦⑧⑨⑩"


def _marker(index: int) -> str:
    return DIGITS[index] if index < len(DIGITS) else f"{index + 1}."


def format_digest(
    entries: Sequence[Any],
    *,
    max_items: int = 10,
    title: str = "🔥 SocialHot AI 今日热点",
) -> str:
    """Render the digest in the spec's §25 layout.

    Each entry is ``(rewrite, hot_content)``. Everything the spec asks a human
    reviewer to see is included: the original headline, why it is hot, the rewrite
    angle, the generated text, and the source link. Section 十九's verification
    warning and section 二十's status are both carried through — a reviewer must be
    able to tell which items still need fact checking.
    """
    selected = list(entries)[:max_items]
    if not selected:
        return f"{title}\n\n本次没有需要审核的新内容。"

    lines = [title, f"发现 {len(selected)} 条值得关注的内容", RULE]
    for index, (rewrite, item) in enumerate(selected):
        lines.append(f"{_marker(index)} 【{item.platform}】")
        lines.append("原标题：")
        lines.append(item.title or "(无标题)")
        if rewrite.summary:
            lines.append("热点摘要：")
            lines.append(rewrite.summary)
        if rewrite.why_hot:
            lines.append("热点原因：")
            lines.append(rewrite.why_hot)
        if rewrite.angle:
            lines.append("推荐二创方向：")
            lines.append(rewrite.angle)
        if rewrite.xiaohongshu_title or rewrite.xiaohongshu_content:
            lines.append("【小红书】")
            if rewrite.xiaohongshu_title:
                lines.append(f"标题：{rewrite.xiaohongshu_title}")
            if rewrite.xiaohongshu_content:
                lines.append(rewrite.xiaohongshu_content)
            if rewrite.xiaohongshu_hashtags:
                lines.append(" ".join(rewrite.xiaohongshu_hashtags))
        if rewrite.weibo_content:
            lines.append("【微博】")
            if rewrite.weibo_opening:
                lines.append(rewrite.weibo_opening)
            lines.append(rewrite.weibo_content)
        if rewrite.douyin_script:
            lines.append("【抖音脚本】")
            if rewrite.douyin_hook:
                lines.append(f"Hook：{rewrite.douyin_hook}")
            lines.append(rewrite.douyin_script)
            if rewrite.douyin_cta:
                lines.append(f"CTA：{rewrite.douyin_cta}")

        if rewrite.needs_verification:
            lines.append("⚠️ 该内容需要人工核实" + (f"：{rewrite.verification_note}" if rewrite.verification_note else ""))
        if rewrite.status == RewriteStatus.NEEDS_REVIEW.value:
            lines.append("状态：NEEDS_REVIEW（未通过自动审核，请勿直接发布）")
        if rewrite.risk_flags:
            lines.append("风险标记：" + "、".join(rewrite.risk_flags))
        lines.append(f"原始链接：{item.url or '(平台未提供)'}")
        lines.append(f"来源：{item.platform}" + (f" ｜ 作者：{item.author}" if item.author else ""))
        lines.append(RULE)

    lines.append("⚠️ 请人工审核后发布（系统不会自动发布）")
    return "\n".join(lines)


#: 平台 → 搜索页模板。**没有原始链接时用搜索页兜底**，保证每条都能点进去。
#: 实测链接覆盖率：小红书 100%、微博 88%、**抖音只有 45%**（榜单很多是关键词条目、
#: 不是具体帖子，拿不到 url）。只给标题等于没给。
SEARCH_URL_TEMPLATES: dict[str, str] = {
    "douyin": "https://www.douyin.com/search/{q}",
    "weibo": "https://s.weibo.com/weibo?q={q}",
    "xiaohongshu": "https://www.xiaohongshu.com/search_result?keyword={q}",
}
#: 这些域名的地址去掉查询串仍可用，而查询串是几百字符的跟踪参数
#: （实测抖音带 600+ 字符的 mid/u_code/share_sign…）。小红书的 xsec_token 必须保留。
STRIPPABLE_HOSTS = ("iesdouyin.com", "douyin.com", "xiaohongshu.com", "xhslink.cn")


def item_link(item: Any, title: str = "") -> str:
    """挑一个可点的链接；没有原始链接就退回平台搜索页。

    **只接受 http(s)。** 实测源数据里出现过 ``javascript:void(0);`` 这种占位值——
    直接透传会给用户一个点了没反应的"链接"，比不给更糟。
    """
    from urllib.parse import quote, urlparse, urlunparse

    raw = str(getattr(item, "url", "") or "").strip()
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
            host = parsed.netloc.lower()
            if parsed.query:
                # 微博只留 q（搜索词），其余都是跟踪参数；小红书要留 xsec_token。
                if host.endswith("weibo.com"):
                    keep_keys = ("q",)
                elif any(host.endswith(c) for c in STRIPPABLE_HOSTS):
                    keep_keys = ("xsec_token", "xsec_source")
                else:
                    keep_keys = ()
                if keep_keys:
                    kept = [
                        pair
                        for pair in parsed.query.split("&")
                        if pair.split("=", 1)[0] in keep_keys
                    ]
                    raw = urlunparse(parsed._replace(query="&".join(kept)))
        except ValueError:
            pass
        return raw

    # 没有可用链接（缺失或是 javascript: 之类）→ 用平台搜索页兜底。
    template = SEARCH_URL_TEMPLATES.get(str(getattr(item, "platform", "")))
    keyword = (title or str(getattr(item, "title", "") or "")).strip()[:40]
    return template.format(q=quote(keyword)) if template and keyword else ""


def format_digest_compact(
    entries: Sequence[Any],
    *,
    max_items: int = 10,
    title: str = "🔥 SocialHot AI 今日热点",
    summary_chars: int = 60,
) -> str:
    """**速览版摘要**：给早中晚定时推送用，每条一行标题 + 一句摘要 + 链接。

    为什么要两个格式：审核摘要（``format_digest``）带三平台全文、风险标记、原始链接，
    实测 6 条就有 **9,373 字符**——按 QQ 单条 800 字符拆分是 **12 条消息**，早中晚各来一次
    就是 36 条，群里会被刷屏，而且没人是想在手机上读完整长文的。

    速览版一条约 150 字符，10 条约 1,500 字符 = 2 条消息，适合扫一眼再决定点哪个链接。
    **三平台全文与风险标记仍在后台**（`/api/rewrites`），需要细读时去看。
    """
    selected = list(entries)[:max_items]
    if not selected:
        return f"{title}\n\n本轮没有新的热点内容。"

    lines = [title, f"共 {len(selected)} 条 · 点链接看原文", RULE]
    for index, (rewrite, item) in enumerate(selected):
        headline = (rewrite.xiaohongshu_title or item.title or "(无标题)").strip()
        lines.append(f"{_marker(index)} {headline}")

        flags: list[str] = [str(item.platform)]
        if item.hot_value:
            flags.append(f"热度 {item.hot_value:,}" if isinstance(item.hot_value, int) else f"热度 {item.hot_value}")
        if rewrite.needs_verification:
            # 速览也要保留"需核实"这个信号——否则有人会照着标题就去发。
            flags.append("⚠️需核实")
        lines.append("   " + " · ".join(flags))

        # **每条都要有可点的链接**（用户明确要求）。没有原始链接就退回平台搜索页，
        # 否则看到感兴趣的标题也没法点进去看。
        link = item_link(item, headline)
        if link:
            lines.append(f"   {link}")

        # 一句摘要放最后：链接在标题正下方最容易被点到，摘要只是补充。
        gist = (rewrite.summary or item.description or "").strip().replace("\n", " ")
        if gist:
            lines.append(f"   {gist[:summary_chars]}{'…' if len(gist) > summary_chars else ''}")

    lines.append(RULE)
    lines.append("需要看三平台全文与风险标记，请到后台「二创审核」。系统不会自动发布。")
    return "\n".join(lines)


@dataclass
class NotificationOutcome:
    """What the manager did with one message."""

    enabled: bool = False
    ok: bool = False
    results: list[SendResult] = field(default_factory=list)
    rendered_chars: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "rendered_chars": self.rendered_chars,
            "channels_tried": [result.as_dict() for result in self.results],
            "error": self.error,
        }


class NotificationManager:
    """Owns the configured channels and the digest."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        channels: Sequence[NotificationChannel] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._channels: list[NotificationChannel] = list(channels) if channels else self._build()

    def _build(self) -> list[NotificationChannel]:
        names = self._settings.notification_channel_names
        built: list[NotificationChannel] = []
        for name in names:
            if name == "wechat":
                built.append(WeChatWebhookChannel(self._settings))
            elif name == "qq":
                built.append(QQBotChannel(self._settings))
            elif name == "log":
                built.append(LogChannel(self._settings.notification_max_chars))
            else:
                logger.warning("unknown notification channel %r ignored", name)
        return built

    @property
    def enabled(self) -> bool:
        return bool(self._settings.notification_enabled and self._channels)

    def usable_channels(self) -> list[NotificationChannel]:
        """Channels that can actually send right now."""
        return [channel for channel in self._channels if channel.configured()]

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self._settings.notification_enabled,
            "configured_channels": self._settings.notification_channel_names,
            "channels": [channel.describe() for channel in self._channels],
            "usable": [channel.name for channel in self.usable_channels()],
            "max_chars": self._settings.notification_max_chars,
        }

    async def send(self, title: str, content: str) -> NotificationOutcome:
        """Deliver one message, trying each usable channel in order (§26).

        Never raises: a delivery problem is data, not an exception, because the
        caller is a pipeline stage that must not fail.
        """
        outcome = NotificationOutcome(enabled=self.enabled, rendered_chars=len(content))
        if not self.enabled:
            outcome.error = "notifications are disabled (NOTIFICATION_ENABLED)"
            return outcome

        for channel in self._channels:
            if not channel.configured():
                logger.warning("notification channel %s is not configured; skipping", channel.name)
                continue
            try:
                result = await channel.send(title, content)
            except Exception as exc:  # noqa: BLE001 - a channel must not break the run
                result = SendResult(channel=channel.name, ok=False, error=f"{type(exc).__name__}: {exc}")
                logger.error("notification channel %s raised: %s", channel.name, exc)
            outcome.results.append(result)
            if result.ok:
                outcome.ok = True
                logger.info("notification delivered via %s in %d part(s)", channel.name, result.parts)
                return outcome
            logger.warning("notification via %s failed: %s", channel.name, result.error)

        outcome.error = "every configured channel failed"
        return outcome

    async def notify_rewrites(
        self,
        entries: Sequence[tuple[Any, Any]] | None = None,
        *,
        content_ids: Sequence[int] | None = None,
        max_items: int | None = None,
        style: str = "compact",
    ) -> NotificationOutcome:
        """Render and send the digest for the rewrites of this run.

        ``content_ids`` comes from the rewrite stage's report, so the notification
        covers what this run produced. When it is absent (a manual ``notify`` run),
        the most recent rewrites are used instead — a fallback that is documented
        rather than silent.

        ``style`` 决定用哪个格式：``"compact"``（默认，速览）或 ``"full"``（审核摘要）。
        **默认是速览**：定时推送一天三次，用完整摘要实测是 12 条消息/次，群里会被刷屏。
        需要细读时去后台「二创审核」，那里的信息一点没少。
        """
        limit = max_items or self._settings.notification_max_items
        if entries is None:
            entries = await self._load_entries(content_ids, limit)
        if not entries:
            return await self.send(
                "SocialHot AI 通知", "本次运行没有产生需要审核的二创内容。"
            )
        if style == "full":
            digest = format_digest(entries, max_items=limit)
        else:
            digest = format_digest_compact(entries, max_items=limit)
        return await self.send("SocialHot AI 今日热点", digest)

    async def _load_entries(
        self, content_ids: Sequence[int] | None, limit: int
    ) -> list[tuple[Any, Any]]:
        """Load the rewrites to notify about."""
        async with session_scope(self._settings) as session:
            rows, _total = await list_rewrites(session, limit=limit)
        if content_ids:
            wanted = set(content_ids)
            filtered = [(rewrite, item) for rewrite, item in rows if rewrite.hot_content_id in wanted]
            if filtered:
                return filtered
            logger.info("notify fallback: none of the run's rewrites were found by id; using the latest")
        return rows

    async def send_test(self) -> NotificationOutcome:
        """The §27 test message."""
        content = (
            "SocialHot AI 通知测试\n\n"
            "如果你收到这条消息，\n说明通知模块配置成功。\n\n"
            f"通道：{', '.join(self._settings.notification_channel_names) or '(none)'}"
        )
        return await self.send("SocialHot AI 通知测试", content)


__all__ = [
    "NotificationManager",
    "NotificationOutcome",
    "RULE",
    "format_digest",
    "format_digest_compact",
]

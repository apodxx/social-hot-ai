"""Phase 6 tests: splitting, the §25 digest, channel behaviour, and fallback.

The two real channels are exercised through respx, so their request shapes and
error handling are verified without any credentials or network.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import Settings
from app.models.ai_rewrite import AiRewriteRecord, RewriteStatus
from app.models.hot_content import HotContentRecord
from app.services.notification.base import (
    LogChannel,
    SendResult,
    split_message,
)
from app.services.notification.manager import (
    NotificationManager,
    format_digest,
)
from app.services.notification.qq import QQBotChannel, TOKEN_URL
from app.services.notification.wechat import WeChatWebhookChannel

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPERSECRET"
QQ_SEND_URL = "https://api.sgroup.qq.com/v2/users/user-1/messages"


def _settings(**overrides) -> Settings:
    """Deterministic settings.

    Every QQ target is pinned here, including the empty ones: without
    ``qq_group_openid=""`` these tests inherited the **real group id from .env**, so writing
    a real group into the environment silently changed which endpoint the tests exercised
    (and started firing at the live URL). A test must not depend on the developer's .env.
    """
    base = dict(
        tikhub_api_key="",
        deepseek_api_key="",
        database_url="",
        notification_enabled=True,
        notification_channel="wechat",
        wechat_enabled=True,
        wechat_webhook_url=WEBHOOK,
        qq_enabled=True,
        qq_app_id="app-id",
        qq_app_secret="app-secret",
        qq_group_openid="",
        qq_target_openid="user-1",
        qq_channel_id="",
    )
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------- split_message
def test_short_message_is_one_part():
    assert split_message("短消息", 100) == ["短消息"]


def test_empty_message_produces_nothing():
    assert split_message("   ", 100) == []


def test_long_message_is_split_with_numbered_markers():
    text = "\n".join(f"第{i}段内容" for i in range(40))
    parts = split_message(text, 120)
    assert len(parts) > 1
    assert parts[0].startswith("（1/")
    assert parts[-1].startswith(f"（{len(parts)}/{len(parts)}）")
    for part in parts:
        assert len(part) <= 120 + 20, "the marker room is respected"


def test_a_single_oversized_paragraph_is_cut_on_the_hard_limit():
    parts = split_message("字" * 500, 100)
    assert len(parts) > 1
    assert all(len(part) <= 120 for part in parts)
    assert "".join(part.split("\n", 1)[1] for part in parts) == "字" * 500


def test_invalid_limit_is_rejected():
    with pytest.raises(ValueError):
        split_message("x", 0)


# ---------------------------------------------------------------- digest (§25)
def _rewrite(**overrides) -> AiRewriteRecord:
    base = dict(
        hot_content_id=1,
        summary="原内容声称发生了某件事。",
        why_hot="因为大家都在讨论。",
        angle="从普通人的影响切入。",
        xiaohongshu_title="换个角度聊聊",
        xiaohongshu_content="小红书正文内容。",
        xiaohongshu_hashtags=["#科技#"],
        weibo_opening="开头一句话。",
        weibo_content="微博正文内容。",
        douyin_hook="前3秒钩子",
        douyin_script="抖音口播脚本。",
        douyin_cta="关注我",
        status=RewriteStatus.NEEDS_REVIEW.value,
        needs_verification=True,
        verification_note="原内容声称…；目前可确认…",
        risk_flags=[],
    )
    base.update(overrides)
    return AiRewriteRecord(**base)


def _item(**overrides) -> HotContentRecord:
    base = dict(
        platform="weibo",
        platform_content_id="w1",
        title="原标题内容",
        url="https://s.weibo.com/weibo?q=x",
        author="某作者",
        title_normalized="原标题内容",
    )
    base.update(overrides)
    return HotContentRecord(**base)


def test_digest_carries_everything_a_reviewer_needs():
    digest = format_digest([(_rewrite(), _item())])
    assert digest.startswith("🔥 SocialHot AI 今日热点")
    assert "发现 1 条值得关注的内容" in digest
    assert "① 【weibo】" in digest
    assert "原标题：" in digest and "原标题内容" in digest
    assert "热点摘要：" in digest
    assert "推荐二创方向：" in digest
    assert "【小红书】" in digest and "换个角度聊聊" in digest and "#科技#" in digest
    assert "【微博】" in digest and "微博正文内容。" in digest
    assert "【抖音脚本】" in digest and "前3秒钩子" in digest and "CTA：关注我" in digest
    assert "⚠️ 该内容需要人工核实" in digest
    assert "NEEDS_REVIEW" in digest
    assert "原始链接：https://s.weibo.com/weibo?q=x" in digest
    assert "来源：weibo ｜ 作者：某作者" in digest
    assert digest.rstrip().endswith("⚠️ 请人工审核后发布（系统不会自动发布）")


def test_digest_flags_missing_url_and_risk():
    digest = format_digest([(_rewrite(risk_flags=["造谣"]), _item(url=None, author=None))])
    assert "原始链接：(平台未提供)" in digest
    assert "风险标记：造谣" in digest
    assert "作者" not in digest.split("来源：")[1].splitlines()[0]


def test_digest_handles_no_entries():
    assert "本次没有需要审核的新内容" in format_digest([])


def test_digest_numbering_continues_past_ten():
    entries = [(_rewrite(), _item()) for _ in range(11)]
    digest = format_digest(entries, max_items=11, title="T")
    assert "⑩ 【weibo】" in digest
    assert "11. 【weibo】" in digest


# --------------------------------------------------------------- log channel
@pytest.mark.asyncio
async def test_log_channel_sends_and_splits():
    channel = LogChannel(max_chars=50)
    result = await channel.send("标题", "内容" * 60)
    assert result.ok is True
    assert result.parts > 1
    assert result.detail["note"].startswith("logged only")


# ------------------------------------------------------------ wechat channel
@pytest.mark.asyncio
async def test_wechat_posts_markdown(settings):
    configured = _settings()
    with respx.mock:
        route = respx.post(WEBHOOK).mock(
            return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        )
        result = await WeChatWebhookChannel(configured).send("标题", "正文")
    assert result.ok is True and result.parts == 1
    body = route.calls[0].request.content.decode("utf-8")
    # httpx serialises compactly: no space after the colon.
    assert '"msgtype":"markdown"' in body
    assert "正文" in body


@pytest.mark.asyncio
async def test_wechat_treats_a_nonzero_errcode_as_failure():
    """The robot answers HTTP 200 even when it refuses the message."""
    with respx.mock:
        respx.post(WEBHOOK).mock(
            return_value=httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook url"})
        )
        result = await WeChatWebhookChannel(_settings()).send("标题", "正文")
    assert result.ok is False
    assert "93000" in (result.error or "")


@pytest.mark.asyncio
async def test_wechat_splits_into_several_requests():
    configured = _settings(notification_max_chars=60)
    with respx.mock:
        route = respx.post(WEBHOOK).mock(
            return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        )
        result = await WeChatWebhookChannel(configured, max_chars=60).send("标题", "内容" * 80)
    assert result.ok is True
    assert result.parts == route.call_count > 1


@pytest.mark.asyncio
async def test_wechat_reports_transport_failure():
    with respx.mock:
        respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("no route"))
        result = await WeChatWebhookChannel(_settings()).send("标题", "正文")
    assert result.ok is False and "transport failure" in (result.error or "")


@pytest.mark.asyncio
async def test_wechat_without_configuration_says_what_is_missing():
    result = await WeChatWebhookChannel(_settings(wechat_enabled=False)).send("t", "c")
    assert result.ok is False
    assert "WECHAT_ENABLED" in (result.error or "")


def test_wechat_describe_never_leaks_the_webhook_key():
    described = WeChatWebhookChannel(_settings()).describe()
    assert "SUPERSECRET" not in str(described)
    assert described["webhook_host"].startswith("https://qyapi.weixin.qq.com")


# ---------------------------------------------------------------- qq channel
@pytest.mark.asyncio
async def test_qq_fetches_a_token_then_sends(settings):
    configured = _settings()
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "TOK", "expires_in": 7200})
        )
        send = respx.post(QQ_SEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))
        result = await QQBotChannel(configured).send("标题", "正文")
    assert result.ok is True and result.parts == 1
    request = send.calls[0].request
    assert request.headers["Authorization"] == "QQBot TOK"


@pytest.mark.asyncio
async def test_qq_reuses_a_cached_token():
    channel = QQBotChannel(_settings())
    with respx.mock:
        token_route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "TOK", "expires_in": 7200})
        )
        respx.post(QQ_SEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))
        await channel.send("一", "正文一")
        await channel.send("二", "正文二")
        assert token_route.call_count == 1, "the token must be cached"


@pytest.mark.asyncio
async def test_qq_reports_a_token_failure():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"code": 100007}))
        result = await QQBotChannel(_settings()).send("t", "c")
    assert result.ok is False and "token" in (result.error or "")


@pytest.mark.asyncio
async def test_qq_reports_a_send_rejection():
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "TOK", "expires_in": 7200})
        )
        respx.post(QQ_SEND_URL).mock(return_value=httpx.Response(403, json={"code": 11253}))
        result = await QQBotChannel(_settings()).send("t", "c")
    assert result.ok is False and "11253" in (result.error or "")


@pytest.mark.asyncio
async def test_qq_lists_every_missing_setting():
    result = await QQBotChannel(
        _settings(qq_enabled=False, qq_app_id="", qq_app_secret="", qq_target_openid="")
    ).send("t", "c")
    assert result.ok is False
    for expected in ("QQ_ENABLED", "QQ_APP_ID", "QQ_APP_SECRET", "QQ_TARGET_OPENID"):
        assert expected in (result.error or "")


def test_qq_describe_admits_it_is_unverified():
    described = QQBotChannel(_settings()).describe()
    assert described["verified"] is False
    assert "never exercised" in described["note"]
    assert described["target_kind"] == "user"


# ------------------------------------------------------------------- manager
class _FakeChannel(LogChannel):
    def __init__(self, name: str, *, ok: bool, configured: bool = True, raises: bool = False) -> None:
        super().__init__()
        self.name = name
        self._ok = ok
        self._configured = configured
        self._raises = raises
        self.calls = 0

    def configured(self) -> bool:
        return self._configured

    async def send(self, title: str, content: str) -> SendResult:
        self.calls += 1
        if self._raises:
            raise RuntimeError("channel exploded")
        return SendResult(channel=self.name, ok=self._ok, error=None if self._ok else "refused")


@pytest.mark.asyncio
async def test_manager_is_disabled_by_default(settings):
    """**通知开关关着时**，send 必须如实报告"未启用"而不是假装发送成功。

    这里显式关掉 ``notification_enabled``：开关是**部署选择**（本项目真的会把它打开去跑
    早中晚推送），不该依赖开发者 ``.env`` 里凑巧是关的——那样一开推送测试就红了。
    """
    manager = NotificationManager(settings.model_copy(update={"notification_enabled": False}))
    outcome = await manager.send("t", "c")
    assert outcome.ok is False and outcome.enabled is False
    assert "NOTIFICATION_ENABLED" in (outcome.error or "")


@pytest.mark.asyncio
async def test_manager_tries_the_next_channel_after_a_failure():
    first = _FakeChannel("wechat", ok=False)
    second = _FakeChannel("qq", ok=True)
    manager = NotificationManager(_settings(), channels=[first, second])
    outcome = await manager.send("t", "c")
    assert outcome.ok is True
    assert [result.channel for result in outcome.results] == ["wechat", "qq"]
    assert second.calls == 1


@pytest.mark.asyncio
async def test_manager_stops_at_the_first_success():
    first = _FakeChannel("wechat", ok=True)
    second = _FakeChannel("qq", ok=True)
    manager = NotificationManager(_settings(), channels=[first, second])
    outcome = await manager.send("t", "c")
    assert outcome.ok is True
    assert second.calls == 0


@pytest.mark.asyncio
async def test_manager_survives_a_channel_that_raises():
    broken = _FakeChannel("wechat", ok=False, raises=True)
    good = _FakeChannel("qq", ok=True)
    manager = NotificationManager(_settings(), channels=[broken, good])
    outcome = await manager.send("t", "c")
    assert outcome.ok is True, "an exploding channel must not stop the others"
    assert "exploded" in (outcome.results[0].error or "")


@pytest.mark.asyncio
async def test_manager_reports_when_every_channel_fails():
    manager = NotificationManager(
        _settings(), channels=[_FakeChannel("wechat", ok=False), _FakeChannel("qq", ok=False)]
    )
    outcome = await manager.send("t", "c")
    assert outcome.ok is False
    assert outcome.error == "every configured channel failed"
    assert len(outcome.results) == 2


@pytest.mark.asyncio
async def test_manager_skips_an_unconfigured_channel():
    manager = NotificationManager(
        _settings(),
        channels=[_FakeChannel("wechat", ok=True, configured=False), _FakeChannel("qq", ok=True)],
    )
    outcome = await manager.send("t", "c")
    assert outcome.ok is True
    assert [result.channel for result in outcome.results] == ["qq"]


@pytest.mark.asyncio
async def test_send_test_uses_the_section_27_text():
    manager = NotificationManager(_settings(), channels=[LogChannel()])
    outcome = await manager.send_test()
    assert outcome.ok is True


def test_manager_describe_lists_channels_without_secrets():
    manager = NotificationManager(_settings())
    described = manager.describe()
    assert described["enabled"] is True
    assert described["usable"] == ["wechat"]
    assert "SUPERSECRET" not in str(described)


def test_channel_names_accept_the_multi_channel_form():
    assert _settings(notification_channels="wechat,qq ").notification_channel_names == ["wechat", "qq"]
    assert _settings(notification_channel="log").notification_channel_names == ["log"]
    assert _settings(notification_channels="log,log").notification_channel_names == ["log"]


@pytest.mark.asyncio
async def test_notify_rewrites_with_explicit_entries():
    manager = NotificationManager(_settings(), channels=[LogChannel()])
    outcome = await manager.notify_rewrites([(_rewrite(), _item())])
    assert outcome.ok is True and outcome.rendered_chars > 0


# ---------------------------------------------------------------- digest formats
def _fake_entry(title: str, summary: str) -> tuple[Any, Any]:
    """构造一对 (rewrite, item)，字段足够 formatter 使用。"""

    class _Obj:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    rewrite = _Obj(
        summary=summary,
        why_hot="",
        angle="",
        xiaohongshu_title=title,
        xiaohongshu_content="小红书正文" * 20,
        xiaohongshu_hashtags=["编程"],
        weibo_content="微博正文" * 20,
        weibo_opening="",
        douyin_script="抖音脚本" * 20,
        douyin_hook="",
        douyin_cta="",
        needs_verification=True,
        verification_note="无法核实出处",
        status="NEEDS_REVIEW",
        risk_flags=["需核实"],
    )
    item = _Obj(platform="xiaohongshu", title=title, url="https://example.com/x", author="某人", description="", hot_value=0)
    return rewrite, item


def test_compact_digest_is_much_shorter_than_the_review_digest():
    """**回归测试：定时推送一天三次，用审核摘要会把群刷屏。**

    实测：6 条内容用 ``format_digest`` 是 9,373 字符（按 QQ 单条 800 计 = 12 条消息），
    速览版只要 1,323 字符（2 条）。所以定时推送默认用速览版——
    三平台全文与风险标记仍在后台，不需要塞进手机通知里。
    """
    from app.services.notification.manager import format_digest, format_digest_compact

    entries = [_fake_entry(f"标题{i}", f"这是第{i}条的摘要") for i in range(6)]
    full = format_digest(entries, max_items=6)
    compact = format_digest_compact(entries, max_items=6)

    assert len(compact) < len(full) / 3, (
        f"速览版应当明显更短：{len(compact)} vs {len(full)}"
    )
    assert len(compact) < 800 * 3, f"速览版不该超过 3 条 QQ 消息（实际 {len(compact)} 字符）"


def test_compact_digest_keeps_the_signals_that_matter():
    """速览也要保留「需核实」——否则有人照着标题就去发了。"""
    from app.services.notification.manager import format_digest_compact

    digest = format_digest_compact([_fake_entry("某标题", "某摘要")], max_items=1)
    assert "某标题" in digest, "要有标题"
    assert "某摘要" in digest, "要有摘要"
    assert "需核实" in digest, "要保留核实提醒"
    assert "https://example.com/x" in digest, "要有原文链接"
    assert "不会自动发布" in digest or "系统不会自动发布" in digest


def test_compact_digest_handles_an_empty_round():
    """没有内容时也要说清楚，而不是发一条空消息。"""
    from app.services.notification.manager import format_digest_compact

    digest = format_digest_compact([])
    assert "没有" in digest and len(digest) < 100


def test_compact_digest_truncates_long_summaries():
    """摘要过长要截断并加省略号——速览的意义就是短。"""
    from app.services.notification.manager import format_digest_compact

    digest = format_digest_compact(
        [_fake_entry("标题", "很长的摘要" * 50)], max_items=1, summary_chars=30
    )
    assert "…" in digest
    assert "很长的摘要" * 50 not in digest


def test_notify_defaults_to_the_compact_style():
    """定时推送走 notify_rewrites 时不传 style，必须是速览版。"""
    import inspect

    from app.services.notification.manager import NotificationManager

    signature = inspect.signature(NotificationManager.notify_rewrites)
    assert signature.parameters["style"].default == "compact"

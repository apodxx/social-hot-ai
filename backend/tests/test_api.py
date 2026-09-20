"""API tests for ``/api/hot`` and ``/api/system/status``.

The TikHub transport is mocked with ``respx``; no test here spends money.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app

WEIBO_URL = "https://api.tikhub.io/api/v1/weibo/web_v2/fetch_hot_search_summary"
WEIBO_FALLBACK_URL = "https://api.tikhub.io/api/v1/weibo/app/fetch_hot_search"
DOUYIN_URL = "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_hot_search_list"
XHS_URL = "https://api.tikhub.io/api/v1/xiaohongshu/web_v3/fetch_homefeed"
PROBE_URL = "https://api.tikhub.io/api/v1/tikhub/user/get_user_info"
DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"

WEIBO_BODY = {"code": 200, "data": [{"word": "微博热点", "num": "1.2万"}]}
DOUYIN_BODY = {"code": 200, "data": {"word_list": [{"word": "抖音热点", "hot_value": 500}]}}
XHS_BODY = {
    "code": 200,
    "data": {"items": [{"id": "n1", "note_card": {"display_title": "小红书笔记"}}]},
}


@pytest.fixture
async def api(monkeypatch, sqlite_db, tmp_path):
    """A TestClient over the in-memory database, fake keys, and no backoff.

    ``sqlite_db`` installs the engine *before* the app builds its session, so the
    API tests never touch the real PostgreSQL instance. The account profile is a
    temporary file so the tests do not depend on the repository's own copy.
    """
    monkeypatch.setenv("TIKHUB_API_KEY", "test-key")
    monkeypatch.setenv("HOT_LIMIT_PER_PLATFORM", "3")
    # Without this the retry paths really sleep and the suite takes ~30s.
    monkeypatch.setenv("TIKHUB_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    # The API tests must not start a background scheduler.
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # Notifications on, through the log channel: no credentials, no network.
    monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_CHANNEL", "log")
    profile_path = tmp_path / "account_profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "account_name": "测试账号",
                "field": "AI / 科技",
                "target_audience": "18-30岁",
                "style": "通俗",
                "tone": "自然",
                "preferred_topics": ["AI"],
                "forbidden": ["虚假信息"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYSIS_PROFILE_PATH", str(profile_path))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
async def api_without_key(monkeypatch, sqlite_db):
    monkeypatch.setenv("TIKHUB_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_hot_requires_a_key(api_without_key):
    response = api_without_key.get("/api/hot")
    assert response.status_code == 503
    assert "TIKHUB_API_KEY" in response.json()["detail"]


def test_hot_returns_the_acceptance_envelope(api):
    with respx.mock:
        respx.get(WEIBO_URL).mock(return_value=httpx.Response(200, json=WEIBO_BODY))
        respx.get(DOUYIN_URL).mock(return_value=httpx.Response(200, json=DOUYIN_BODY))
        respx.get(XHS_URL).mock(return_value=httpx.Response(200, json=XHS_BODY))
        response = api.get("/api/hot")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert set(body["data"]) == {"xiaohongshu", "weibo", "douyin"}
    assert body["counts"] == {"xiaohongshu": 1, "weibo": 1, "douyin": 1}
    assert body["errors"] == {}
    assert body["limit_per_platform"] == 3

    weibo_item = body["data"]["weibo"][0]
    assert weibo_item["platform"] == "weibo"
    assert weibo_item["rank"] == 1
    assert weibo_item["hot_value"] == 12000
    assert "raw_data" not in weibo_item, "raw_data is opt-in"


def test_hot_can_include_raw_data(api):
    with respx.mock:
        respx.get(WEIBO_URL).mock(return_value=httpx.Response(200, json=WEIBO_BODY))
        respx.get(DOUYIN_URL).mock(return_value=httpx.Response(200, json=DOUYIN_BODY))
        respx.get(XHS_URL).mock(return_value=httpx.Response(200, json=XHS_BODY))
        response = api.get("/api/hot?include_raw=true")
    assert response.json()["data"]["weibo"][0]["raw_data"]["word"] == "微博热点"


def test_one_platform_failure_does_not_hide_the_others(api):
    with respx.mock:
        respx.get(WEIBO_URL).mock(return_value=httpx.Response(500, json={"message": "boom"}))
        respx.get(WEIBO_FALLBACK_URL).mock(return_value=httpx.Response(500, json={"message": "boom"}))
        respx.get(DOUYIN_URL).mock(return_value=httpx.Response(200, json=DOUYIN_BODY))
        respx.get(XHS_URL).mock(return_value=httpx.Response(200, json=XHS_BODY))
        response = api.get("/api/hot")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["weibo"] == []
    assert "weibo" in body["errors"]
    assert body["counts"]["douyin"] == 1 and body["counts"]["xiaohongshu"] == 1


def test_total_failure_is_a_gateway_error(api):
    with respx.mock:
        respx.get(WEIBO_URL).mock(return_value=httpx.Response(500, json={}))
        respx.get(WEIBO_FALLBACK_URL).mock(return_value=httpx.Response(500, json={}))
        respx.get(DOUYIN_URL).mock(return_value=httpx.Response(500, json={}))
        respx.get(DOUYIN_FALLBACK := "https://api.tikhub.io/api/v1/douyin/billboard/fetch_hot_total_list").mock(
            return_value=httpx.Response(500, json={})
        )
        respx.get(XHS_URL).mock(return_value=httpx.Response(500, json={}))
        response = api.get("/api/hot")

    assert response.status_code == 502
    assert set(response.json()["detail"]["errors"]) == {"weibo", "douyin", "xiaohongshu"}


def test_platform_filter_and_unknown_platform(api):
    with respx.mock:
        respx.get(DOUYIN_URL).mock(return_value=httpx.Response(200, json=DOUYIN_BODY))
        response = api.get("/api/hot?platforms=douyin")
    assert response.status_code == 200
    assert set(response.json()["data"]) == {"douyin"}

    assert api.get("/api/hot?platforms=myspace").status_code == 422


def test_status_reports_components(api):
    response = api.get("/api/system/status?probe=false")
    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == 10
    # The database is reachable (in-memory SQLite) and reports its counts.
    assert body["components"]["database"]["status"] == "connected"
    assert "hot_contents" in body["components"]["database"]["detail"]
    # DeepSeek is configured but its probe was skipped: no tokens for a health check.
    # "skipped" is not "not_configured" — the key is present, it just was not probed.
    assert body["components"]["deepseek"]["status"] == "skipped"
    assert "probe skipped" in body["components"]["deepseek"]["detail"]


def test_status_reports_database_not_configured(api_without_key):
    body = api_without_key.get("/api/system/status?probe=false").json()
    assert body["components"]["database"]["status"] == "not_configured"
    assert body["components"]["deepseek"]["status"] == "not_configured"


def _models_body(*ids: str) -> dict:
    return {"data": [{"id": model_id} for model_id in ids]}


def test_status_probes_both_providers(api):
    with respx.mock:
        respx.get(PROBE_URL).mock(return_value=httpx.Response(200, json={"code": 200}))
        respx.get(DEEPSEEK_MODELS_URL).mock(
            return_value=httpx.Response(200, json=_models_body("deepseek-flash", "deepseek-v4-pro"))
        )
        response = api.get("/api/system/status")
    body = response.json()
    assert body["success"] is True
    assert body["components"]["tikhub"]["status"] == "connected"
    assert body["components"]["tikhub"]["latency_ms"] is not None
    assert body["components"]["deepseek"]["status"] == "connected"
    assert "deepseek-flash" in body["components"]["deepseek"]["detail"]


def test_status_flags_a_model_the_account_does_not_have(api):
    with respx.mock:
        respx.get(PROBE_URL).mock(return_value=httpx.Response(200, json={"code": 200}))
        respx.get(DEEPSEEK_MODELS_URL).mock(
            return_value=httpx.Response(200, json=_models_body("deepseek-v4-pro"))
        )
        response = api.get("/api/system/status")
    deepseek = response.json()["components"]["deepseek"]
    assert deepseek["status"] == "error"
    assert "not in the account's model list" in deepseek["detail"]


def test_status_reports_tikhub_error(api):
    with respx.mock:
        respx.get(PROBE_URL).mock(return_value=httpx.Response(401, json={"message": "bad key"}))
        respx.get(DEEPSEEK_MODELS_URL).mock(
            return_value=httpx.Response(200, json=_models_body("deepseek-flash"))
        )
        response = api.get("/api/system/status")
    body = response.json()
    assert body["success"] is False
    assert body["components"]["tikhub"]["status"] == "error"
    # A dead content provider must not make the AI provider look broken.
    assert body["components"]["deepseek"]["status"] == "connected"


# =============================================================================
# Phase 2 endpoints
# =============================================================================


async def _seed(settings, items):
    """Store items through the real pipeline against the in-memory database."""
    from app.db.database import session_scope
    from app.services.pipeline.hot_pipeline import store_items

    async with session_scope(settings) as session:
        return await store_items(items, settings=settings, session=session)


def test_stored_endpoint_is_empty_before_any_collection(api):
    body = api.get("/api/hot/stored").json()
    assert body == {"success": True, "total": 0, "limit": 50, "offset": 0, "items": []}


@pytest.mark.asyncio
async def test_stored_and_topics_endpoints(api, item_factory):
    from app.core.config import get_settings as settings_factory

    settings = settings_factory()
    await _seed(
        settings,
        [
            item_factory("weibo", "w1", "某某重大事件", hot_value=10, rank=1),
            item_factory("douyin", "d1", "某某重大事件", hot_value=20, rank=1),
            item_factory("xiaohongshu", "x1", "无关的话题", hot_value=5, rank=3),
        ],
    )

    stored = api.get("/api/hot/stored?platform=weibo").json()
    assert stored["total"] == 1
    item = stored["items"][0]
    assert item["platform"] == "weibo"
    assert item["topic_group_id"] is not None, "a grouped item exposes its topic"
    assert "raw_data" not in item

    with_raw = api.get("/api/hot/stored?platform=weibo&include_raw=true").json()
    assert with_raw["items"][0]["raw_data"] == {"seed": "w1"}

    topics = api.get("/api/hot/topics").json()
    assert topics["total"] == 1
    topic = topics["items"][0]
    assert topic["topic"] == "某某重大事件"
    assert topic["related_contents"] == 2
    assert topic["platforms"] == ["douyin", "weibo"]
    assert topic["is_cross_platform"] is True
    assert topic["summary"] is None, "Phase 2 does not invent summaries"

    cross = api.get("/api/hot/topics?cross_platform_only=true").json()
    assert cross["total"] == 1

    detail = api.get(f"/api/hot/topics/{topic['id']}").json()
    assert len(detail["members"]) == 2
    assert {member["platform"] for member in detail["members"]} == {"weibo", "douyin"}

    assert api.get("/api/hot/topics/999999").status_code == 404


def test_collect_requires_a_key(api_without_key):
    assert api_without_key.post("/api/hot/collect").status_code == 503


# =============================================================================
# Phase 3 endpoints
# =============================================================================


def test_analysis_endpoints_are_empty_before_any_run(api):
    body = api.get("/api/analysis").json()
    assert body == {"success": True, "total": 0, "limit": 50, "offset": 0, "items": []}
    assert api.get("/api/analysis/999999").status_code == 404


def test_analysis_profile_endpoint_reflects_the_file(api):
    body = api.get("/api/analysis/profile/current").json()
    assert body["is_empty"] is False
    assert body["profile"]["field"] == "AI / 科技"
    assert "测试账号" in body["prompt_block"]


def test_analysis_run_requires_a_key(api_without_key):
    assert api_without_key.post("/api/analysis/run").status_code == 503


# =============================================================================
# Phase 4 endpoints
# =============================================================================


def test_rewrite_endpoints_are_empty_before_any_run(api):
    body = api.get("/api/rewrite").json()
    assert body == {"success": True, "total": 0, "limit": 50, "offset": 0, "items": []}
    assert api.get("/api/rewrite/999999").status_code == 404


def test_rewrite_rejects_an_unknown_status_filter(api):
    response = api.get("/api/rewrite?status=MAYBE")
    assert response.status_code == 422
    assert "unknown status" in response.json()["detail"]


def test_rewrite_run_requires_a_key(api_without_key):
    assert api_without_key.post("/api/rewrite/run").status_code == 503


# =============================================================================
# Phase 5 endpoints
# =============================================================================


def test_schedule_endpoint_reports_configuration(api):
    body = api.get("/api/pipeline/schedule").json()
    schedule = body["schedule"]
    assert schedule["times"] == ["08:00", "12:00", "18:00"]
    assert schedule["running"] is False, "the scheduler is disabled in tests"
    assert schedule["jobs"] == []
    assert schedule["billed_stages"] == ["analyze", "detail", "fetch", "rewrite", "watch"]
    # The recurring cost of the domain-search stage is exposed, not hidden.
    assert isinstance(schedule["watch_billed_calls_per_run"], int)
    # All five stages are implemented as of Phase 6.
    assert schedule["unimplemented_stages"] == []


def test_pipeline_run_executes_the_detail_stage_and_explains_it_is_off(api):
    body = api.post("/api/pipeline/run?stages=detail").json()
    assert body["success"] is True
    assert body["billed_stages_selected"] == ["detail"]
    run = body["run"]
    assert run["status"] == "success"
    assert run["task_id"] is not None
    step = run["steps"][0]
    assert step["name"] == "detail" and step["status"] == "success"
    assert step["detail"]["billed_calls"] == 0
    assert any("DETAIL_FETCH_ENABLED" in error for error in step["detail"]["errors"])

    history = api.get("/api/pipeline/tasks").json()
    assert history["total"] == 1
    assert history["items"][0]["task_type"] == "manual"


def test_pipeline_run_rejects_an_unknown_stage(api):
    response = api.post("/api/pipeline/run?stages=fetch,nonsense")
    assert response.status_code == 422
    assert "unknown stage" in response.json()["detail"]


def test_task_history_rejects_an_unknown_status(api):
    assert api.get("/api/pipeline/tasks?status=BOGUS").status_code == 422


def test_pipeline_run_needs_at_least_one_provider(api_without_key):
    assert api_without_key.post("/api/pipeline/run").status_code == 503


# =============================================================================
# Phase 6 endpoints
# =============================================================================


def test_notification_status_reports_the_log_channel(api):
    body = api.get("/api/notification/status").json()
    notification = body["notification"]
    assert notification["enabled"] is True
    assert notification["usable"] == ["log"]
    assert notification["ready"] is True
    assert "blocked_by" not in notification


def test_notification_test_endpoint_sends_through_the_log_channel(api):
    body = api.post("/api/notification/test").json()
    assert body["success"] is True
    outcome = body["outcome"]
    assert outcome["ok"] is True
    assert outcome["channels_tried"][0]["channel"] == "log"
    assert outcome["channels_tried"][0]["detail"]["note"].startswith("logged only")


def test_notification_test_is_refused_when_disabled(api_without_key, monkeypatch):
    """通知开关关着时，测试接口必须拒绝并说明原因。

    这里显式设 ``NOTIFICATION_ENABLED=false``：它是**部署选择**（本项目会打开它跑早中晚
    推送），不该依赖开发者 ``.env`` 里凑巧是关的——否则一开推送测试就红了。
    """
    monkeypatch.setenv("NOTIFICATION_ENABLED", "false")
    get_settings.cache_clear()
    response = api_without_key.post("/api/notification/test")
    assert response.status_code == 503
    assert "NOTIFICATION_ENABLED" in response.json()["detail"]

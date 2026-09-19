"""Phase 7 API tests: the admin list, the dashboard stats, and the settings page.

None of these touch the network or spend money: they read what is already stored
and edit configuration files. The settings tests **redirect the environment file
into ``tmp_path``** — the deployment's real ``.env`` holds live API keys, and a test
that wrote to it would be an incident, not a test.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import session_scope
from app.db.repository import upsert_items
from app.main import create_app
from app.models.ai_analysis import AiAnalysisRecord
from app.models.ai_rewrite import AiRewriteRecord
from app.models.hot_content import HotContentRecord
from app.services import settings_editor


@pytest.fixture
async def admin(monkeypatch, sqlite_db, tmp_path):
    """A TestClient whose environment file and profile live in ``tmp_path``."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# deployment settings\n"
        "TIKHUB_API_KEY=test-key-secret-value\n"
        "HOT_LIMIT_PER_PLATFORM=40\n"
        "HOT_FETCH_TIMES=08:00,12:00,18:00\n"
        "\n"
        "# a key this module does not know about\n"
        "SOME_OPERATOR_NOTE=leave-me\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_editor, "ENV_PATH", env_file)

    monkeypatch.setenv("TIKHUB_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    profile_path = tmp_path / "account_profile.json"
    profile_path.write_text(
        json.dumps({"account_name": "测试账号", "field": "AI / 科技"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYSIS_PROFILE_PATH", str(profile_path))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client, env_file, profile_path
    get_settings.cache_clear()


async def _seed(item_factory, *, with_analysis: bool = False) -> None:
    from app.services.pipeline.hot_pipeline import store_items

    settings = get_settings()
    async with session_scope(settings) as session:
        await store_items(
            [
                item_factory("weibo", "w1", "被推荐的甲", hot_value=100, rank=1),
                item_factory("douyin", "d1", "没分析的乙", hot_value=50, rank=2),
            ],
            settings=settings,
            session=session,
        )
    if with_analysis:
        async with session_scope(settings) as session:
            stored = {
                row.title: row
                for row in (await session.execute(select(HotContentRecord))).scalars().all()
            }
            session.add(
                AiAnalysisRecord(
                    hot_content_id=stored["被推荐的甲"].id,
                    topic="话题",
                    summary="摘要",
                    why_hot="因为",
                    content_angle="角度",
                    recommended=True,
                    confidence=0.8,
                    selected=True,
                    needs_verification=True,
                )
            )
            session.add(
                AiRewriteRecord(
                    hot_content_id=stored["被推荐的甲"].id,
                    xiaohongshu_title="小红书标题",
                    xiaohongshu_content="正文",
                    status="NEEDS_REVIEW",
                    needs_verification=True,
                )
            )


# ------------------------------------------------------------------ admin list
@pytest.mark.asyncio
async def test_stored_hot_embeds_analysis_and_rewrite(admin, item_factory):
    client, _, _ = admin
    await _seed(item_factory, with_analysis=True)

    response = client.get("/api/hot/stored")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    by_title = {item["title"]: item for item in body["items"]}

    analysed = by_title["被推荐的甲"]
    assert analysed["analysis"]["recommended"] is True
    assert analysed["analysis"]["confidence"] == 0.8
    assert analysed["rewrite"]["status"] == "NEEDS_REVIEW"
    assert analysed["rewrite"]["needs_verification"] is True
    assert "raw_data" not in analysed, "raw_data stays opt-in"

    plain = by_title["没分析的乙"]
    assert plain["analysis"] is None and plain["rewrite"] is None


@pytest.mark.asyncio
async def test_stored_hot_filters(admin, item_factory):
    client, _, _ = admin
    await _seed(item_factory, with_analysis=True)

    assert client.get("/api/hot/stored?platform=douyin").json()["total"] == 1
    assert client.get("/api/hot/stored?q=甲").json()["total"] == 1
    assert client.get("/api/hot/stored?recommended=true").json()["total"] == 1
    assert client.get("/api/hot/stored?selected=true").json()["total"] == 1
    # Not-recommended excludes the unanalysed row: its column is NULL, not False.
    assert client.get("/api/hot/stored?recommended=false").json()["total"] == 0
    assert client.get("/api/hot/stored?limit=1&offset=1").json()["total"] == 2

    paged = client.get("/api/hot/stored?limit=1&offset=1").json()
    assert len(paged["items"]) == 1 and paged["limit"] == 1 and paged["offset"] == 1


# ---------------------------------------------------------------- dashboard §29
@pytest.mark.asyncio
async def test_stats_endpoint_counts_everything(admin, item_factory):
    client, _, _ = admin
    await _seed(item_factory, with_analysis=True)

    stats = client.get("/api/system/stats?today=false").json()["stats"]
    assert stats["total_items"] == 2
    assert stats["by_platform"] == {"weibo": 1, "douyin": 1}
    assert stats["analyses"] == 1
    assert stats["recommended"] == 1
    assert stats["selected"] == 1
    assert stats["needs_verification"] == 1, "analyses the model flagged as unverified"
    assert stats["rewrites"] == 1
    assert stats["ready_to_publish"] == 0
    assert stats["needs_review"] == 1

    # "Today" must not drop rows that were just stored, and must still report the
    # lifetime AI totals (a review backlog is not a same-day question).
    today = client.get("/api/system/stats").json()["stats"]
    assert today["total_items"] == 2
    assert today["analyses"] == 1
    assert today["since"] is not None


# ------------------------------------------------------------------- settings
def test_get_settings_masks_secrets(admin):
    client, _, _ = admin
    body = client.get("/api/settings").json()["settings"]

    assert body["exists"] is True
    groups = body["groups"]
    assert set(groups) >= {"tikhub", "deepseek", "hot", "notification"}

    tikhub = {entry["key"]: entry for entry in groups["tikhub"]}
    secret = tikhub["TIKHUB_API_KEY"]
    assert secret["value"] == "", "a secret is never returned"
    assert secret["masked"] == "test-k…alue", "6 leading and 4 trailing characters"
    assert "test-key-secret-value" not in json.dumps(body), "the full secret must not appear"

    hot = {entry["key"]: entry for entry in groups["hot"]}
    assert hot["HOT_LIMIT_PER_PLATFORM"]["value"] == "40", "read from the env file on disk"
    assert body["editable_keys"]


def test_put_settings_writes_and_preserves_the_rest(admin):
    client, env_file, _ = admin
    response = client.put(
        "/api/settings", json={"values": {"HOT_LIMIT_PER_PLATFORM": "60", "HOT_FETCH_TIMES": "09:30"}}
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["changed"]) == ["HOT_FETCH_TIMES", "HOT_LIMIT_PER_PLATFORM"]
    assert body["restart_required"] is True, "env changes are read at process start"

    text = env_file.read_text(encoding="utf-8")
    assert "HOT_LIMIT_PER_PLATFORM=60" in text
    assert "HOT_FETCH_TIMES=09:30" in text
    assert "# deployment settings" in text, "comments survive"
    assert "SOME_OPERATOR_NOTE=leave-me" in text, "unknown keys survive"
    assert "TIKHUB_API_KEY=test-key-secret-value" in text, "untouched keys keep their value"


def test_put_settings_blank_secret_keeps_the_stored_value(admin):
    client, env_file, _ = admin
    # A blank secret means "leave it": editing a neighbour must not force a retype.
    response = client.put(
        "/api/settings", json={"values": {"TIKHUB_API_KEY": "", "HOT_LIMIT_PER_PLATFORM": "55"}}
    )
    assert response.status_code == 200
    assert response.json()["changed"] == ["HOT_LIMIT_PER_PLATFORM"]
    assert "TIKHUB_API_KEY=test-key-secret-value" in env_file.read_text(encoding="utf-8")


def test_put_settings_replaces_a_secret_that_is_provided(admin):
    client, env_file, _ = admin
    response = client.put("/api/settings", json={"values": {"TIKHUB_API_KEY": "brand-new-key"}})
    assert response.status_code == 200
    text = env_file.read_text(encoding="utf-8")
    assert "TIKHUB_API_KEY=brand-new-key" in text
    assert "test-key-secret-value" not in text


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"NOPE": "1"}, "not editable"),
        ({"ANALYSIS_MAX_SELECTED": "0"}, "invalid"),
        ({"HOT_FETCH_TIMES": "25:00"}, "invalid"),
        ({"HOT_LIMIT_PER_PLATFORM": "abc"}, "invalid"),
        ({"NOTIFICATION_ENABLED": "maybe"}, "invalid"),
    ],
)
def test_put_settings_rejects_bad_input(admin, values, expected):
    client, env_file, _ = admin
    before = env_file.read_text(encoding="utf-8")

    response = client.put("/api/settings", json={"values": values})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any(expected in error for error in errors), errors
    assert env_file.read_text(encoding="utf-8") == before, "a rejected request writes nothing"


def test_put_settings_with_nothing_to_change_is_a_no_op(admin):
    client, env_file, _ = admin
    before = env_file.read_text(encoding="utf-8")
    # Only a blank secret: nothing to write, and no restart should be demanded.
    body = client.put("/api/settings", json={"values": {"DEEPSEEK_API_KEY": ""}}).json()
    assert body["changed"] == [] and body["restart_required"] is False
    assert env_file.read_text(encoding="utf-8") == before


def test_put_settings_appends_a_key_that_is_not_in_the_file(admin):
    client, env_file, _ = admin
    response = client.put("/api/settings", json={"values": {"DETAIL_FETCH_ENABLED": "true"}})
    assert response.status_code == 200
    text = env_file.read_text(encoding="utf-8")
    assert "DETAIL_FETCH_ENABLED=true" in text
    assert "# added by the admin UI" in text


def test_saving_the_profile_takes_effect_without_a_restart(admin):
    client, _, profile_path = admin
    response = client.put(
        "/api/settings/profile",
        json={
            "account_name": "新账号",
            "field": "职场 / 效率",
            "target_audience": "25-35岁",
            "style": "干货",
            "tone": "稳重",
            "preferred_topics": ["效率工具"],
            "forbidden": ["未经核实的信息"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["restart_required"] is False, "the profile is re-read on every run"
    assert body["profile"]["account_name"] == "新账号"
    assert "职场 / 效率" in body["prompt_block"]

    on_disk = json.loads(profile_path.read_text(encoding="utf-8"))
    assert on_disk["account_name"] == "新账号"

    # The cache is cleared, so the next load sees the new file rather than the old
    # one — the point of saving from the UI at all.
    from app.services.ai.account_profile import load_account_profile

    assert load_account_profile(profile_path).account_name == "新账号"

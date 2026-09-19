"""Phase 9: the keyword-search path and the 图文 layout plan.

The search tests replay the captured fixtures through a stub client, so the whole real
path runs — adapter, dedup, storage, media step — with only the provider faked and
nothing billed. Image downloading is exercised separately in ``test_search.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import session_scope
from app.main import create_app
from app.models.hot_content import ContentOrigin, HotContentRecord, MediaBundle, MediaImage

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "raw"


def load_fixture(name: str) -> dict:
    path = FIXTURE_DIR / name
    if not path.exists():  # pragma: no cover - the fixtures are committed
        pytest.skip(f"{name} not captured; run scripts/discover_search.py --yes")
    return json.loads(path.read_text(encoding="utf-8"))


class StubTikHub:
    """Replays one captured response for any request."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[str] = []

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(f"GET {path}")
        return self._payload

    async def post_json(self, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append(f"POST {path}")
        return self._payload

    async def aclose(self) -> None:  # pragma: no cover
        return None


# ------------------------------------------------------------- the search API
@pytest.fixture
def search_client(monkeypatch, sqlite_db, tmp_path):
    """A TestClient for the search endpoint; no downloads, no network."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("MEDIA_DOWNLOAD_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_search_requires_a_keyword(search_client):
    """A blank keyword would still cost one billed call per platform."""
    assert search_client.post("/api/hot/search", json={"keyword": ""}).status_code == 422


def test_search_rejects_an_unknown_platform(search_client):
    response = search_client.post(
        "/api/hot/search", json={"keyword": "露营", "platforms": ["myspace"]}
    )
    assert response.status_code == 422
    assert "myspace" in response.json()["detail"]


def test_search_without_a_key_returns_503(monkeypatch, sqlite_db):
    monkeypatch.setenv("TIKHUB_API_KEY", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            response = client.post("/api/hot/search", json={"keyword": "露营"})
        assert response.status_code == 503
        assert "TIKHUB_API_KEY" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_stored_endpoint_actually_applies_the_new_filters(
    monkeypatch, sqlite_db, item_factory
):
    """The route must forward ``origin`` and ``with_images`` to the repository.

    A regression test with a real history: the filters were added to the repository
    and to the MCP tool, but not to the HTTP route, so ``?origin=search`` returned
    every row and the UI's new filters silently did nothing.
    """
    from app.services.pipeline.hot_pipeline import store_items

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()

    plain = item_factory("weibo", "w1", "榜单里的词条")
    with_media = item_factory("xiaohongshu", "x1", "搜索到的帖子")
    with_media.origin = ContentOrigin.SEARCH
    with_media.source_keyword = "露营"
    with_media.media = MediaBundle(images=[MediaImage(url="https://x/1.jpg")])
    async with session_scope(settings) as session:
        await store_items([plain, with_media], settings=settings, session=session)

    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as client:
        all_rows = client.get("/api/hot/stored").json()
        search_only = client.get("/api/hot/stored", params={"origin": "search"}).json()
        hot_only = client.get("/api/hot/stored", params={"origin": "hot"}).json()
        images_only = client.get("/api/hot/stored", params={"with_images": "true"}).json()
        no_images = client.get("/api/hot/stored", params={"with_images": "false"}).json()
        by_keyword = client.get(
            "/api/hot/stored", params={"source_keyword": "露营"}
        ).json()
        other_keyword = client.get(
            "/api/hot/stored", params={"source_keyword": "别的词"}
        ).json()
        bad_origin = client.get("/api/hot/stored", params={"origin": "nonsense"})

    assert all_rows["total"] == 2
    assert search_only["total"] == 1, "origin=search must filter"
    assert search_only["items"][0]["title"] == "搜索到的帖子"
    assert hot_only["total"] == 1, "origin=hot must filter"
    assert images_only["total"] == 1, "with_images=true must filter"
    assert images_only["items"][0]["image_count"] == 1
    assert no_images["total"] == 1, "with_images=false must filter the other way"
    # A search that collected N items must be able to show all N. Matching the title
    # substring showed 5 of 30 in a real run, which reads as "the search lost results".
    assert by_keyword["total"] == 1, "source_keyword must match exactly"
    assert by_keyword["items"][0]["title"] == "搜索到的帖子"
    assert other_keyword["total"] == 0, "a different keyword must not match"
    assert bad_origin.status_code == 422, "an unknown origin is a client error, not a silent pass"

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_search_collects_stores_and_carries_media(monkeypatch, sqlite_db, tmp_path):
    """Fixture -> adapter -> dedup -> store, with images recorded on the rows."""
    from app.services.pipeline.search_pipeline import collect_by_keyword

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("MEDIA_DOWNLOAD_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()

    result = await collect_by_keyword(
        "中国男篮",
        platforms=["xiaohongshu"],
        settings=settings,
        client=StubTikHub(load_fixture("search_xiaohongshu.json")),
        download_media=False,
    )

    assert result.billed_calls == 1, "one platform, one billed call"
    assert result.fetched["xiaohongshu"] == 20
    assert result.stored["inserted"] == 20
    assert result.images.get("downloaded") == 0

    async with session_scope(settings) as session:
        rows = (await session.execute(select(HotContentRecord))).scalars().all()

    assert len(rows) == 20
    assert all(row.origin == "search" for row in rows)
    assert all(row.source_keyword == "中国男篮" for row in rows)
    assert all(row.image_count > 0 for row in rows), "search results carry images"
    first = rows[0]
    assert (first.media or {}).get("images"), "the bundle is stored, not just the count"
    assert first.content_type in {"note", "video"}
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_search_hit_and_a_ranking_hit_are_distinguishable(monkeypatch, sqlite_db):
    """`origin` is what makes "why is this row here" answerable."""
    from app.services.pipeline.search_pipeline import collect_by_keyword

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("MEDIA_DOWNLOAD_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()

    await collect_by_keyword(
        "中国男篮",
        platforms=["xiaohongshu"],
        settings=settings,
        client=StubTikHub(load_fixture("search_xiaohongshu.json")),
        download_media=False,
    )

    from app.db.repository import list_admin_hot_contents

    async with session_scope(settings) as session:
        search_rows, search_total = await list_admin_hot_contents(session, origin="search")
        hot_rows, hot_total = await list_admin_hot_contents(session, origin="hot")
        with_images, _ = await list_admin_hot_contents(session, with_images=True)
        without_images, _ = await list_admin_hot_contents(session, with_images=False)

    assert search_total == 20 and hot_total == 0
    assert len(search_rows) == 20 and not hot_rows
    assert len(with_images) == 20 and not without_images
    get_settings.cache_clear()


# ------------------------------------------------------- the layout plan (§17+)
def test_media_settings_reject_values_that_silently_disable_them():
    """``0`` is not "off", it is a number that quietly stops the feature working.

    Found on the live server: ``MEDIA_MAX_IMAGES_PER_ITEM=0`` passed validation, which
    would have meant "capture no images" while looking like a normal configuration.
    """
    from app.services.settings_editor import validate_updates

    for key in ("MEDIA_MAX_IMAGES_PER_ITEM", "SEARCH_LIMIT_PER_PLATFORM"):
        clean, errors = validate_updates({key: "0"})
        assert not clean, f"{key}=0 must be rejected"
        assert errors and "at least 1" in errors[0], errors

    clean, errors = validate_updates({"MEDIA_MAX_BYTES_PER_FILE": "512"})
    assert not clean and errors, "a 512-byte cap cannot hold an image"
    assert "1024" in errors[0]

    clean, errors = validate_updates({"MEDIA_MAX_IMAGES_PER_ITEM": "9"})
    assert clean == {"MEDIA_MAX_IMAGES_PER_ITEM": "9"} and not errors


def test_rewrite_prompt_asks_for_a_plan_only_when_images_exist():
    """Asking for an image layout with no images invites the model to invent them."""
    from app.models.hot_content import (
        ContentType,
        HotContent,
        MediaBundle,
        MediaImage,
        Platform,
    )
    from app.services.ai.account_profile import AccountProfile
    from app.services.ai.rewriter import build_rewrite_prompt

    profile = AccountProfile(field="AI")
    with_images = HotContent(
        id="x:1",
        platform=Platform.XIAOHONGSHU,
        platform_content_id="1",
        title="标题",
        content_type=ContentType.NOTE,
        media=MediaBundle(images=[MediaImage(url="https://x/1.jpg", width=1080, height=1440)]),
    )
    without = HotContent(
        id="x:2", platform=Platform.XIAOHONGSHU, platform_content_id="2", title="标题"
    )

    prompt = build_rewrite_prompt(profile, with_images, None)
    assert "可用图片素材" in prompt
    assert "image_plan" in prompt
    assert "竖图" in prompt, "the aspect ratio is what the model can actually reason about"
    assert "看不到图片内容" in prompt, "the model must not claim to have seen the images"

    plain = build_rewrite_prompt(profile, without, None)
    assert "image_plan" not in plain
    assert "可用图片素材" not in plain


@pytest.mark.asyncio
async def test_layout_plan_is_persisted(sqlite_db, settings, item_factory):
    """The plan travels with the drafts it belongs to."""
    from app.db.repository import list_rewrites
    from app.models.hot_content import MediaBundle, MediaImage
    from app.services.ai.analyzer import run_analysis
    from app.services.ai.rewriter import run_rewriting
    from app.services.pipeline.hot_pipeline import store_items
    from tests.test_rewriter import StubDeepSeekClient, _analysis_payload, _rewrite_payload

    item = item_factory("xiaohongshu", "x1", "带图的热点")
    item.media = MediaBundle(images=[MediaImage(url="https://x/1.jpg", width=1080, height=1440)])
    async with session_scope(settings) as session:
        await store_items([item], settings=settings, session=session)

    await run_analysis(
        settings=settings, client=StubDeepSeekClient([{"results": [_analysis_payload(0)]}])
    )
    # ``run_analysis`` stores the analysis and marks it selected (the payload is
    # recommended=True), so nothing needs to be inserted by hand — and inserting one
    # anyway would violate the one-analysis-per-item unique constraint.

    plan = {
        "cover_index": 0,
        "cover_text": "封面大字",
        "caption_strategy": "首图给结论",
        "slots": [{"index": 0, "role": "钩子", "overlay": "大字", "caption": "配文"}],
    }
    payload = _rewrite_payload(needs_verification=False)
    payload["image_plan"] = plan

    await run_rewriting(settings=settings, client=StubDeepSeekClient([payload]))

    async with session_scope(settings) as session:
        rows, _total = await list_rewrites(session)
    assert rows, "a rewrite should have been stored"
    rewrite, _item = rows[0]
    assert rewrite.layout, "the layout plan must be persisted"
    assert rewrite.layout.get("cover_text") == "封面大字"
    assert rewrite.layout.get("slots")

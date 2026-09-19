"""Section 十六 tests: request building, extraction, and the detail stage.

The extraction tests run against the **real captured responses** in
``tests/fixtures/raw/detail_*.json``, which is what makes them meaningful: the
first version of this module used "the longest string in the payload" as a generic
body and would have fed TikHub's 215-character cache notice to the rewriter. Only a
real capture could reveal that.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.database import session_scope
from app.db.repository import recent_rows, rows_needing_detail, store_detail
from app.models.hot_content import HotContent, Platform
from app.services.pipeline.detail_stage import run_detail_stage
from app.services.tikhub.client import TikHubError
from app.services.tikhub.details import (
    DOUYIN_TOPIC_DETAIL,
    WEIBO_REALTIME_SEARCH,
    XIAOHONGSHU_IMAGE_DETAIL,
    XIAOHONGSHU_NOTE_DETAIL,
    build_detail_request,
    extract_detail,
)


def _item(platform: str, content_id: str, title: str, raw: dict | None = None) -> HotContent:
    return HotContent(
        id=HotContent.make_id(Platform(platform), content_id),
        platform=Platform(platform),
        platform_content_id=content_id,
        title=title,
        raw_data=raw or {},
    )


# ------------------------------------------------------------ request building
def test_xiaohongshu_prefers_the_note_endpoint_when_the_token_is_present():
    item = _item("xiaohongshu", "note123", "标题", {"xsec_token": "TOKEN"})
    path, params = build_detail_request(item)
    assert path == XIAOHONGSHU_NOTE_DETAIL
    assert params == {"note_id": "note123", "xsec_token": "TOKEN"}


def test_xiaohongshu_falls_back_to_the_app_endpoint_without_a_token():
    item = _item("xiaohongshu", "note123", "标题", {})
    path, params = build_detail_request(item)
    assert path == XIAOHONGSHU_IMAGE_DETAIL
    assert params == {"note_id": "note123"}


def test_xiaohongshu_without_an_id_has_no_route():
    assert build_detail_request(_item("xiaohongshu", "synth-abc", "标题", {})) is None


def test_douyin_uses_the_topic_name():
    path, params = build_detail_request(_item("douyin", "d1", "某个话题"))
    assert path == DOUYIN_TOPIC_DETAIL
    assert params == {"topic_name": "某个话题"}


def test_weibo_searches_because_a_hot_word_has_no_post_id():
    path, params = build_detail_request(_item("weibo", "w1", "某个热词"))
    assert path == WEIBO_REALTIME_SEARCH
    assert params["query"] == "某个热词"


def test_unknown_platform_has_no_route():
    item = _item("weibo", "w1", "")
    assert build_detail_request(item) is None


# ------------------------------------------------------- real fixture extracts
def test_xiaohongshu_extraction_from_the_capture(raw_fixture):
    payload = raw_fixture("detail_xiaohongshu")
    item = _item("xiaohongshu", "690c8bfc00000000070377b6", "kpopdemonhunters", {"xsec_token": "T"})
    result = extract_detail(item, XIAOHONGSHU_NOTE_DETAIL, payload)

    assert result.platform == "xiaohongshu"
    assert result.source_items == 1
    assert result.images, "the note card carries images"
    assert result.text.startswith("正文：")
    assert "话题：" in result.text
    assert "作者：" in result.text
    # The trap the first version fell into: never the cache notice.
    assert "cache" not in result.text.lower()
    assert "缓存" not in result.text


def test_douyin_extraction_collects_real_posts(raw_fixture):
    payload = raw_fixture("detail_douyin")
    item = _item("douyin", "d1", "九一八事变爆发95周年")
    result = extract_detail(item, DOUYIN_TOPIC_DETAIL, payload)

    assert result.source_items == 5, "the top five works become the material"
    assert result.text.startswith("抖音话题「九一八事变爆发95周年」")
    assert "（点赞" in result.text, "engagement is kept as context"
    assert "[陕视新闻]" in result.text, "the author is attributed"
    assert "话题指数趋势" in result.text, "the trend samples are summarised"
    assert len(result.images) == 5
    assert result.usable is True


def test_weibo_extraction_collects_posts_with_links(raw_fixture):
    payload = raw_fixture("detail_weibo")
    item = _item("weibo", "w1", "泰国网民吐槽日本亚运会")
    result = extract_detail(item, WEIBO_REALTIME_SEARCH, payload)

    assert result.source_items == 5
    assert result.text.startswith("微博实时搜索「泰国网民吐槽日本亚运会」")
    assert "搜索素材" in result.text, "the material is labelled as unverified search results"
    assert "https://weibo.com/" in result.text, "profile-relative links are absolutised"
    # `https://weibo.com` itself contains "//weibo.com", so strip the absolutised
    # form before checking that no bare protocol-relative link survived.
    assert "//weibo.com" not in result.text.replace("https://weibo.com", "")


def test_extraction_never_invents_text_when_the_payload_is_empty():
    result = extract_detail(_item("douyin", "d1", "话题"), DOUYIN_TOPIC_DETAIL, {"data": {}})
    assert result.text == ""
    assert result.usable is False
    assert result.images == []


# --------------------------------------------------------------- detail stage
class StubTikHubClient:
    """A TikHub stand-in for the stage tests."""

    def __init__(self, payloads: dict[str, Any], *, fail: set[str] | None = None) -> None:
        self._payloads = payloads
        self._fail = fail or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, params or {}))
        if path in self._fail:
            raise TikHubError("provider said no", status=500, path=path)
        return self._payloads.get(path, {"data": {}})

    async def aclose(self) -> None:  # pragma: no cover
        return None


async def _seed_selected(sqlite_db, settings, item_factory, items, *, selected: bool = True):
    """Store items and mark them selected, as Phase 3 would."""
    from app.models.ai_analysis import AiAnalysisRecord
    from app.services.pipeline.hot_pipeline import store_items

    async with session_scope(settings) as session:
        await store_items(items, settings=settings, session=session)
        rows = await recent_rows(session)
        for row in rows:
            session.add(
                AiAnalysisRecord(
                    hot_content_id=row.id,
                    topic="t",
                    summary="s",
                    recommended=True,
                    confidence=0.9,
                    selected=selected,
                )
            )


@pytest.mark.asyncio
async def test_detail_stage_is_off_by_default(sqlite_db, settings):
    result = await run_detail_stage(settings)
    assert result.skipped == 1
    assert result.billed_calls == 0
    assert any("DETAIL_FETCH_ENABLED" in error for error in result.errors)


@pytest.mark.asyncio
async def test_detail_stage_fetches_stores_and_records_provenance(
    sqlite_db, settings, item_factory, raw_fixture
):
    enabled = settings.model_copy(update={"detail_fetch_enabled": True})
    await _seed_selected(
        sqlite_db,
        enabled,
        item_factory,
        [
            item_factory("weibo", "w1", "泰国网民吐槽日本亚运会"),
            item_factory("xiaohongshu", "690c8bfc00000000070377b6", "kpopdemon", raw_data={"xsec_token": "T"}),
        ],
    )
    client = StubTikHubClient(
        {
            WEIBO_REALTIME_SEARCH: raw_fixture("detail_weibo"),
            XIAOHONGSHU_NOTE_DETAIL: raw_fixture("detail_xiaohongshu"),
        }
    )

    result = await run_detail_stage(enabled, client=client)

    assert result.considered == 2
    assert result.fetched == 2 and result.usable == 2 and result.failed == 0
    assert result.billed_calls == 2, "one billed call per item is reported"
    assert len(client.calls) == 2

    async with session_scope(enabled) as session:
        rows = await recent_rows(session)
        bodies = {row.platform: row for row in rows}
        assert bodies["weibo"].description.startswith("微博实时搜索")
        assert bodies["weibo"].detail_endpoint == WEIBO_REALTIME_SEARCH
        assert bodies["weibo"].detail_fetched_at is not None
        assert bodies["xiaohongshu"].description.startswith("正文：")
        # A fetched body means the item is no longer pending.
        remaining = await rows_needing_detail(session)
        assert remaining == []


@pytest.mark.asyncio
async def test_detail_stage_survives_a_failing_item(sqlite_db, settings, item_factory):
    enabled = settings.model_copy(update={"detail_fetch_enabled": True})
    await _seed_selected(sqlite_db, enabled, item_factory, [item_factory("weibo", "w1", "某热词")])
    client = StubTikHubClient({}, fail={WEIBO_REALTIME_SEARCH})

    result = await run_detail_stage(enabled, client=client)

    assert result.failed == 1 and result.fetched == 0
    assert result.billed_calls == 0, "a failed call is not counted as billed"
    assert any("provider said no" in error for error in result.errors)
    # The item stays pending so a later run can retry it.
    async with session_scope(enabled) as session:
        assert len(await rows_needing_detail(session)) == 1


@pytest.mark.asyncio
async def test_store_detail_keeps_the_row_pending_free_for_unusable_text(sqlite_db, settings, item_factory):
    """A hashtag-only note must not be re-fetched (and re-billed) forever."""
    from app.services.tikhub.details import DetailResult

    await _seed_selected(sqlite_db, settings, item_factory, [item_factory("weibo", "w1", "某热词")])
    async with session_scope(settings) as session:
        rows = await recent_rows(session)
        row = rows[0]
        await store_detail(
            session,
            row.id,
            DetailResult(platform="weibo", endpoint=WEIBO_REALTIME_SEARCH, text="太短", source_items=1),
        )
    async with session_scope(settings) as session:
        refreshed = (await recent_rows(session))[0]
        assert refreshed.description == "", "unusable text is not stored as a body"
        assert refreshed.detail_fetched_at is not None, "but the attempt is recorded"
        assert await rows_needing_detail(session) == []

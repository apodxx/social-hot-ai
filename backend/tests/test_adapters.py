"""Adapter tests: extractors, tolerant normalisation, and real fixtures.

The synthetic payloads below only exercise the coercion paths. Genuine provider
data lives in ``tests/fixtures/raw/`` once ``scripts/discover_raw.py`` has run,
and the fixture tests then assert the normalisers against the real shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.hot_content import ContentType, Platform
from app.services.tikhub.base import as_datetime, as_int, as_text, dig, find_item_list
from app.services.tikhub.client import TikHubClient
from app.services.tikhub.douyin import DouyinAdapter
from app.services.tikhub.weibo import WeiboAdapter
from app.services.tikhub.xiaohongshu import XiaohongshuAdapter


# --------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1234, 1234),
        ("1234", 1234),
        ("1,234", 1234),
        ("1.2万", 12000),
        ("3亿", 300000000),
        ("12.5w", 125000),
        ("2k", 2000),
        ("", None),
        (None, None),
        ("no digits", None),
        (True, None),
    ],
)
def test_as_int_handles_provider_formats(raw, expected):
    assert as_int(raw) == expected


def test_as_text_trims_and_rejects_empties():
    assert as_text("  hi  ") == "hi"
    assert as_text("   ") is None
    assert as_text(None) is None
    assert as_text(7) == "7"


def test_as_datetime_accepts_seconds_millis_and_text():
    seconds = as_datetime(1700000000)
    assert seconds is not None and seconds.year == 2023
    millis = as_datetime(1700000000000)
    assert millis == seconds
    text = as_datetime("2024-01-02 03:04:05")
    assert text is not None and text.tzinfo is not None
    assert as_datetime("not a date") is None


def test_dig_walks_dicts_and_lists():
    payload = {"data": {"items": [{"id": "a"}]}}
    assert dig(payload, "data.items.0.id") == "a"
    assert dig(payload, "data.missing.id") is None


def test_find_item_list_prefers_the_longest_dict_list():
    payload = {"data": {"noise": [{"a": 1}], "items": [{"id": 1}, {"id": 2}, {"id": 3}]}}
    items = find_item_list(payload)
    assert [item["id"] for item in items] == [1, 2, 3]


def test_find_item_list_honours_a_hint():
    payload = {"data": {"a": [{"x": 1}, {"x": 2}], "b": [{"y": 1}]}}
    assert find_item_list(payload, path_hint=("data.b",)) == [{"y": 1}]


# -------------------------------------------------------------------- adapters
@pytest.fixture
def client(settings):
    return TikHubClient(settings)


def test_weibo_normalize_matches_the_real_payload_shape(client):
    """Field names come from a real capture: keyword/keyword_url/heat/rank/is_top."""
    items = [
        {
            "rank": 0,
            "is_top": True,
            "keyword": "置顶话题",
            "keyword_url": "/weibo?q=%23top%23",
            "tag": "热",
            "heat": "",
        },
        {
            "rank": 1,
            "is_top": False,
            "keyword": "某事件",
            "keyword_url": "/weibo?q=%23x%23&band_rank=1",
            "tag": "新",
            "heat": "",
        },
        {"rank": 0, "is_top": False, "keyword": "另一置顶", "keyword_url": "", "tag": "重磅", "heat": ""},
        {"rank": 4, "is_top": False, "keyword": "有热度的事件", "heat": "1.2万"},
        {"rank": 9, "is_top": False, "heat": "12345"},  # no keyword: skipped
    ]
    out = WeiboAdapter(client).normalize(items)

    assert [item.title for item in out] == ["置顶话题", "某事件", "另一置顶", "有热度的事件"]
    # Real data has two entries sharing rank 0 (multiple pinned items), so the
    # payload's own rank is not unique: rank must come from list order.
    assert [item.rank for item in out] == [1, 2, 3, 4]
    # keyword_url is site-relative in the real response, so it is absolutised.
    assert out[0].url == "https://s.weibo.com/weibo?q=%23top%23"
    assert out[2].url is None, "an empty keyword_url must stay None, not become a URL"
    assert out[0].hot_value is None, "heat arrived empty for every real entry"
    assert out[3].hot_value == 12000, "a non-empty heat is parsed"
    assert out[0].content_type is ContentType.TOPIC
    assert out[0].platform is Platform.WEIBO
    assert out[0].raw_data["tag"] == "热", "provider-only fields stay in raw_data"
    assert out[0].id == "weibo:置顶话题"


def test_douyin_normalize_reads_nested_video(client):
    items = [
        {
            "word": "热点话题",
            "hot_value": 900,
            "aweme_id": "7123",
            "aweme": {"video": {"play_addr": {"url_list": ["https://v/1.mp4"]}}},
        },
        {"word": "另一话题", "hot_value": "2.5万"},
    ]
    out = DouyinAdapter(client).normalize(items)
    assert out[0].platform_content_id == "7123"
    assert out[0].content_type is ContentType.VIDEO
    assert out[0].video_url == "https://v/1.mp4"
    assert out[1].content_type is ContentType.TOPIC
    assert out[1].hot_value == 25000


def test_xiaohongshu_normalize_reads_note_card(client):
    items = [
        {
            "id": "note-1",
            "note_card": {
                "display_title": "标题",
                "desc": "正文",
                "user": {"nickname": "作者", "user_id": "u1"},
                "interact_info": {"liked_count": "1.5万", "comment_count": 12},
                "cover": {"url_default": "https://img/1.jpg"},
            },
        }
    ]
    out = XiaohongshuAdapter(client).normalize(items)
    assert len(out) == 1
    assert out[0].title == "标题"
    assert out[0].author == "作者"
    assert out[0].likes == 15000
    assert out[0].comments == 12
    assert out[0].cover_url == "https://img/1.jpg"
    assert out[0].content_type is ContentType.NOTE
    assert out[0].rank == 1, "feed position, not a hot ranking"


def test_adapters_never_invent_a_missing_counter(client):
    out = WeiboAdapter(client).normalize([{"word": "无数据条目"}])
    assert out[0].hot_value is None, "an absent counter must stay None, not become 0"
    assert out[0].publish_time is None


# ------------------------------------------------------------- real fixtures
@pytest.mark.parametrize("platform", ["weibo", "douyin", "xiaohongshu"])
def test_fixtures_are_not_encoding_mangled(platform, raw_fixture):
    """Guard against Latin-1 mangling of captured responses.

    PowerShell's ``Invoke-WebRequest`` decodes a charset-less body as ISO-8859-1,
    which silently turned Chinese titles into Latin-1 gibberish in three fixtures.
    The damage is recoverable (``scripts/repair_fixture_encoding.py``) but it must
    not go unnoticed again.
    """
    payload = raw_fixture(platform)
    if payload is None:
        pytest.skip(f"no captured fixture for {platform}")
    from app.services.tikhub.base import demangle_latin1, looks_latin1_mangled

    titles = [
        item.get("keyword") or item.get("word") or item.get("display_title") or ""
        for item in find_item_list(payload)
    ]
    mangled = [title for title in titles if looks_latin1_mangled(title)]
    assert not mangled, (
        f"{len(mangled)} mangled titles in the {platform} fixture, e.g. "
        f"{mangled[0][:40]!r} -> {demangle_latin1(mangled[0])!r}; "
        f"repair with `python scripts/repair_fixture_encoding.py --apply`"
    )


@pytest.mark.parametrize(
    ("platform", "adapter_class"),
    [
        ("weibo", WeiboAdapter),
        ("douyin", DouyinAdapter),
        ("xiaohongshu", XiaohongshuAdapter),
    ],
)
def test_normalize_real_captured_response(platform, adapter_class, client, raw_fixture):
    """Validate against genuine TikHub data when a capture exists."""
    payload = raw_fixture(platform)
    if payload is None:
        pytest.skip(
            f"no captured fixture for {platform}; run "
            f"`python scripts/discover_raw.py --yes --platform {platform}` first"
        )
    items = find_item_list(payload)
    assert items, "a real response must contain item dicts"
    out = adapter_class(client).normalize(items)
    assert out, "the normaliser must map real items, not drop them all"
    for item in out:
        assert item.platform_content_id, "every item needs an id"
        assert item.title, "every mapped item needs a title"
        assert item.raw_data, "raw_data must be preserved"
        if item.publish_time is not None:
            assert item.publish_time.tzinfo is not None

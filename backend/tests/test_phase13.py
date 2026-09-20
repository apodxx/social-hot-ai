"""Phase 13: 图片搜索（搜图配图）。

对着**真实响应的存档**测试，所以离线、免费、且覆盖真实字段名。存档是必须的：
这个接口的字段分散在 ``image_info`` / ``note_info`` / ``user_info`` / ``share_info``
四组里，第一版解析器把它们当成 item 的直接子键，结果 20 条解析出 0 条。

fixture: docs/fixtures/xiaohongshu_image_search.json（一次真实调用后存档）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.tikhub.base import dig
from app.services.tikhub.image_search import (
    XIAOHONGSHU_IMAGE_SEARCH,
    FoundImage,
    normalize_images,
)

FIXTURE = (
    Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "xiaohongshu_image_search.json"
)


@pytest.fixture(scope="module")
def payload() -> dict:
    if not FIXTURE.is_file():
        pytest.skip(f"fixture missing: {FIXTURE}")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_endpoint_is_the_verified_one():
    """微博的 fetch_pic_search 试过同关键词返回 0 张，所以不作为默认来源。"""
    assert XIAOHONGSHU_IMAGE_SEARCH == "/api/v1/xiaohongshu/app_v2/search_images"


def test_every_fixture_item_parses(payload):
    """**回归测试**：字段在四组嵌套里，压平着读会得到 0 条。"""
    items = dig(payload, "data.data.items")
    assert len(items) == 20, "the fixture should hold one full page"

    found = normalize_images(items)
    assert len(found) == 20, f"all items must parse, got {len(found)}"

    for image in found:
        assert image.url.startswith("http"), image.url
        # Attribution is not optional: these are other people's pictures.
        assert image.author, "作者必须保留，用于署名"
        assert image.link.startswith("http"), "原文链接必须保留，用于溯源"
        assert image.note_id, "note_id 用于去重与回链"


def test_the_parsed_fields_come_from_the_right_groups(payload):
    """字段必须取自正确的嵌套组，而不是碰巧同名。"""
    first = dig(payload, "data.data.items")[0]
    image = normalize_images([first])[0]

    assert image.url in (
        first["image_info"]["original"],
        first["image_info"]["url_size_large"],
        first["image_info"]["url"],
    )
    assert image.title == first["note_info"]["title"]
    assert image.likes == first["note_info"]["liked_count"]
    assert image.collects == first["note_info"]["collected_count"]
    assert image.author == first["user_info"]["nickname"]
    assert image.note_id == first["note_info"]["note_id"]
    # The link lives in share_info, not note_info.
    assert image.link == first["share_info"]["link"]


def test_the_fixture_is_topically_relevant(payload):
    """搜到的内容确实和关键词相关——这正是搜图优于随便抓图的地方。"""
    titles = " ".join(image.title for image in normalize_images(dig(payload, "data.data.items")))
    assert "数据结构" in titles, "检索结果应当围绕关键词"


def test_missing_fields_are_skipped_not_faked():
    """缺图/缺 id 的条目跳过，而不是编一个出来。"""
    items = [
        {"image_info": {"url": "https://x/1.jpg"}},  # no note_info at all
        {"image_info": {}, "note_info": {"title": "没有图"}},  # no image
        {"note_info": {"note_id": "abc"}},  # no image_info
        "not a dict",
    ]
    found = normalize_images(items)
    assert len(found) == 1, "only the first entry has an image url"
    assert found[0].url == "https://x/1.jpg"
    assert found[0].author == ""  # absent, not invented


def test_url_preference_prefers_the_original():
    """清晰度优先：original > url_size_large > url。"""
    item = {
        "image_info": {
            "original": "https://x/original.jpg",
            "url_size_large": "https://x/large.jpg",
            "url": "https://x/small.jpg",
        }
    }
    assert normalize_images([item])[0].url == "https://x/original.jpg"

    # Falls back in order when the better ones are absent.
    assert normalize_images([{"image_info": {"url": "https://x/s.jpg"}}])[0].url == "https://x/s.jpg"
    assert normalize_images([{"image_info": {"url": "not-a-url"}}]) == []


def test_relevance_filter_rejects_same_words_different_domain():
    """**回归测试：解决"图不对题"。**

    小红书图片搜索是**宽松文本匹配**。实测搜「红黑树」拿回的配图来自：
      * 「壁纸 #红黑美学 #自然治愈 #树的哲学」——它把"红黑"和"树"当两个词分别匹配；
      * 「功夫不负有心人之六尺红豆杉树墙」——园艺内容，只匹配了"树"。

    我们看不到图，但看得到来源笔记的标题与正文——用它在下载前就能剔掉这些。
    """
    from app.services.tikhub.image_search import FoundImage, is_relevant

    def note(title: str, description: str = "") -> FoundImage:
        return FoundImage(url="https://x/1.jpg", title=title, description=description)

    # 真实的误召回：同名不同域，必须剔除。
    assert not is_relevant(note("壁纸 #红黑美学 #自然治愈 #树的哲学"), "红黑树")
    assert not is_relevant(note("功夫不负有心人之六尺红豆杉树墙"), "B+树")
    assert not is_relevant(note("留白美学：树冠羞避"), "B+树")
    assert not is_relevant(note("🩸", "#审美#艺术#树#枝"), "红黑树")

    # 真正相关的保留。
    assert is_relevant(note("红黑树原理图解"), "红黑树")
    assert is_relevant(note("B+树"), "B+树")
    assert is_relevant(note("B树/B+树常考知识点"), "B+树")

    # 归一化：真实数据里出现过 "B + 树"（带空格），直接包含比较会漏掉它。
    assert is_relevant(note("二叉树、红黑树、B + 树适用场景对比"), "B+树")
    # 正文里出现也算（有些笔记标题是水文，正文才对题）。
    assert is_relevant(note("随手记", "今天把红黑树的插入调整搞懂了"), "红黑树")

    # 主题为空时不过滤（避免把一切都丢掉）。
    assert is_relevant(note("任何标题"), "")


def test_media_fetch_failures_are_never_stored():
    """媒体下载失败的行不该被当成"有图"。"""
    from app.services.tikhub.image_search import FoundImage

    image = FoundImage(url="https://x/1.jpg", title="B+树")
    assert image.local_path == "", "默认没有本地路径"
    assert image.as_dict()["local_path"] == ""


def test_found_image_serialises_for_the_api():
    image = FoundImage(
        url="https://x/1.jpg",
        title="数据结构",
        author="某人",
        link="https://www.xiaohongshu.com/explore/abc",
        likes=10,
        local_path="media/aa/bb.jpg",
    )
    payload = image.as_dict()
    assert payload["local_path"] == "media/aa/bb.jpg"
    assert payload["author"] == "某人"
    assert "url" in payload and "link" in payload


# --------------------------------------------------- 偶发 400 的重试
@pytest.mark.asyncio
async def test_a_transient_400_is_retried(monkeypatch):
    """**实测这个接口偶发返回 400**：同一个请求几秒后再发就是 200。

    TikHub 客户端把 4xx（429 除外）当终态，对这个接口过于严格——所以搜图自己再试。
    失败请求不计费，重试的成本只是时间。
    """
    from app.core.config import get_settings
    from app.services.tikhub.client import TikHubError
    from app.services.tikhub.image_search import search_images

    attempts = {"n": 0}
    good = {
        "data": {
            "data": {
                "items": [
                    {
                        "image_info": {"url": "https://x/1.jpg", "width": 100, "height": 200},
                        "note_info": {"title": "数据结构", "note_id": "abc"},
                        "user_info": {"nickname": "某人"},
                        "share_info": {"link": "https://www.xiaohongshu.com/explore/abc"},
                    }
                ]
            }
        }
    }

    class FlakyClient:
        async def get_json(self, path, params):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TikHubError(f"request failed: HTTP 400 | path={path} | status=400")
            return good

        async def aclose(self):
            return None

    # Don't actually sleep in the test.
    import asyncio as asyncio_module

    real_sleep = asyncio_module.sleep
    monkeypatch.setattr(asyncio_module, "sleep", lambda _seconds: real_sleep(0))

    found = await search_images(
        "数据结构", client=FlakyClient(), settings=get_settings()
    )
    assert attempts["n"] == 3, "should have retried twice then succeeded"
    assert len(found) == 1 and found[0].url == "https://x/1.jpg"


@pytest.mark.asyncio
async def test_a_persistent_failure_gives_up_and_raises(monkeypatch):
    """一直失败就如实抛出，不要假装搜到了图。"""
    from app.core.config import get_settings
    from app.services.tikhub.client import TikHubError
    from app.services.tikhub.image_search import SEARCH_ATTEMPTS, search_images

    attempts = {"n": 0}

    class BrokenClient:
        async def get_json(self, path, params):
            attempts["n"] += 1
            raise TikHubError("request failed: HTTP 400 | status=400")

        async def aclose(self):
            return None

    import asyncio as asyncio_module

    real_sleep = asyncio_module.sleep
    monkeypatch.setattr(asyncio_module, "sleep", lambda _seconds: real_sleep(0))

    with pytest.raises(TikHubError):
        await search_images("数据结构", client=BrokenClient(), settings=get_settings())
    assert attempts["n"] == SEARCH_ATTEMPTS

"""Phase 13: 知识科普文章、标签词表、标签球的数据面。

不花钱：DeepSeek 用 stub，搜图用 respx 模拟（并复用已有 fixture 的形状）。
重点是三件事——**标签存库后免费复用**、**分步失败不互相牵连**、**字段级容错**。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.services.ai.knowledge import (
    ARTICLE_SYSTEM_PROMPT,
    parse_article,
    parse_platforms,
    parse_tags,
)
from tests.test_rewriter import StubDeepSeekClient

GOOD_ARTICLE = {
    "title": "B+树为什么是数据库的默认选择",
    "hook": "你有没有想过，为什么数据库的索引几乎都用 B+树，而不是更「省内存」的二叉树？",
    "audience": "大二、刚学完数据结构",
    "difficulty": "进阶",
    "sections": [
        {"heading": "从磁盘说起", "body": "内存访问大约几十纳秒……", "key_points": ["磁盘比内存慢几个数量级"]},
        {"heading": "树高决定 IO 次数", "body": "一次 IO 读一个页……", "key_points": ["树高就是 IO 次数"]},
        {"heading": "为什么不是二叉树", "body": "二叉树的树高是 log2(N)……", "key_points": []},
    ],
    "glossary": [
        {"term": "页", "explanation": "数据库读写的最小单位，通常 16KB"},
        {"term": "B+树", "explanation": "所有数据在叶子节点、叶子成链的多路平衡树"},
    ],
    "takeaways": ["树高就是磁盘 IO 次数", "多路比二叉更矮"],
    "further_reading": [{"title": "手写一个最小 B+树", "note": "能彻底搞清楚分裂过程"}],
}

GOOD_PLATFORMS = {
    "xiaohongshu": {"title": "数据库为什么都用B+树", "content": "面试被问懵了……", "hashtags": ["数据结构", "数据库"]},
    "weibo": {"title": "一个反常识", "content": "越省内存的结构，数据库越不用。", "hashtags": ["计算机"]},
    "douyin": {
        "hook": "数据库的索引为什么不用二叉树？",
        "script": "第一句话先说结论……",
        "scenes": ["镜头一：翻书"],
        "subtitles": "树高 = IO 次数",
        "cta": "评论区说说你被问过什么",
    },
}

GOOD_TAGS = {
    "tags": [
        {"name": "数据结构", "kind": "基础理论", "difficulty": "入门", "blurb": "组织数据的方式", "weight": 3.0},
        {"name": "B+树", "kind": "基础理论", "difficulty": "进阶", "blurb": "数据库索引的结构", "weight": 1.5},
        {"name": "页表", "kind": "系统网络", "difficulty": "高阶", "blurb": "虚拟内存的映射表", "weight": 1.2},
        {"name": "Git", "kind": "工程实践", "difficulty": "入门", "blurb": "版本控制", "weight": 2.0},
        # 同义重复：只应保留权重更高的那个。
        {"name": "b+树", "kind": "基础理论", "difficulty": "进阶", "blurb": "重复项", "weight": 2.2},
        {"name": "", "kind": "x", "blurb": "空名字应被丢弃"},
        "Linux",
    ]
}


@pytest.fixture
def knowledge_settings(monkeypatch, tmp_path, sqlite_db):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


# ------------------------------------------------------------------ 解析容错
def test_article_parsing_tolerates_missing_fields():
    """模型漏字段时按空处理并记警告，而不是整篇作废。"""
    draft = parse_article({"title": "只有标题"})
    assert draft.title == "只有标题"
    assert draft.sections == []
    assert any("sections" in warning for warning in draft.warnings)
    assert any("hook" in warning for warning in draft.warnings)

    assert parse_article(None).warnings, "非 JSON 也要有警告而不是静默"
    assert parse_article([1, 2]).sections == []


def test_article_parsing_clamps_runaway_sections():
    """模型给太多节时截断并说明——不截断会撑爆前端与输出预算。"""
    from app.services.ai.knowledge import MAX_SECTIONS

    payload = dict(GOOD_ARTICLE)
    payload["sections"] = [{"heading": f"第{i}节", "body": "…"} for i in range(12)]
    draft = parse_article(payload)
    assert len(draft.sections) == MAX_SECTIONS
    assert any("截断" in warning for warning in draft.warnings)


def test_prompts_forbid_writing_past_the_output_limit():
    """回归测试：文章太长会被截断成**不合法 JSON**，整篇作废。

    实测踩到过：报错里 JSON 断在 `"audie`，因为文章复用了 REWRITE_MAX_TOKENS=6000，
    而 3-7 节 × 200-500 字根本写不完。现在提示词里明确要求克制篇幅，并单独给输出上限。
    """
    from app.services.ai.knowledge import ARTICLE_SYSTEM_PROMPT

    assert "篇幅要克制" in ARTICLE_SYSTEM_PROMPT
    assert "被截断的 JSON 是完全不可用的" in ARTICLE_SYSTEM_PROMPT
    # 请求的篇幅本身也要收窄。
    assert "sections 给 3-6 个" in ARTICLE_SYSTEM_PROMPT
    assert "150-400 字" in ARTICLE_SYSTEM_PROMPT


def test_article_tokens_are_separate_from_rewrite_tokens():
    """文章的输出上限必须是**独立设置**，不能复用二创的（那正是截断的原因）。"""
    from app.core.config import Settings

    settings = Settings(
        tikhub_api_key="", deepseek_api_key="", database_url=""
    )
    assert settings.article_max_tokens > settings.rewrite_max_tokens, (
        "文章比二创长得多，上限必须更高"
    )


def test_glossary_without_a_term_is_dropped():
    draft = parse_article(
        {"title": "t", "hook": "h", "glossary": [{"explanation": "没有术语"}, {"term": "有"}]}
    )
    assert [entry["term"] for entry in draft.glossary] == ["有"]


def test_further_reading_accepts_plain_strings():
    """模型有时直接给字符串数组，这也要能用。"""
    draft = parse_article(
        {"title": "t", "hook": "h", "further_reading": ["去看 RFC", {"title": "写代码"}]}
    )
    titles = [entry["title"] for entry in draft.further_reading]
    assert titles == ["去看 RFC", "写代码"]


def test_tag_parsing_dedupes_case_insensitively_and_keeps_the_heavier():
    tags = parse_tags(GOOD_TAGS)
    names = [tag.name for tag in tags]
    assert "B+树" in names
    assert len([name for name in names if name.casefold() == "b+树"]) == 1
    assert tags[0].name == "数据结构", "按权重降序"
    # A bare string is accepted as a name.
    assert "Linux" in names
    assert all(tag.name.strip() for tag in tags), "空名字必须被丢弃"


def test_platform_parsing_keeps_the_three_shapes_distinct():
    parsed = parse_platforms(GOOD_PLATFORMS)
    assert set(parsed) == {"xiaohongshu", "weibo", "douyin"}
    assert parsed["douyin"]["hook"].startswith("数据库")
    assert parsed["xiaohongshu"]["hashtags"] == ["数据结构", "数据库"]
    # 缺平台就不出现在结果里，而不是编一个空壳。
    assert parse_platforms({"xiaohongshu": {}}).get("weibo") is None
    assert parse_platforms(None) == {}


# ------------------------------------------------------------- 标签词表与存储
@pytest.mark.asyncio
async def test_tag_vocabulary_is_cached_and_free_after_the_first_time(
    knowledge_settings, monkeypatch
):
    """**打开页面不花钱**：词表存库，第二次调用不再访问模型。"""
    from app.services.knowledge_service import ensure_tag_vocabulary
    import app.services.ai.deepseek as deepseek_module

    calls = {"n": 0}

    def factory(settings):
        calls["n"] += 1
        return StubDeepSeekClient([GOOD_TAGS])

    monkeypatch.setattr(deepseek_module, "DeepSeekClient", factory)

    first = await ensure_tag_vocabulary(knowledge_settings)
    assert first.generated is True
    assert first.total >= 5
    assert calls["n"] == 1

    second = await ensure_tag_vocabulary(knowledge_settings)
    assert second.generated is False, "第二次必须复用库里的词表"
    assert calls["n"] == 1, "不能再次调用模型"
    assert second.total == first.total


@pytest.mark.asyncio
async def test_a_small_vocabulary_warns_instead_of_regenerating(
    knowledge_settings, monkeypatch
):
    """**回归测试：这里曾经是一笔反复发生的静默费用。**

    早期实现是"少于 20 个标签就重新生成"。只要模型某次输出偏少，**每次打开页面都会再调
    一次模型**——与"先免费复用、手动才重新生成"的约定直接冲突。

    现在只要库里有标签就复用，数量不足只用 warnings 提示运营方去点刷新。
    """
    from app.services.knowledge_service import MIN_TAGS, ensure_tag_vocabulary
    import app.services.ai.deepseek as deepseek_module

    calls = {"n": 0}

    def factory(settings):
        calls["n"] += 1
        return StubDeepSeekClient([GOOD_TAGS])  # 只有 6 个标签，少于 MIN_TAGS

    monkeypatch.setattr(deepseek_module, "DeepSeekClient", factory)

    first = await ensure_tag_vocabulary(knowledge_settings)
    assert first.total < MIN_TAGS, "这个 stub 故意给得少"
    assert calls["n"] == 1, "空库时生成一次"

    for _ in range(3):
        again = await ensure_tag_vocabulary(knowledge_settings)
        assert again.generated is False
    assert calls["n"] == 1, "反复打开页面绝不能反复调模型"
    assert any("刷新标签" in warning for warning in again.warnings), "要提示运营方手动刷新"


@pytest.mark.asyncio
async def test_refresh_keeps_manual_weights(knowledge_settings, monkeypatch):
    """人工调过的标签不该被一次刷新覆盖。"""
    from app.db.database import session_scope
    from app.models.knowledge import KnowledgeTagRecord
    from app.services import knowledge_service

    async with session_scope(knowledge_settings) as session:
        session.add(
            KnowledgeTagRecord(name="数据结构", kind="人工分类", weight=9.5, source="manual")
        )

    import app.services.ai.deepseek as deepseek_module

    monkeypatch.setattr(
        deepseek_module, "DeepSeekClient", lambda settings: StubDeepSeekClient([GOOD_TAGS])
    )
    await knowledge_service.refresh_tag_vocabulary(knowledge_settings)

    from sqlalchemy import select

    async with session_scope(knowledge_settings) as session:
        row = (
            await session.execute(
                select(KnowledgeTagRecord).where(KnowledgeTagRecord.name == "数据结构")
            )
        ).scalars().first()
    assert row.weight == 9.5, "人工权重必须保留"
    assert row.kind == "人工分类", "人工分类必须保留"


# ------------------------------------------------------------------ 文章生成
@pytest.mark.asyncio
async def test_article_generation_records_each_step_separately(
    knowledge_settings, monkeypatch
):
    """文章/平台/搜图三步各自记账，失败互不牵连。"""
    from app.services import knowledge_service
    import app.services.ai.deepseek as deepseek_module

    monkeypatch.setattr(
        deepseek_module,
        "DeepSeekClient",
        lambda settings: StubDeepSeekClient([GOOD_ARTICLE, GOOD_PLATFORMS]),
    )

    async def fake_search(keyword, *, limit, settings, client=None):
        from app.services.tikhub.image_search import FoundImage

        return (
            [
                FoundImage(
                    url="https://x/1.jpg",
                    title=keyword,
                    author="某人",
                    link="https://www.xiaohongshu.com/explore/abc",
                    local_path="media/aa/1.jpg",
                )
            ],
            {"searched": 1, "downloaded": 1},
        )

    import app.services.tikhub.image_search as image_search_module

    monkeypatch.setattr(image_search_module, "search_and_download", fake_search)

    result = await knowledge_service.generate_article(
        "B+树", settings=knowledge_settings
    )
    assert result.ok, result.error
    assert result.article["title"] == GOOD_ARTICLE["title"]
    assert set(result.article["platforms"]) == {"xiaohongshu", "weibo", "douyin"}
    assert result.images and result.images[0]["local_path"] == "media/aa/1.jpg"
    assert set(result.steps) >= {"article", "platforms", "images"}
    assert result.steps["article"]["ok"] and result.steps["article"]["tokens"]
    assert result.estimated_cny > 0
    # 文章自带标签：主题 + 术语表 + 短小标题，无需额外调用模型。
    assert "B+树" in result.tags and "页" in result.tags


@pytest.mark.asyncio
async def test_platform_failure_still_saves_the_article(knowledge_settings, monkeypatch):
    """**已经付过钱的文章不该被后面的失败作废。**"""
    from app.services import knowledge_service
    import app.services.ai.deepseek as deepseek_module
    import app.services.tikhub.image_search as image_search_module

    class HalfBroken(StubDeepSeekClient):
        async def complete_json(self, *, system, user, **kwargs):
            if system == ARTICLE_SYSTEM_PROMPT:
                return await super().complete_json(system=system, user=user, **kwargs)
            raise RuntimeError("平台渲染炸了")

    monkeypatch.setattr(
        deepseek_module, "DeepSeekClient", lambda settings: HalfBroken([GOOD_ARTICLE])
    )

    async def no_images(keyword, *, limit, settings, client=None):
        raise RuntimeError("搜图也炸了")

    monkeypatch.setattr(image_search_module, "search_and_download", no_images)

    result = await knowledge_service.generate_article("B+树", settings=knowledge_settings)
    assert result.ok, "文章成功就必须算成功"
    assert result.article["platforms"] == {}
    assert result.article["images"] == []
    assert result.steps["platforms"]["ok"] is False
    assert result.steps["images"]["ok"] is False
    assert "炸了" in result.steps["platforms"]["error"]


@pytest.mark.asyncio
async def test_a_useless_article_is_rejected_before_storing(knowledge_settings, monkeypatch):
    """模型返回空壳时不要存一篇空文章，也不要说成功。"""
    from app.services import knowledge_service
    import app.services.ai.deepseek as deepseek_module

    monkeypatch.setattr(
        deepseek_module, "DeepSeekClient", lambda settings: StubDeepSeekClient([{"title": ""}])
    )
    result = await knowledge_service.generate_article("B+树", settings=knowledge_settings)
    assert not result.ok
    assert "缺少标题或正文" in result.error
    rows, total = await knowledge_service.list_articles(knowledge_settings)
    assert total == 0, "空壳不该入库"


@pytest.mark.asyncio
async def test_generating_for_a_tag_bumps_its_weight(knowledge_settings, monkeypatch):
    """点标签生成文案后，该标签在球体上应该更「重」。"""
    from app.db.database import session_scope
    from app.models.knowledge import KnowledgeTagRecord
    from app.services import knowledge_service
    import app.services.ai.deepseek as deepseek_module

    async with session_scope(knowledge_settings) as session:
        tag = KnowledgeTagRecord(name="B+树", kind="基础理论", weight=1.0)
        session.add(tag)
        await session.flush()
        tag_id = tag.id

    monkeypatch.setattr(
        deepseek_module,
        "DeepSeekClient",
        lambda settings: StubDeepSeekClient([GOOD_ARTICLE, GOOD_PLATFORMS]),
    )
    result = await knowledge_service.generate_article(
        "B+树", settings=knowledge_settings, with_images=False, tag_id=tag_id
    )
    assert result.ok

    from sqlalchemy import select

    async with session_scope(knowledge_settings) as session:
        row = (
            await session.execute(
                select(KnowledgeTagRecord).where(KnowledgeTagRecord.id == tag_id)
            )
        ).scalars().first()
    assert row.article_count == 1
    assert row.weight > 1.0


# ------------------------------------------------- 截断：区分、重试、可诊断
def test_truncation_is_a_distinct_error_from_a_syntax_error():
    """**截断和 JSON 语法错误需要不同的应对**，混在一起就无法判断该不该重试。

    实测踩到过两次：报错只说 "not parseable as JSON"，看不出是截断还是模型写坏了 JSON。
    """
    from app.services.ai.deepseek import DeepSeekJSONError, DeepSeekTruncatedError

    assert issubclass(DeepSeekTruncatedError, DeepSeekJSONError), (
        "必须是子类，这样已有的 except DeepSeekJSONError 仍然能兜住"
    )
    error = DeepSeekTruncatedError("cut off", completion_tokens=8000, finish_reason="length")
    assert error.completion_tokens == 8000
    assert error.finish_reason == "length"


def test_literal_newlines_inside_strings_are_tolerated():
    """中文长文本最常见的坏法：**字符串内部出现真实换行**。

    JSON 规范不允许，但模型经常这么写。``strict=False`` 正是为此存在，所以先免费试一次，
    省掉一次重试调用。
    """
    from app.services.ai.deepseek import extract_json

    broken = '{"title": "标题", "hook": "第一行\n第二行\n第三行"}'
    with pytest.raises(Exception):
        import json

        json.loads(broken)  # 确认标准解析确实拒绝它
    parsed = extract_json(broken)
    assert parsed["hook"] == "第一行\n第二行\n第三行"


def test_json_errors_report_the_length_and_the_tail():
    """报错只带前 200 字符是**诊断缺陷**：截断发生在末尾，头 200 字符看起来总是正常。

    所以报错里要有总长度与结尾片段——上一轮就是因为只有头部才难以定位。
    """
    from app.services.ai.deepseek import DeepSeekJSONError, extract_json

    broken = '{"title": "标题", "hook": "开头看起来很正常，' + "很长的正文" * 200
    with pytest.raises(DeepSeekJSONError) as excinfo:
        extract_json(broken)
    message = str(excinfo.value)
    assert "响应共" in message, "要报告总长度"
    assert "结尾" in message, "要报告结尾片段（截断发生在那里）"
    assert str(len(broken)) in message


def test_a_compact_prompt_is_used_for_the_retry():
    """被截断后的重试必须**明确要求更短**，否则同样的提示词会再次被截断。"""
    from app.services.ai.knowledge import build_article_prompt

    normal = build_article_prompt("神经网络")
    compact = build_article_prompt("神经网络", compact=True)
    assert "篇幅必须短" not in normal
    assert "篇幅必须短" in compact
    assert "被截断" in compact, "要告诉模型上一次为什么失败"


@pytest.mark.asyncio
async def test_a_truncated_article_is_retried_with_a_shorter_ask(
    knowledge_settings, monkeypatch
):
    """**回归测试**：文章被截断时，自动用更短的篇幅重试一次，而不是直接失败。

    不重试的话，那一次已经付费的调用就白费了——而截断恰恰是可以靠"要求更短"解决的。
    """
    from app.services import knowledge_service
    from app.services.ai.deepseek import DeepSeekTruncatedError
    import app.services.ai.deepseek as deepseek_module

    calls: list[str] = []

    class TruncatingOnce(StubDeepSeekClient):
        async def complete_json(self, *, system, user, **kwargs):
            calls.append(user)
            if len(calls) == 1:
                raise DeepSeekTruncatedError(
                    "cut off at 8000", completion_tokens=8000, finish_reason="length"
                )
            return await super().complete_json(system=system, user=user, **kwargs)

    monkeypatch.setattr(
        deepseek_module,
        "DeepSeekClient",
        lambda settings: TruncatingOnce([GOOD_ARTICLE, GOOD_PLATFORMS]),
    )

    result = await knowledge_service.generate_article(
        "神经网络", settings=knowledge_settings, with_images=False
    )
    assert result.ok, result.error
    assert len(calls) == 3, "文章(截断) + 文章(重试) + 平台"
    assert "篇幅必须短" not in calls[0], "第一次是正常请求"
    assert "篇幅必须短" in calls[1], "第二次必须要求更短"
    assert result.steps["article_retry"]["tokens"] == 8000
    assert result.article["title"] == GOOD_ARTICLE["title"]


@pytest.mark.asyncio
async def test_truncated_twice_fails_with_an_actionable_message(
    knowledge_settings, monkeypatch
):
    """两次都截断就如实失败，并明确说该改哪个设置——不要留一个看不懂的原始错误。"""
    from app.services import knowledge_service
    from app.services.ai.deepseek import DeepSeekTruncatedError
    import app.services.ai.deepseek as deepseek_module

    class AlwaysTruncated(StubDeepSeekClient):
        async def complete_json(self, *, system, user, **kwargs):
            raise DeepSeekTruncatedError(
                "cut off", completion_tokens=8000, finish_reason="length"
            )

    monkeypatch.setattr(
        deepseek_module, "DeepSeekClient", lambda settings: AlwaysTruncated([])
    )
    result = await knowledge_service.generate_article(
        "神经网络", settings=knowledge_settings, with_images=False
    )
    assert not result.ok
    assert "两次都被截断" in result.error
    assert "ARTICLE_MAX_TOKENS" in result.error
    assert "只写 3 节" in result.error


@pytest.mark.asyncio
async def test_invalid_json_is_retried_once_then_fails(knowledge_settings, monkeypatch):
    """**回归测试，纠正了我自己上一版的错误判断。**

    我原先断言"JSON 语法错误重试没意义"并写进了测试——那是把它当成确定性问题。
    实测证据：同一个主题「神经网络」失败一次，重新调用就正常解析了（temperature 0.7
    是随机采样）。所以**必须重试一次**，并要求更严格的 JSON 格式。
    """
    from app.services import knowledge_service
    from app.services.ai.deepseek import DeepSeekJSONError
    import app.services.ai.deepseek as deepseek_module

    calls: list[str] = []

    class AlwaysBroken(StubDeepSeekClient):
        async def complete_json(self, *, system, user, **kwargs):
            calls.append(user)
            raise DeepSeekJSONError("字符串里有未转义的引号")

    monkeypatch.setattr(deepseek_module, "DeepSeekClient", lambda settings: AlwaysBroken([]))
    result = await knowledge_service.generate_article(
        "神经网络", settings=knowledge_settings, with_images=False
    )
    assert not result.ok
    assert len(calls) == 2, "应当重试一次（且只一次）"
    assert "未转义的双引号" in calls[1], "重试时要明确要求更严格的 JSON"
    assert "两次都不是合法 JSON" in result.error
    assert "未转义的引号" in result.error, "两次的原始错误都要保留，便于定位"


@pytest.mark.asyncio
async def test_a_retry_that_succeeds_saves_the_article(knowledge_settings, monkeypatch):
    """第一次 JSON 坏了、第二次好了——那就该把文章存下来，而不是报错。"""
    from app.services import knowledge_service
    from app.services.ai.deepseek import DeepSeekJSONError
    import app.services.ai.deepseek as deepseek_module

    class BrokenOnce(StubDeepSeekClient):
        def __init__(self):
            super().__init__([GOOD_ARTICLE, GOOD_PLATFORMS])
            self.first = True

        async def complete_json(self, *, system, user, **kwargs):
            if self.first:
                self.first = False
                raise DeepSeekJSONError("第一次的 JSON 坏了")
            return await super().complete_json(system=system, user=user, **kwargs)

    monkeypatch.setattr(deepseek_module, "DeepSeekClient", lambda settings: BrokenOnce())
    result = await knowledge_service.generate_article(
        "神经网络", settings=knowledge_settings, with_images=False
    )
    assert result.ok, result.error
    assert result.steps["article_retry"]["reason"] == "invalid_json"
    assert result.article["title"] == GOOD_ARTICLE["title"]

"""Phase 10: domain relevance ranking and README -> promotion copy.

No test here touches the network or spends money: the promotion generator is exercised
through a stub DeepSeek client, and the digest is pure text processing.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.db.database import session_scope
from app.models.hot_content import ContentType, HotContent, Platform
from app.models.hot_content import ContentOrigin, HotContentRecord, MediaBundle, MediaImage
from app.services.ai.promo import digest_readme, readme_fingerprint
from app.services.pipeline.interest import (
    InterestProfile,
    relevance_summary,
    score_item,
    select_relevant_candidates,
)


def _item(title: str, *, body: str = "", hot: int = 100, rank: int = 1, platform: str = "weibo"):
    return HotContent(
        id=f"{platform}:{title}",
        platform=Platform(platform),
        platform_content_id=title,
        title=title,
        description=body,
        hot_value=hot,
        rank=rank,
        content_type=ContentType.TOPIC,
    )


PROFILE = InterestProfile(
    keywords=("人工智能", "AI", "编程", "代码", "程序员", "大学生", "数码", "毕业季"),
    negatives=("明星", "八卦", "恋情"),
    only=True,
)


# ------------------------------------------------------------- interest scoring
def test_relevance_beats_heat_when_choosing_candidates():
    """The whole point: the paid step must see the relevant items, not the hottest.

    Before this, selection was pure ``hot_value`` order, so an account about technology
    and programming paid to analyse whatever was trending.
    """
    items = [
        _item("明星恋情曝光引发热议", hot=9_000_000, rank=1),
        _item("AI 编程助手横评", hot=1_200, rank=40),
        _item("大学生必看的编程入门路线", hot=900, rank=55),
        _item("某综艺最新一期", hot=8_000_000, rank=2),
    ]
    chosen = select_relevant_candidates(items, 2, profile=PROFILE)
    titles = [item.title for item in chosen]
    assert "AI 编程助手横评" in titles
    assert "大学生必看的编程入门路线" in titles
    assert not any("综艺" in title or "恋情" in title for title in titles)


def test_interest_only_drops_irrelevant_items():
    items = [_item("明星八卦"), _item("AI 工具推荐")]
    assert len(select_relevant_candidates(items, 10, profile=PROFILE)) == 1

    permissive = InterestProfile(keywords=PROFILE.keywords, negatives=PROFILE.negatives, only=False)
    # With only=False nothing is dropped, it is merely ranked lower.
    assert len(select_relevant_candidates(items, 10, profile=permissive)) == 2


def test_platform_balance_is_kept():
    """One platform's 300-item board must not crowd out the others."""
    items = [
        _item("AI 新闻一", platform="weibo", hot=5000),
        _item("AI 新闻二", platform="weibo", hot=4000),
        _item("AI 新闻三", platform="weibo", hot=3000),
        _item("AI 工具", platform="xiaohongshu", hot=100),
    ]
    chosen = select_relevant_candidates(items, 2, profile=PROFILE)
    assert {item.platform.value for item in chosen} == {"weibo", "xiaohongshu"}


def test_scoring_is_explainable_and_title_weighted():
    title_hit = score_item(_item("AI 编程", body=""), PROFILE)
    body_hit = score_item(_item("无关标题", body="讲的是 AI 编程"), PROFILE)
    assert title_hit.score > body_hit.score, "a title match is worth more than a body match"
    assert "AI" in title_hit.matched and "编程" in title_hit.matched
    assert title_hit.is_relevant and not score_item(_item("完全无关"), PROFILE).is_relevant


def test_negative_keywords_lower_the_score():
    score = score_item(_item("明星恋情曝光"), PROFILE)
    assert score.score < 0 and score.negative


def test_no_keywords_configured_behaves_like_before():
    """An unconfigured profile must not silently change what the paid step sees."""
    empty = InterestProfile(keywords=(), negatives=(), only=False)
    items = [_item("A", hot=10, rank=2), _item("B", hot=100, rank=1)]
    chosen = select_relevant_candidates(items, 2, profile=empty)
    assert [item.title for item in chosen] == ["B", "A"], "falls back to heat order"


def test_relevance_summary_counts_coverage():
    items = [_item("AI 工具"), _item("娱乐新闻"), _item("大学生数码")]
    summary = relevance_summary(items, PROFILE)
    assert summary["considered"] == 3
    assert summary["relevant"] == 2
    assert 0.6 < summary["coverage"] < 0.7
    assert summary["top_keywords"]


# --------------------------------------------------------------- readme digest
SAMPLE_README = """# MyTool — 一个示例项目

MyTool 是一个帮助学生整理课程笔记的小工具，支持 Markdown 与导出 PDF。

![badge](https://img.shields.io/badge/x-y)

## 功能

- 一键导入 Markdown 笔记
- 按课程自动分类
- 导出为 PDF 与图片

## 安装

```bash
pip install mytool
```

## 使用

```python
from mytool import Notebook
nb = Notebook("数学")
nb.import_markdown("chapter1.md")
```

## 常见问题

请勿提交 issue 询问安装问题，先看文档。
"""


def test_digest_extracts_structure_and_strips_badges():
    digest = digest_readme(SAMPLE_README)
    assert digest.project_name == "MyTool", "the subtitle after the separator is dropped"
    assert "帮助学生整理课程笔记" in digest.pitch
    assert any("功能" == heading for heading in digest.headings)
    assert any("一键导入 Markdown" in bullet for bullet in digest.bullets)
    assert any("pip install mytool" in hint for hint in digest.code_hints)
    rendered = digest.render()
    assert "img.shields.io" not in rendered, "badges are noise, not content"
    assert "shields" not in rendered.lower()


def test_long_readme_is_bounded_and_says_so():
    """A promotional prompt must not silently present a truncated view as the whole."""
    huge = "# Big\n\n简介。\n\n" + "\n\n".join(
        f"## 章节 {index}\n" + ("这是一个很长的段落。" * 200) for index in range(40)
    )
    digest = digest_readme(huge)
    assert digest.total_chars > 50_000, "the sample is genuinely long"
    assert digest.sent_chars <= 13_000, "the digest stays near the cap"
    assert digest.truncated, "it must know it was truncated"
    assert digest.notes, "and say so in the notes"
    rendered = digest.render()
    assert "节选" in rendered and "不要" in rendered, "the model is told what was cut"


def test_empty_readme_is_reported_not_crashed():
    digest = digest_readme("   ")
    assert digest.project_name == ""
    assert digest.notes and "空" in digest.notes[0]


def test_fingerprint_is_stable():
    assert readme_fingerprint("abc") == readme_fingerprint("abc")
    assert readme_fingerprint("abc") != readme_fingerprint("abd")


# ------------------------------------------------------------ promo generation
class StubDeepSeek:
    """A DeepSeek stand-in returning one canned promotion payload."""

    def __init__(self, payload: dict, *, finish_reason: str = "") -> None:
        from app.services.ai.deepseek import ChatResult, ChatUsage

        self._payload = payload
        self.model = "deepseek-flash"
        self.finish_reason = finish_reason
        self.calls: list[dict] = []
        self.usage = ChatUsage(prompt_tokens=3000, completion_tokens=900, total_tokens=3900)
        self.total_usage = self.usage
        self._ChatResult = ChatResult

    async def complete_json(self, *, system: str, user: str, **kwargs):
        import json

        self.calls.append({"system": system, "user": user, **kwargs})
        return self._payload, self._ChatResult(
            content=json.dumps(self._payload, ensure_ascii=False),
            model=self.model,
            usage=self.usage,
            finish_reason=self.finish_reason,
        )

    async def aclose(self) -> None:  # pragma: no cover
        return None


PROMO_PAYLOAD = {
    "project_name": "MyTool",
    "one_liner": "帮学生整理课程笔记的小工具",
    "cover_text": "笔记终于不用手抄了",
    "versions": {
        "xiaohongshu": {
            "title": "期末笔记救星",
            "content": "正文内容" * 40,
            "ending": "你怎么整理笔记？",
            "hashtags": ["#大学生#", "#效率工具#"],
        },
        "weibo": {"opening": "发现一个笔记工具", "content": "正文" * 30, "hashtags": ["#效率#"]},
        "douyin": {
            "hook": "期末周必备",
            "script": "口播" * 60,
            "scenes": ["录屏导入", "分类效果"],
            "cues": "字幕要点",
            "cta": "关注我",
        },
    },
    "source_points": ["支持 Markdown 导入", "可导出 PDF"],
    "unknowns": ["README 没写支持哪些平台"],
    "image_ideas": ["截导入界面", "截导出效果"],
}


@pytest.mark.asyncio
async def test_generate_promo_produces_three_versions(sqlite_db, monkeypatch):
    from app.services.ai.promo_service import generate_promo

    client = StubDeepSeek(PROMO_PAYLOAD)
    run = await generate_promo(text=SAMPLE_README, client=client, store=True)

    assert run.ok, run.error
    assert run.record_id, "the promotion should be stored"
    versions = run.result["versions"]
    assert set(versions) == {"xiaohongshu", "weibo", "douyin"}, "three platforms, as asked"
    assert versions["xiaohongshu"]["title"] and versions["douyin"]["script"]
    assert run.result["unknowns"], "what the README did not say is surfaced"
    assert run.tokens["total_tokens"] == 3900
    assert run.estimated_cny > 0
    # The prompt must carry the no-invention rules and the digest, not the raw file.
    prompt = client.calls[0]["user"]
    assert "不许编造" in prompt
    assert "shields" not in prompt, "badges are not sent"


@pytest.mark.asyncio
async def test_generate_promo_reports_truncated_output(sqlite_db):
    """A cut-off answer must be reported, never stored as if it were complete."""
    from app.services.ai.promo_service import generate_promo

    run = await generate_promo(
        text=SAMPLE_README, client=StubDeepSeek(PROMO_PAYLOAD, finish_reason="length")
    )
    assert not run.ok
    assert "truncated" in run.error
    assert run.record_id is None


@pytest.mark.asyncio
async def test_generate_promo_needs_exactly_one_source():
    from app.services.ai.promo_service import generate_promo, load_readme

    with pytest.raises(ValueError):
        load_readme(text="a", path="b")
    with pytest.raises(ValueError):
        load_readme()

    run = await generate_promo(client=StubDeepSeek(PROMO_PAYLOAD))
    assert not run.ok and "exactly one" in run.error


def test_load_readme_refuses_unexpected_file_types(tmp_path):
    """A path is only accepted for document-like files: reading arbitrary files into a
    prompt is a way to leak a private key.

    The first version put ``""`` in the allowed-suffix tuple, which accepted **every
    extension-less file** — ``id_rsa``, ``.env``, ``.git-credentials`` — because
    ``Path(x).suffix`` is ``""`` for all of them. The guard looked present and did
    nothing.
    """
    from app.services.ai.promo_service import load_readme

    for name in ("id_rsa", ".env", ".git-credentials", "secrets"):
        secret = tmp_path / name
        secret.write_text("PRIVATE KEY MATERIAL", encoding="utf-8")
        with pytest.raises(ValueError):
            load_readme(path=str(secret))

    # A real extension-less README is still accepted.
    bare = tmp_path / "README"
    bare.write_text("# ok", encoding="utf-8")
    assert load_readme(path=str(bare))[0] == "# ok"

    good = tmp_path / "README.md"
    good.write_text("# ok", encoding="utf-8")
    content, kind, name = load_readme(path=str(good))
    assert content == "# ok" and kind == "path" and name.endswith("README.md")


def test_shipped_defaults_are_domain_focused():
    """The default configuration ships with filtering on and the watch stage enabled.

    Asserted here because ``tests/conftest.py`` deliberately turns ``interest_only`` off
    so unrelated tests are not coupled to the keyword list — which means nothing else
    would catch a change to the shipped default.
    """
    from app.core.config import Settings

    # An empty environment: what a fresh install gets.
    fresh = Settings(
        tikhub_api_key="",
        database_url="",
        deepseek_api_key="",
    )
    assert fresh.interest_only is True
    assert fresh.interest_keyword_list, "the default keyword list must not be empty"
    assert "编程" in fresh.interest_keyword_list
    assert fresh.watch_search_enabled is True
    assert fresh.watch_keyword_list
    # The cost of one watch pass is exactly keywords x platforms, and it is exposed.
    assert fresh.watch_billed_calls_per_run == len(fresh.watch_keyword_list) * len(
        fresh.watch_platform_list
    )


def test_html_input_is_refused_before_any_paid_call():
    """A real incident: a 小红书 share link was pasted into the URL field.

    It redirects to a **login page**. The loader digested 35,865 characters of HTML/JS
    down to 417 characters of noise and then paid for a generation whose only possible
    answer was "the README does not say". The model behaved correctly — the input should
    never have been sent.
    """
    from app.services.ai.promo_service import html_title, looks_like_html

    login_page = (
        "<!doctype html><html><head><title>小红书 - 你的生活兴趣社区</title>"
        "<script>function e(e){for(var r=1;r<e;r++){}}</script></head><body></body></html>"
    )
    assert looks_like_html(login_page)
    assert looks_like_html("just text", "text/html; charset=utf-8")
    assert html_title(login_page) == "小红书 - 你的生活兴趣社区"

    # Real document payloads are not mistaken for web pages.
    assert not looks_like_html("# MyTool\n\nA tool.", "text/plain; charset=utf-8")
    assert not looks_like_html(SAMPLE_README, "text/markdown")

    with pytest.raises(ValueError) as excinfo:
        from app.services.ai.promo_service import load_readme

        load_readme(text=login_page)
    assert "网页" in str(excinfo.value)


@pytest.mark.asyncio
async def test_url_returning_a_web_page_is_refused_and_not_billed(monkeypatch):
    """The refusal must happen before the model call, so nothing is spent."""
    import httpx
    import respx

    from app.services.ai.promo_service import generate_promo

    login_page = "<!doctype html><html><head><title>登录</title></head><body>请登录</body></html>"
    with respx.mock:
        respx.get("https://xhslink.cn/o/abc").mock(
            return_value=httpx.Response(
                200, html=login_page, headers={"content-type": "text/html; charset=utf-8"}
            )
        )
        client = StubDeepSeek(PROMO_PAYLOAD)
        run = await generate_promo(url="https://xhslink.cn/o/abc", client=client, store=False)

    assert not run.ok
    assert "网页" in run.error
    assert client.calls == [], "no model call may happen for a non-README URL"
    assert run.record_id is None


@pytest.mark.asyncio
async def test_contentless_digest_is_refused_and_not_billed():
    """The general gate: no usable structure means nothing to promote."""
    from app.services.ai.promo_service import generate_promo

    client = StubDeepSeek(PROMO_PAYLOAD)
    # A blob with no headings, bullets, pitch or code — e.g. a minified script.
    run = await generate_promo(text="var a=1;" * 40, client=client, store=False)

    assert not run.ok
    assert "没有可识别的 README 结构" in run.error
    assert "没有产生费用" in run.error
    assert run.status == 422, "an input problem must not be reported as a provider failure"
    assert client.calls == []
    assert run.record_id is None


# ----------------------------------------------- style reference (a 小红书 note)
def test_note_target_parsing_is_pure_and_covers_the_real_encodings():
    """Parsing is separated from fetching so it can be tested without the network.

    The suite once fetched ``example.com`` and only passed while that host answered —
    this project promises its tests are offline. The double encoding is real: the token
    sits inside an encoded ``redirectPath`` and ends ``%253D`` for ``=``.
    """
    from app.services.ai.style_reference import parse_note_target

    note_id, token = parse_note_target(
        "https://www.xiaohongshu.com/explore/6aa377f60000000028036b7a?xsec_token=ABC123%3D"
    )
    assert note_id == "6aa377f60000000028036b7a"
    assert token == "ABC123=", "percent-decoding must be applied"

    # The real share-link shape: a login redirect whose redirectPath carries both.
    wrapped = (
        "https://www.xiaohongshu.com/login?redirectPath=http%3A%2F%2Fwww.xiaohongshu.com"
        "%2Fdiscovery%2Fitem%2F6aa377f60000000028036b7a%3Ftype%3Dnormal%26"
        "xsec_token%3DCB9zFkdOFLsQ%253D"
    )
    note_id, token = parse_note_target(wrapped)
    assert note_id == "6aa377f60000000028036b7a"
    assert token.startswith("CB9zFkdOFLsQ")

    assert parse_note_target("https://example.com/nope") == ("", "")


def test_share_link_uses_the_cached_form_without_a_network_call():
    """A complete URL must be answered with no fetch at all."""
    from app.services.ai.style_reference import resolve_note_link

    parsed = resolve_note_link(
        "https://www.xiaohongshu.com/explore/6aa377f60000000028036b7a?xsec_token=ABC123%3D"
    )
    assert parsed.resolved and parsed.note_id and parsed.xsec_token
    assert parsed.final_url.endswith("ABC123%3D"), "no redirect was followed"


def test_a_link_without_a_token_is_refused_before_spending():
    """Without the token the detail endpoint cannot succeed, so say so rather than pay."""
    import httpx
    import respx

    from app.services.ai.style_reference import resolve_note_link

    with respx.mock:
        # Mocked, so the suite stays offline.
        respx.get("https://www.xiaohongshu.com/discovery/item/6aa377f60000000028036b7a").mock(
            return_value=httpx.Response(200, html="<html><title>x</title></html>")
        )
        parsed = resolve_note_link(
            "https://www.xiaohongshu.com/discovery/item/6aa377f60000000028036b7a"
        )
    assert not parsed.resolved
    assert parsed.note_id, "the id was found"
    assert "xsec_token" in parsed.error

    with respx.mock:
        respx.get("https://example.com/not-a-note").mock(
            return_value=httpx.Response(200, html="<html><title>example</title></html>")
        )
        junk = resolve_note_link("https://example.com/not-a-note")
    assert not junk.resolved and "笔记 id" in junk.error
    assert not resolve_note_link("").resolved


def test_style_block_forbids_copying_content():
    """The reference supplies the *form*; copying a stranger's post would be plagiarism."""
    from app.services.ai.style_reference import StyleSample, render_style_block

    sample = StyleSample(
        note_id="abc",
        title="我用这个方法三个月涨粉一万",
        body="第一段。\n第二段。",
        hashtags=["#编程#", "#大学生#"],
        author="某作者",
        image_count=6,
    )
    block = render_style_block(sample)
    assert "风格参考笔记" in block
    assert "绝对不要复制" in block
    assert "学的是**形式**" in block
    assert "不要把它的作者经历" in block
    assert sample.as_dict()["paragraphs"] == 2


def test_style_sample_keeps_title_and_body_separate():
    """The detail stage joins them into quotable source; a style sample must not.

    The structure is the point: how long the title is against how the body runs.
    """
    from app.services.ai.style_reference import NoteLink, style_sample_from_payload

    payload = {
        "data": {
            "data": {
                "items": [
                    {
                        "note_card": {
                            "title": "标题",
                            "desc": "正文内容",
                            "type": "normal",
                            "tag_list": [{"name": "编程"}, {"name": "大学生"}],
                            "user": {"nickname": "作者"},
                            "interact_info": {"liked_count": "1.2万"},
                            "image_list": [{"url_default": "https://x/1.jpg"}, {"url": "https://x/2.jpg"}],
                        }
                    }
                ]
            }
        }
    }
    sample = style_sample_from_payload(payload, link=NoteLink(note_id="abc"))
    assert sample.title == "标题"
    assert sample.body == "正文内容"
    assert sample.hashtags == ["编程", "大学生"]
    assert sample.likes == 12000
    assert sample.image_count == 2
    assert sample.url.endswith("/explore/abc")


@pytest.mark.asyncio
async def test_unusable_style_link_costs_nothing():
    """A style link that cannot be used must fail before the DeepSeek call."""
    from app.services.ai.promo_service import generate_promo

    client = StubDeepSeek(PROMO_PAYLOAD)
    run = await generate_promo(
        text=SAMPLE_README,
        style_url="https://example.com/definitely-not-a-note",
        client=client,
        store=False,
    )
    assert not run.ok
    assert "风格参考笔记不可用" in run.error
    assert "没有产生费用" in run.error
    assert run.status == 422
    assert client.calls == [], "no model call when the style reference failed"
    assert run.billed_calls == 0, "and no TikHub call either"


def test_style_reference_prompt_reaches_the_model():
    """When a style sample is supplied, the prompt carries it and its no-copy rules."""
    from app.services.ai.promo import build_promo_prompt
    from app.services.ai.style_reference import StyleSample, render_style_block

    digest = digest_readme(SAMPLE_README)
    block = render_style_block(StyleSample(title="参考标题", body="参考正文。"))
    prompt = build_promo_prompt(digest, style_block=block)
    assert "风格参考笔记" in prompt
    assert "绝对不要复制" in prompt

    without = build_promo_prompt(digest)
    assert "风格参考笔记" not in without


# ------------------------------------------------------- manual runs are recorded
@pytest.mark.asyncio
async def test_manual_search_is_recorded_as_a_task(sqlite_db, settings):
    """A billed manual search must appear on the 任务记录 page.

    Before this, ``POST /api/hot/search`` and ``POST /api/hot/watch`` spent real money
    and created **no task row**, so the spend could only be inferred from the balance.
    """
    from app.db.repository import list_tasks
    from app.services.pipeline.task_recorder import run_as_task

    async def work() -> dict:
        return {"billed_calls": 3, "inserted": 12, "errors": {}}

    recorded = await run_as_task(
        task_type="search", step_name="search", work=work, settings=settings
    )
    assert recorded.task_id
    assert recorded.status == "success"

    async with session_scope(settings) as session:
        rows, total = await list_tasks(session, limit=10)
    assert total == 1
    task = rows[0]
    assert task.task_type == "search"
    assert task.status == "success"
    assert task.steps and task.steps[0]["name"] == "search"
    assert task.steps[0]["billed"] is True
    assert task.summary["billed_calls"] == 3
    assert task.duration_ms is not None


@pytest.mark.asyncio
async def test_a_failed_manual_search_is_recorded_and_re_raised(sqlite_db, settings):
    """The failure must reach the caller *and* leave a permanent record."""
    from app.db.repository import list_tasks
    from app.services.pipeline.task_recorder import run_as_task

    async def work() -> dict:
        raise RuntimeError("provider exploded")

    with pytest.raises(RuntimeError):
        await run_as_task(
            task_type="watch", step_name="watch", work=work, settings=settings
        )

    async with session_scope(settings) as session:
        rows, _total = await list_tasks(session, limit=10)
    assert rows and rows[0].status == "failed"
    assert "provider exploded" in (rows[0].error_message or "")


@pytest.mark.asyncio
async def test_a_partial_manual_search_is_marked_partial(sqlite_db, settings):
    """One platform failing makes the run partial, not success."""
    from app.db.repository import list_tasks
    from app.services.pipeline.task_recorder import run_as_task

    async def work() -> dict:
        return {"billed_calls": 1, "errors": {"douyin": "boom"}}

    recorded = await run_as_task(
        task_type="search", step_name="search", work=work, settings=settings
    )
    assert recorded.status == "partial"

    async with session_scope(settings) as session:
        rows, _total = await list_tasks(session, limit=5)
    assert rows[0].status == "partial"


# -------------------------------------------------- the collected/analysed gap
@pytest.mark.asyncio
async def test_pending_analysis_reports_the_gap(sqlite_db, settings, item_factory):
    """The operator's question: 191 rows collected, "未分析" down the whole column.

    The analysis was not broken — nothing had run it, because a manual search only
    collects and the ``analyze`` stage lives in the pipeline. This endpoint makes the
    gap visible and states what a run would cost, without spending anything.
    """
    from app.services.pipeline.analysis_gap import pending_analysis
    from app.services.pipeline.hot_pipeline import store_items

    items = [
        item_factory("weibo", "w1", "Python 零基础入门路线"),      # interest match
        item_factory("weibo", "w2", "某明星恋情曝光"),              # filtered out
        item_factory("douyin", "d1", "大学生数码好物推荐"),          # interest match
    ]
    async with session_scope(settings) as session:
        await store_items(items, settings=settings, session=session)

    # The shared settings fixture turns interest filtering off, so enable it here: this
    # test is about what the filter does to the pending count.
    filtering = settings.model_copy(update={"interest_only": True})
    pending = await pending_analysis(filtering)

    assert pending.stored_rows == 3
    assert pending.analysed_rows == 0
    assert pending.pending_rows == 3
    assert pending.eligible_rows == 2, "the celebrity item must not be eligible"
    assert pending.would_be_sent == 2
    assert pending.estimated_cny > 0
    assert pending.note
    # The most relevant items are offered first.
    assert pending.sample[0]["interest_score"] > 0


@pytest.mark.asyncio
async def test_pending_analysis_is_free(sqlite_db, settings, monkeypatch):
    """It must not call DeepSeek: it is the *check* before spending."""
    from app.services.pipeline import analysis_gap

    def explode(*_args, **_kwargs):  # pragma: no cover - only runs on a bug
        raise AssertionError("pending_analysis must not call the model")

    monkeypatch.setattr(analysis_gap, "run_analysis_after_collection", explode)
    pending = await analysis_gap.pending_analysis(settings)
    assert pending.stored_rows == 0
    assert pending.note == "所有已入库内容都已分析过"


# ------------------------------------------------- per-item analysis / rewrite
@pytest.mark.asyncio
async def test_analysing_one_item_bypasses_the_interest_filter(sqlite_db, settings, item_factory):
    """Clicking "AI 分析" on an item must analyse *that* item.

    The interest filter exists to stop the batch path paying for irrelevant content; it
    must not overrule an explicit choice, and the candidate cap must not either.
    """
    from app.db.repository import list_analyses
    from app.services.ai.analyzer import run_analysis
    from app.services.pipeline.hot_pipeline import store_items
    from tests.test_rewriter import StubDeepSeekClient, _analysis_payload

    # "完全无关的标题" matches no interest keyword.
    item = item_factory("weibo", "w1", "完全无关的标题")
    async with session_scope(settings) as session:
        await store_items([item], settings=settings, session=session)

    filtering = settings.model_copy(update={"interest_only": True})
    client = StubDeepSeekClient([{"results": [_analysis_payload(0)]}])
    result = await run_analysis(settings=filtering, client=client, only_ids=[1])

    assert result.analysed == 1, "the explicit item is analysed despite the filter"
    assert result.interest["bypassed"] is True
    async with session_scope(filtering) as session:
        rows, total = await list_analyses(session)
    assert total == 1 and rows[0][0].hot_content_id == 1


@pytest.mark.asyncio
async def test_rewriting_one_item_does_not_require_selection(sqlite_db, settings, item_factory):
    """The "二创" button works on an item the batch flow never selected — but only when
    an analysis exists, because the prompt is built from it."""
    from app.db.repository import list_analyses, list_rewrites
    from app.services.ai.analyzer import run_analysis
    from app.services.ai.rewriter import run_rewriting
    from app.services.pipeline.hot_pipeline import store_items
    from tests.test_rewriter import StubDeepSeekClient, _analysis_payload, _rewrite_payload

    item = item_factory("weibo", "w1", "某个话题")
    async with session_scope(settings) as session:
        await store_items([item], settings=settings, session=session)

    # No analysis yet: the rewriter must say so rather than silently do nothing.
    empty = await run_rewriting(
        settings=settings, client=StubDeepSeekClient([]), only_ids=[1]
    )
    assert empty.considered == 0
    assert any("no analysis" in error for error in empty.errors), empty.errors

    # Analyse it, then clear `selected` so the batch flow would skip it.
    await run_analysis(
        settings=settings, client=StubDeepSeekClient([{"results": [_analysis_payload(0)]}])
    )
    async with session_scope(settings) as session:
        for analysis, _item in (await list_analyses(session))[0]:
            analysis.selected = False

    result = await run_rewriting(
        settings=settings,
        client=StubDeepSeekClient([_rewrite_payload(needs_verification=False)]),
        only_ids=[1],
    )
    assert result.considered == 1 and result.rewritten == 1
    async with session_scope(settings) as session:
        rewrites, total = await list_rewrites(session)
    assert total == 1


@pytest.mark.asyncio
async def test_one_item_analysis_response_is_serialisable(sqlite_db, settings, item_factory):
    """The endpoint must not 500 *after* the tokens were spent.

    ``analysis_for_content`` returns ``(analysis, item)``; the first version unpacked that
    tuple as if it were the analysis and crashed on the response, having already paid for
    the call. This asserts the shape the route depends on.
    """
    from app.db.repository import analysis_for_content
    from app.services.ai.analyzer import run_analysis
    from app.services.pipeline.hot_pipeline import store_items
    from tests.test_rewriter import StubDeepSeekClient, _analysis_payload

    item = item_factory("weibo", "w1", "某个话题")
    async with session_scope(settings) as session:
        await store_items([item], settings=settings, session=session)
    await run_analysis(
        settings=settings, client=StubDeepSeekClient([{"results": [_analysis_payload(0)]}])
    )

    async with session_scope(settings) as session:
        found = await analysis_for_content(session, 1)

    assert found is not None
    assert isinstance(found, tuple) and len(found) == 2, (
        "the route unpacks this as (analysis, item); treating it as one object 500s"
    )
    analysis, _source = found
    assert analysis.hot_content_id == 1


def test_every_model_module_is_registered_with_alembic():
    """A new model file not imported by ``app.models`` produces a *silently empty*
    migration.

    This actually happened while building Phase 10: autogenerate reported nothing, the
    migration body was ``pass``, and the table was never created — with no error at any
    point. The registry in ``app/models/__init__.py`` is what Alembic reads, so it is
    asserted here instead of trusted.
    """
    import importlib
    import pkgutil

    import app.models as models_package
    from app.db.database import Base

    modules = [
        name
        for _finder, name, _ispkg in pkgutil.iter_modules(models_package.__path__)
        if name != "__init__"
    ]
    assert modules, "expected model modules"

    registered_tables = set(Base.metadata.tables)
    for name in modules:
        module = importlib.import_module(f"app.models.{name}")
        declared = {
            value.__tablename__
            for value in vars(module).values()
            if hasattr(value, "__tablename__") and hasattr(value, "__table__")
        }
        assert declared, f"{name} declares no table; is it a model module?"
        missing = declared - registered_tables
        assert not missing, (
            f"{name} declares {missing} but app/models/__init__.py does not import it, "
            "so Alembic autogenerate will silently skip it"
        )

"""知识科普文章的生成提示词与解析（Phase 13）。

面向**大学生计算机大类**。这不只是"换个受众"的改写：学生读者需要的是**能建立直觉的
解释**与**可以照着做的东西**，所以提示词里明确要求比喻、要求指出常见误解、要求给出可
执行的小练习，而不是堆术语。

三件与"贵"有关的设计：

* 文章、三平台文案、标签抽取是**三次独立调用**，各自记账，失败互不牵连（一次失败不会
  让已经付过钱的部分白费）；
* 标签抽取要求 30-50 个，**一次调用产出整个词表**，而不是每个标签一次；
* 三平台文案复用同一篇文章结论，不重新检索、不重新组织知识。

解析一律走 JSON 并做**字段级容错**：模型偶尔会漏字段或换个键名，缺的按空处理并记录，
而不是让整篇文章因为一个字段名不对而作废。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 文章正文分节数的上限与下限：太少撑不起一篇科普，太多会超出输出长度被截断。
MIN_SECTIONS = 3
MAX_SECTIONS = 6

ARTICLE_SYSTEM_PROMPT = """你是一位给**大学计算机大类本科生**写科普的作者。

读者画像：大一到大三，学过或正在学 C/Python、数据结构、计算机组成、操作系统；
看得懂代码但**缺乏工程直觉**；讨厌"名词解释堆砌"，喜欢"原来如此"的瞬间。

**输出必须是合法 JSON**（这一点经常出错，务必注意）：
字符串内部**不要出现真实换行**，需要分段就写 \\n；**不要出现未转义的双引号**，
要强调就用中文引号「」或把外层引号转义。宁可少写内容，也不要把 JSON 写坏。

写作要求：
1. 用**生活化的类比**建立直觉，再落到准确的定义。类比之后必须说明它在哪里不成立。
2. **主动指出常见误解**——学生最容易搞错的地方，明确写"很多人以为…其实…"。
3. 给**可执行的小练习或观察方法**，不是"建议多练习"这种空话。
4. 涉及数字、复杂度、性能时，如果无法确认就给量级与前提，**绝不编造精确数据**。
5. 代码示例只在能显著帮助理解时给，并保持极短（十几行以内）。
6. 不吹嘘、不标题党；不确定的地方明确说"这一点我不确定/取决于实现"。

只输出 JSON，不要任何解释文字或 Markdown 代码块围栏。字段如下：

{
  "title": "文章标题（不超过 30 字，说清讲什么，不要问句标题党）",
  "hook": "导语（80-150 字，从一个具体场景或困惑切入）",
  "audience": "面向人群（如 '大二、刚学完数据结构'）",
  "difficulty": "入门 | 进阶 | 高阶",
  "sections": [
    {"heading": "小标题", "body": "正文，150-400 字", "key_points": ["要点1", "要点2"]}
  ],
  "glossary": [{"term": "术语", "explanation": "一句话说清，30 字内"}],
  "takeaways": ["读完应该掌握什么，一句话一条"],
  "further_reading": [{"title": "延伸方向或实践建议", "note": "为什么值得做，一句话"}]
}

sections 给 3-6 个，glossary 给 4-8 个，takeaways 给 3-5 条。

**篇幅要克制**：整篇 JSON 必须能在输出上限内写完。宁可每节写得紧凑，也不要写到一半被截断——
被截断的 JSON 是完全不可用的。如果内容多，优先保证 3-4 节写完整。
"""

PLATFORM_SYSTEM_PROMPT = """你把一篇计算机科普文章改写成三个平台各自的帖子。

三个平台的写法**必须不同**，不要一份文案改标题糊弄过去：

* **小红书**：第一人称、口语、有情绪；标题 20 字内带钩子；正文分短段（1-3 行一段），
  可用少量 emoji；结尾有互动引导；标签 5-8 个，写进 hashtags 数组。
* **微博**：开门见山给结论或反常识点；正文 300 字内，节奏快；标签 3-5 个。
* **抖音**：视频脚本。hook 是前 3 秒的口播钩子；script 是分镜式口播稿；
  scenes 是画面建议（每条一句话）；subtitles 是字幕要点；cta 是结尾引导。

**所有平台的正文都要「带序号」并且「说人话」（重要，用户明确要求）：**

1. **用序号把内容拆成条目**，让人一眼看出讲了几个点：
   - 小红书 / 微博用 `1. 2. 3.` 或 `①②③`；
   - 抖音的 script 按 `1. 2. 3.` 分段口播，scenes 与 script 的序号**一一对应**。
2. **序号条目要能独立读懂**：先一句话说结论，再补一句为什么。
   不要写成"首先/其次/最后"这种看不出几点的流水句。
3. **通俗易懂**：用日常词讲清概念，必要术语第一次出现时**顺手解释半句**
   （例如"索引，可以理解成书的目录"）。
4. 每条控制在 1-3 行，**不要一大段文字**。
5. 全文条目数以 3-6 条为宜——太少显得空，太多没人看完。
6. 序号**贯穿全文**，不要开头列了号、后面又变成大段散文。

硬性要求：
1. **只依据给定文章里的内容**。文章没写的数字、案例、个人经历，一律不许编。
2. 面向大学生，不用"家人们""绝绝子"这类油腻话术，也不要装可爱。
3. 文章里标注"不确定"的地方，在文案里也要保留不确定性，不许说成定论。

只输出 JSON，不要解释文字或代码块围栏：

{
  "xiaohongshu": {"title": "", "content": "", "hashtags": [""]},
  "weibo": {"title": "", "content": "", "hashtags": [""]},
  "douyin": {"hook": "", "script": "", "scenes": [""], "subtitles": "", "cta": ""}
}
"""

TAG_SYSTEM_PROMPT = """你在为计算机大类学生构建一个**知识标签词表**，用于知识地图的标签球。

要求：
1. 输出 30-50 个标签，覆盖**不同粒度**：
   - 大类（如 数据结构、操作系统、计算机网络、数据库、编译原理）
   - 细分技术（如 B+树、页表、三次握手、垃圾回收、红黑树）
   - 编程语言与工具（如 Python、C、Git、Linux、Docker）
   - 数学与基础（如 离散数学、概率论、线性代数）
   - 工程与职业（如 代码review、实习面试、开源贡献、读文档）
2. **不要同义重复**（"操作系统"和"OS"只留一个）；不要过于宽泛（"编程"这类没有信息量）。
3. 每个标签给出分类 kind，取值必须是：基础理论 / 编程语言 / 系统网络 / 数据与AI /
   工程实践 / 数学基础 / 职业发展
4. difficulty 取值：入门 / 进阶 / 高阶。
5. blurb 用一句话（20 字内）说明这个标签讲什么，用于鼠标悬停显示。
6. weight 给 0.5-3.0 的数字，表示它有多核心（大类给高，细分给低）。

只输出 JSON，不要解释文字或代码块围栏：

{"tags": [{"name": "", "kind": "", "difficulty": "", "blurb": "", "weight": 1.0}]}
"""


@dataclass
class ArticleDraft:
    """解析后的文章草稿。"""

    title: str = ""
    hook: str = ""
    audience: str = ""
    difficulty: str = ""
    sections: list[dict[str, Any]] = field(default_factory=list)
    glossary: list[dict[str, Any]] = field(default_factory=list)
    takeaways: list[str] = field(default_factory=list)
    further_reading: list[dict[str, Any]] = field(default_factory=list)
    #: 解析时发现的问题（缺字段、类型不对），如实记录而不是静默吞掉。
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "hook": self.hook,
            "audience": self.audience,
            "difficulty": self.difficulty,
            "sections": self.sections,
            "glossary": self.glossary,
            "takeaways": self.takeaways,
            "further_reading": self.further_reading,
            "warnings": self.warnings,
        }


@dataclass
class TagDraft:
    """解析后的标签。"""

    name: str
    kind: str = ""
    difficulty: str = ""
    blurb: str = ""
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "difficulty": self.difficulty,
            "blurb": self.blurb,
            "weight": self.weight,
        }


def build_article_prompt(topic: str, *, extra: str = "", compact: bool = False) -> str:
    """文章生成请求。

    ``extra`` 让运营方补充要求（如"多讲代码示例"）。
    ``compact=True`` 用于**被截断后的重试**：明确要求更短的篇幅，因为同样的提示词再来
    一次大概率还是会被截断。
    """
    lines = [
        f"请写一篇关于「{topic.strip()}」的计算机科普文章，面向大学计算机大类本科生。",
        "重点是建立直觉、破除误解，而不是罗列定义。",
    ]
    if extra.strip():
        lines.append(f"额外要求：{extra.strip()}")
    if compact:
        lines.append(
            "**篇幅必须短**：sections 只给 3 个，每节正文 150 字以内；"
            "glossary 给 4 个、takeaways 给 3 条、further_reading 给 2 条。"
            "宁可少写，也必须把 JSON 写完——上一次的输出因为太长被截断了。"
        )
    return "\n".join(lines)


def build_platform_prompt(article: ArticleDraft, topic: str) -> str:
    """三平台改写请求：把文章的结构化内容交给模型，要求只依据它改写。"""
    body_parts: list[str] = [f"主题：{topic}", f"标题：{article.title}", f"导语：{article.hook}"]
    for index, section in enumerate(article.sections, start=1):
        body_parts.append(
            f"\n第{index}节 {section.get('heading', '')}\n{section.get('body', '')}"
        )
    if article.glossary:
        terms = "；".join(
            f"{item.get('term', '')}：{item.get('explanation', '')}" for item in article.glossary
        )
        body_parts.append(f"\n术语表：{terms}")
    if article.takeaways:
        body_parts.append("\n要点：" + "；".join(article.takeaways))
    body_parts.append("\n请按三个平台各自的写法改写。只使用上面的内容。")
    return "\n".join(body_parts)


def parse_article(payload: Any) -> ArticleDraft:
    """把模型返回的 JSON 转成 :class:`ArticleDraft`，字段级容错。"""
    draft = ArticleDraft()
    if not isinstance(payload, dict):
        draft.warnings.append(f"响应不是 JSON 对象：{type(payload).__name__}")
        return draft

    draft.title = _text(payload.get("title"))
    draft.hook = _text(payload.get("hook"))
    draft.audience = _text(payload.get("audience"))
    draft.difficulty = _text(payload.get("difficulty"))
    if not draft.title:
        draft.warnings.append("缺少 title")
    if not draft.hook:
        draft.warnings.append("缺少 hook")

    raw_sections = payload.get("sections")
    if isinstance(raw_sections, list):
        for entry in raw_sections:
            if not isinstance(entry, dict):
                continue
            heading = _text(entry.get("heading"))
            body = _text(entry.get("body"))
            if not (heading or body):
                continue
            points = entry.get("key_points")
            draft.sections.append(
                {
                    "heading": heading,
                    "body": body,
                    "key_points": [
                        _text(point) for point in points if _text(point)
                    ]
                    if isinstance(points, list)
                    else [],
                }
            )
    else:
        draft.warnings.append("缺少 sections")

    if len(draft.sections) > MAX_SECTIONS:
        draft.warnings.append(
            f"sections 有 {len(draft.sections)} 节，超过 {MAX_SECTIONS} 节，已截断"
        )
        draft.sections = draft.sections[:MAX_SECTIONS]
    if draft.sections and len(draft.sections) < MIN_SECTIONS:
        draft.warnings.append(f"sections 只有 {len(draft.sections)} 节，少于 {MIN_SECTIONS} 节")

    raw_glossary = payload.get("glossary")
    if isinstance(raw_glossary, list):
        for entry in raw_glossary:
            if isinstance(entry, dict):
                term = _text(entry.get("term"))
                explanation = _text(entry.get("explanation"))
                if term:
                    draft.glossary.append({"term": term, "explanation": explanation})

    raw_takeaways = payload.get("takeaways")
    if isinstance(raw_takeaways, list):
        draft.takeaways = [_text(item) for item in raw_takeaways if _text(item)]

    raw_reading = payload.get("further_reading")
    if isinstance(raw_reading, list):
        for entry in raw_reading:
            if isinstance(entry, dict):
                title = _text(entry.get("title"))
                if title:
                    draft.further_reading.append(
                        {"title": title, "note": _text(entry.get("note"))}
                    )
            elif isinstance(entry, str) and entry.strip():
                # 模型有时直接给字符串数组。
                draft.further_reading.append({"title": entry.strip(), "note": ""})

    return draft


def parse_platforms(payload: Any) -> dict[str, Any]:
    """解析三平台文案，缺的平台如实留空而不是伪造。"""
    result: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return result
    for platform in ("xiaohongshu", "weibo", "douyin"):
        node = payload.get(platform)
        if not isinstance(node, dict):
            continue
        if platform == "douyin":
            result[platform] = {
                "hook": _text(node.get("hook")),
                "script": _text(node.get("script")),
                "scenes": [
                    _text(item) for item in (node.get("scenes") or []) if _text(item)
                ]
                if isinstance(node.get("scenes"), list)
                else [],
                "subtitles": _text(node.get("subtitles")),
                "cta": _text(node.get("cta")),
            }
        else:
            hashtags = node.get("hashtags")
            result[platform] = {
                "title": _text(node.get("title")),
                "content": _text(node.get("content")),
                "hashtags": [
                    _text(tag) for tag in hashtags if _text(tag)
                ]
                if isinstance(hashtags, list)
                else [],
            }
    return result


def parse_tags(payload: Any, *, limit: int = 60) -> list[TagDraft]:
    """解析标签词表：去重（同名只留一个）、丢弃空名、按 weight 降序。"""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("tags")
    if not isinstance(raw, list):
        return []

    seen: dict[str, TagDraft] = {}
    for entry in raw:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue
        name = _text(entry.get("name"))
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            # 同名保留权重更高的那个（模型有时会给出重复项）。
            existing = seen[key]
            try:
                weight = float(entry.get("weight") or 1.0)
            except (TypeError, ValueError):
                weight = 1.0
            if weight > existing.weight:
                existing.weight = weight
            continue
        try:
            weight = float(entry.get("weight") or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        seen[key] = TagDraft(
            name=name,
            kind=_text(entry.get("kind")),
            difficulty=_text(entry.get("difficulty")),
            blurb=_text(entry.get("blurb")),
            weight=max(0.1, min(10.0, weight)),
        )
    ordered = sorted(seen.values(), key=lambda tag: -tag.weight)
    return ordered[:limit]


def _text(value: Any) -> str:
    """把任意值转成去空白的字符串。

    模型偶尔会返回数字或 None；``None`` 一旦漏到数据库或 JSON 里就会变成 null，
    在前端显示成 "None"。统一在入口处收敛。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


__all__ = [
    "ARTICLE_SYSTEM_PROMPT",
    "MAX_SECTIONS",
    "MIN_SECTIONS",
    "PLATFORM_SYSTEM_PROMPT",
    "TAG_SYSTEM_PROMPT",
    "ArticleDraft",
    "TagDraft",
    "build_article_prompt",
    "build_platform_prompt",
    "parse_article",
    "parse_platforms",
    "parse_tags",
]

"""README -> 小红书/微博/抖音 promotion copy (Phase 10).

The second thing this phase adds, and a different genre from the hot-topic rewriting:
here the source is *your own* project documentation and the goal is to get a student or
a fellow developer to try it.

Three decisions that shape the implementation, each for a concrete reason:

* **A README is digested, not pasted.** A real one runs to tens of thousands of
  characters, and paying to send all of it is waste: the interesting parts are the
  one-line pitch, the feature list, the tech stack and the quick-start. So the text is
  reduced to a bounded digest, and — crucially — the model is **told exactly what was
  cut** so it cannot present a truncated view as the whole project.
* **Nothing may be invented.** Star counts, benchmarks, user numbers and download
  figures are the usual filler in promotional copy and the fastest way to lose trust.
  The prompt forbids them outright, and anything numeric must come from the README.
* **It is promotional, not deceptive.** No fake "我踩了三个月的坑" backstory. The
  genre conventions 小红书 rewards (concrete benefit, short lines, a cover headline)
  are used honestly.

Cost: one DeepSeek call per generation. The digest cap is what keeps it predictable, and
the token usage is recorded per row exactly like the rewrite path.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: Hard ceiling on what is sent. ~12k characters is roughly 4-6k tokens of Chinese
#: and mixed technical text; larger READMEs are reduced rather than truncated blindly.
MAX_DIGEST_CHARS = 12_000
#: Per-section ceilings inside the digest, so one giant section cannot crowd out the
#: rest (a README with a 40-line changelog and a one-line feature list is common).
MAX_SECTION_CHARS = 1_400
MAX_HEADINGS = 40
MAX_BULLETS = 24


class PromoVersions(BaseModel):
    """The three platform versions, matching §17's shapes where they apply."""

    class Xiaohongshu(BaseModel):
        title: str = ""
        content: str = ""
        ending: str = ""
        hashtags: list[str] = Field(default_factory=list)

    class Weibo(BaseModel):
        opening: str = ""
        content: str = ""
        hashtags: list[str] = Field(default_factory=list)

    class Douyin(BaseModel):
        hook: str = ""
        script: str = ""
        scenes: list[str] = Field(default_factory=list)
        cues: str = ""
        cta: str = ""

    xiaohongshu: Xiaohongshu = Field(default_factory=Xiaohongshu)
    weibo: Weibo = Field(default_factory=Weibo)
    douyin: Douyin = Field(default_factory=Douyin)


class PromoResult(BaseModel):
    """One generated promotion set."""

    project_name: str = ""
    one_liner: str = ""
    #: Cover headline for the first 小红书 image (the click driver).
    cover_text: str = ""
    versions: PromoVersions = Field(default_factory=PromoVersions)
    #: Which parts of the README the copy leans on, so a reviewer can check the claim.
    source_points: list[str] = Field(default_factory=list)
    #: Things the model could not determine from the README (shown to the operator).
    unknowns: list[str] = Field(default_factory=list)
    #: Suggestions for what to screenshot, since no images exist yet.
    image_ideas: list[str] = Field(default_factory=list)


@dataclass
class ReadmeDigest:
    """The bounded view of a README that is actually sent."""

    project_name: str = ""
    pitch: str = ""
    headings: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    code_hints: list[str] = field(default_factory=list)
    sections: list[tuple[str, str]] = field(default_factory=list)
    total_chars: int = 0
    sent_chars: int = 0
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "headings": self.headings,
            "bullets": self.bullets[:12],
            "code_hints": self.code_hints,
            "total_chars": self.total_chars,
            "sent_chars": self.sent_chars,
            "truncated": self.truncated,
            "notes": self.notes,
        }

    def render(self) -> str:
        """The digest as prompt text, with the truncation stated."""
        parts: list[str] = []
        if self.project_name:
            parts.append(f"项目名：{self.project_name}")
        if self.pitch:
            parts.append(f"一句话简介（README 开头）：{self.pitch}")
        if self.headings:
            parts.append("章节结构：\n" + "\n".join(f"- {h}" for h in self.headings))
        if self.bullets:
            parts.append(
                "README 中的要点条目：\n" + "\n".join(f"- {b}" for b in self.bullets)
            )
        if self.code_hints:
            parts.append("命令/代码片段：\n" + "\n".join(f"  {c}" for c in self.code_hints))
        if self.sections:
            rendered = "\n\n".join(f"### {title}\n{body}" for title, body in self.sections)
            parts.append(f"正文节选：\n{rendered}")
        if self.truncated:
            parts.append(
                "⚠️ 注意：以上是**节选**，原始 README 更长"
                f"（原文 {self.total_chars} 字符，本次只送了 {self.sent_chars} 字符）。"
                "你不知道被省略的内容，**不要**猜测或补全任何没看到的功能、数据、性能指标。"
            )
        return "\n\n".join(parts)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
#: Badges, images and HTML are noise for a promotional post.
_NOISE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|<[^>]+>")


def _clean(text: str) -> str:
    return _NOISE_RE.sub("", text)


def _name_from_title(title: str) -> str:
    """Strip a subtitle from a document title: ``Foo — Phase 1`` -> ``Foo``."""
    for separator in (" — ", " – ", " | ", " - ", "：", ":"):
        if separator in title:
            head = title.split(separator, 1)[0].strip()
            if len(head) >= 2:
                return head[:80]
    return title[:80]


def _first_paragraph(lines: list[str]) -> str:
    """The first non-heading, non-badge paragraph: usually the pitch."""
    buffer: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if buffer:
                break
            continue
        if not stripped:
            if buffer:
                break
            continue
        buffer.append(stripped)
    return " ".join(buffer)[:400]


def digest_readme(text: str, *, project_name: str = "") -> ReadmeDigest:
    """Reduce a README to a bounded, structurally meaningful digest."""
    raw = (text or "").replace("\r\n", "\n")
    digest = ReadmeDigest(total_chars=len(raw))
    if not raw.strip():
        digest.notes.append("README 是空的")
        return digest

    cleaned = _clean(raw)
    lines = cleaned.split("\n")

    # Project name: the first H1, else the caller's hint. A title like
    # "SocialHot AI — Phase 1" (real example) is the project name plus a subtitle, and
    # the subtitle is not part of the name — so the part before a separator wins.
    for line in lines:
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            digest.project_name = _name_from_title(match.group(2).strip())
            break
    if not digest.project_name:
        digest.project_name = project_name

    digest.pitch = _first_paragraph(lines)

    headings: list[str] = []
    bullets: list[str] = []
    code_hints: list[str] = []
    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            title = match.group(2).strip()
            if title and title not in headings:
                headings.append(title[:80])
            continue
        match = _BULLET_RE.match(line)
        if match:
            entry = match.group(1).strip()
            if 6 <= len(entry) <= 200 and entry not in bullets:
                bullets.append(entry)
        elif line.strip().startswith(("pip ", "npm ", "yarn ", "pnpm ", "cargo ", "go ", "git clone", "docker ")):
            code_hints.append(line.strip()[:160])

    for fence in _FENCE_RE.finditer(cleaned):
        body = fence.group(2).strip()
        if body and len(body) < 400:
            first = body.split("\n")[0].strip()
            if first and first not in code_hints and any(
                token in first for token in ("pip", "npm", "python", "import", "from", "docker", "git", "./")
            ):
                code_hints.append(first[:160])

    digest.headings = headings[:MAX_HEADINGS]
    digest.bullets = bullets[:MAX_BULLETS]
    digest.code_hints = code_hints[:8]

    # Anything left on the cutting-room floor counts as truncation. Getting this wrong
    # is how a model ends up presenting 14% of a document as the whole thing while
    # believing it saw everything (a real bug caught by the test below).
    if (
        len(headings) > MAX_HEADINGS
        or len(bullets) > MAX_BULLETS
        or len(code_hints) > 8
    ):
        digest.truncated = True

    # Section bodies: take the most information-dense ones first, bounded each.
    sections: list[tuple[str, str]] = []
    current_title = ""
    current: list[str] = []
    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            if current_title and current:
                body = "\n".join(current).strip()
                if body:
                    sections.append((current_title, body[:MAX_SECTION_CHARS]))
            current_title = match.group(2).strip()[:80]
            current = []
        else:
            current.append(line)
    if current_title and current:
        body = "\n".join(current).strip()
        if body:
            sections.append((current_title, body[:MAX_SECTION_CHARS]))

    # Prefer sections with substance and skip pure link lists.
    def weight(entry: tuple[str, str]) -> float:
        title, body = entry
        link_ratio = body.count("http") / max(len(body), 1)
        return len(body) * (0.2 if link_ratio > 0.02 else 1.0)

    sections.sort(key=weight, reverse=True)
    if len(sections) > 8 or any(len(body) >= MAX_SECTION_CHARS for _title, body in sections):
        # Sections were dropped, or a section was cut mid-way.
        digest.truncated = True
    digest.sections = sections[:8]

    # Enforce the overall ceiling by dropping the least useful sections.
    while digest.sections and len(digest.render()) > MAX_DIGEST_CHARS:
        digest.sections.pop()
        digest.truncated = True
    if len(digest.render()) > MAX_DIGEST_CHARS:
        # Structure alone exceeds the cap: trim the bullet list, then the headings.
        digest.bullets = digest.bullets[:8]
        digest.headings = digest.headings[:16]
        digest.truncated = True

    digest.sent_chars = len(digest.render())
    # Safety net: whatever the paths above, if most of the document was not sent, the
    # model must be told. Silent reduction is the failure this guards against.
    if not digest.truncated and digest.total_chars and digest.sent_chars < digest.total_chars * 0.9:
        digest.truncated = True
    if digest.truncated and not any("节选" in note for note in digest.notes):
        digest.notes.append("README 过长，只送了结构化节选")
    return digest


def readme_fingerprint(text: str) -> str:
    """Content hash, so the same README is recognisable across runs."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


PROMO_SYSTEM_PROMPT = """你是帮开发者写推广文案的社媒编辑，服务对象是**大学生、学生和编程学习者**。
你只输出严格合法的 JSON：不要 Markdown 代码块、不要解释、不要多余标点。

你的职业底线：
- **只写 README 里真实存在的东西。** 没有写的功能、数据、性能、用户量、star 数一律不许出现。
- 不编造个人经历。不要写"我踩了三个月的坑""被同事安利"这类虚构故事。
- 不夸大。README 说"实验性"就不要写成"生产可用"。
- 读不懂或没看到的部分，写进 unknowns，而不是猜一个答案。"""


def build_promo_prompt(
    digest: ReadmeDigest, *, extra_note: str = "", style_block: str = ""
) -> str:
    """The user message for one promotion generation."""
    return f"""下面是一个开源项目的 README 结构化节选，请把它写成推广帖子。

{digest.render()}

{style_block}

{('补充说明：' + extra_note) if extra_note else ''}

请只输出下面这个 JSON（不要任何其他文字）：
{{
  "project_name": "项目名",
  "one_liner": "一句话说清它是干什么的、给谁用（不超过40字）",
  "cover_text": "小红书封面大字（不超过14字，要让人想点开）",
  "versions": {{
    "xiaohongshu": {{
      "title": "标题（不超过20字，带钩子：痛点/数字/反差，但数字必须来自 README）",
      "content": "正文（250-450字，短句分段，可用适量 emoji；先说解决什么问题，再说怎么上手）",
      "ending": "结尾互动引导（一句提问或邀请）",
      "hashtags": ["#编程#", "#开源项目#", "#大学生#", "#程序员#", "#AI#", "#代码#"]
    }},
    "weibo": {{
      "opening": "开头一句话（要能单独成立）",
      "content": "正文（100-200字，直接、信息密度高）",
      "hashtags": ["#开源#", "#程序员#"]
    }},
    "douyin": {{
      "hook": "前3秒钩子口播",
      "script": "30秒口播脚本（150-250字，口语，面向学生）",
      "scenes": ["画面建议1", "画面建议2", "画面建议3"],
      "cues": "字幕要点",
      "cta": "结尾行动号召"
    }}
  }},
  "source_points": ["这条文案依据的 README 要点，逐条列出"],
  "unknowns": ["README 里没写、你无法确定的信息"],
  "image_ideas": ["建议截什么图做配图，3-5 条（说明截哪个界面/文件）"]
}}

硬性规则：
1. **不许编造**：任何数字（star、下载量、性能、用户数）、任何"我用了多久"的经历，README 没写就不许写。
2. **面向学生**：读者是大学生和编程初学者。少用黑话，第一次出现的术语要给一句人话解释。
3. **说清三件事**：它解决什么问题 / 谁适合用 / 怎么开始用（给出 README 里的真实安装或启动命令）。
4. **三个平台不同**：小红书口语化分段、微博短句、抖音是口播脚本。不要把同一段复制三次。
5. **不要罗列技术栈充数**：技术栈最多提一句，重点在"能帮我做什么"。
6. 如果节选里没有安装/使用方式，就在 unknowns 里说明，**不要自己编命令**。"""


__all__ = [
    "MAX_DIGEST_CHARS",
    "PROMO_SYSTEM_PROMPT",
    "PromoResult",
    "PromoVersions",
    "ReadmeDigest",
    "build_promo_prompt",
    "digest_readme",
    "readme_fingerprint",
]

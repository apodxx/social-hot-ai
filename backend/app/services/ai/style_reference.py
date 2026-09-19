"""Use a Xiaohongshu note as a **style reference** for promotion copy (Phase 10).

The distinction this module is built around: the project README supplies the *substance*,
a note supplies the *voice*. A student-facing account wants to sound like the posts its
audience already reads, and the honest way to do that is to learn the shape of a good
note — how long the title is, how the body is broken up, how emoji and hashtags are used
— without reproducing a single sentence of someone else's post.

Two steps, deliberately separated because their costs differ:

1. :func:`resolve_note_link` — **free**. A share link redirects to a URL that carries the
   note id and the ``xsec_token`` the detail endpoint requires. Verified against a real
   link: ``https://xhslink.cn/o/…`` → login page whose ``redirectPath`` contains
   ``/discovery/item/<id>`` and a double-encoded ``xsec_token``. No API call is needed to
   learn those, so a bad link costs nothing.
2. :func:`fetch_note` — **one billed TikHub call**, opening the note itself.

The reference is passed to the model with explicit rules: imitate the *form*, never the
content. Copying a stranger's post into a project promotion would be both plagiarism and
off-topic, and the model is told exactly that.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

from app.services.tikhub.base import as_int, as_text, dig
from app.services.tikhub.client import TikHubClient, TikHubError
from app.services.tikhub.details import XIAOHONGSHU_NOTE_DETAIL, XHS_ITEMS_PATH

logger = logging.getLogger(__name__)

#: Note ids are 24 hex characters.
_NOTE_ID_RE = re.compile(r"(?:explore|discovery/item|item)/([0-9a-fA-F]{24})")
_TOKEN_RE = re.compile(r"xsec_token=([A-Za-z0-9_\-=+%/]+)")
#: How many times to decode. The token arrives double-encoded inside ``redirectPath``.
_DECODE_ROUNDS = 3

SHARE_HOSTS = ("xhslink.cn", "xhslink.com", "xiaohongshu.com", "www.xiaohongshu.com")


@dataclass
class NoteLink:
    """What a share link turned out to contain."""

    note_id: str = ""
    xsec_token: str = ""
    final_url: str = ""
    resolved: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "has_token": bool(self.xsec_token),
            "final_url": self.final_url[:300],
            "resolved": self.resolved,
            "error": self.error,
        }


@dataclass
class StyleSample:
    """The shape of a reference note, without its claims."""

    note_id: str = ""
    title: str = ""
    body: str = ""
    hashtags: list[str] = field(default_factory=list)
    author: str = ""
    note_type: str = ""
    likes: int | None = None
    image_count: int = 0
    url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "title": self.title,
            "title_length": len(self.title),
            "body_length": len(self.body),
            "paragraphs": len([p for p in self.body.split("\n") if p.strip()]),
            "hashtags": self.hashtags,
            "author": self.author,
            "note_type": self.note_type,
            "likes": self.likes,
            "image_count": self.image_count,
            "url": self.url,
        }


def parse_note_target(*texts: str) -> tuple[str, str]:
    """Pull ``(note_id, xsec_token)`` out of any of ``texts``. Pure, no I/O.

    Separated from :func:`resolve_note_link` so the parsing is testable without a
    network call — a test that fetched ``example.com`` made the suite depend on the
    internet, which this project explicitly promises it does not.
    """
    note_id = ""
    token = ""
    expanded: list[str] = []
    for text in texts:
        if not text:
            continue
        current = text
        for _ in range(_DECODE_ROUNDS):
            current = unquote(current)
            if current not in expanded:
                expanded.append(current)

    # Decoded forms are searched first: the raw URL still holds ``%3D`` where the token
    # ends in ``=``, and taking that form first is how the padding leaked into the value.
    for candidate in [*expanded, *texts]:
        if not candidate:
            continue
        if not note_id:
            match = _NOTE_ID_RE.search(candidate)
            if match:
                note_id = match.group(1)
        if not token:
            match = _TOKEN_RE.search(candidate)
            if match:
                # Unquote whatever was matched: the token may still carry escapes even
                # in a decoded candidate (nested encoding).
                token = unquote(match.group(1)).strip().rstrip("/")
        if note_id and token:
            break
    return note_id, token


def resolve_note_link(url: str, *, timeout: float = 20.0) -> NoteLink:
    """Turn a Xiaohongshu share link into a note id and token. **Free.**

    Follows the redirect chain and looks for the id/token in the final URL *and* inside
    an encoded ``redirectPath`` parameter, because the share link lands on a login page
    whose query string carries the real destination.

    A link that already carries both (a normal ``/explore/<id>?xsec_token=…`` URL) is
    answered without any network call at all.
    """
    link = NoteLink()
    target = (url or "").strip()
    if not target:
        link.error = "链接为空"
        return link
    if not target.startswith(("http://", "https://")):
        link.error = "链接必须以 http:// 或 https:// 开头"
        return link

    # Nothing to fetch when the URL is already complete.
    note_id, token = parse_note_target(target)
    if note_id and token:
        link.note_id, link.xsec_token = note_id, token
        link.final_url = target
        link.resolved = True
        logger.info("resolved xiaohongshu note %s directly from the link", note_id)
        return link

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            response = client.get(target)
            link.final_url = str(response.url)
    except httpx.HTTPError as exc:
        link.error = f"打不开这个链接：{type(exc).__name__}: {exc}"
        return link

    note_id, token = parse_note_target(target, link.final_url)
    link.note_id, link.xsec_token = note_id, token

    if not link.note_id:
        link.error = (
            "没能在链接里找到笔记 id。请确认这是小红书笔记的分享链接"
            f"（最终地址：{link.final_url[:160] or target}）"
        )
        return link
    if not link.xsec_token:
        # Without the token the detail endpoint refuses; better to say so than to spend a
        # call that cannot succeed.
        link.error = (
            f"找到了笔记 id（{link.note_id}）但链接里没有 xsec_token，"
            "无法读取笔记内容。请用小红书 App 的「复制链接」重新获取。"
        )
        return link

    link.resolved = True
    logger.info("resolved xiaohongshu note %s from a share link", link.note_id)
    return link


async def fetch_note(
    link: NoteLink, *, client: TikHubClient | None = None, settings: Any = None
) -> tuple[dict[str, Any] | None, str]:
    """Open the note. **One billed TikHub call.** Returns ``(payload, error)``."""
    if not link.resolved:
        return None, link.error or "链接未解析成功"
    owns = client is None
    if owns:
        from app.core.config import get_settings

        client = TikHubClient(settings or get_settings())
    assert client is not None
    try:
        payload = await client.get_json(
            XIAOHONGSHU_NOTE_DETAIL,
            {"note_id": link.note_id, "xsec_token": link.xsec_token},
        )
    except TikHubError as exc:
        return None, f"读取笔记失败：{exc}"
    finally:
        if owns:
            await client.aclose()
    return payload, ""


def style_sample_from_payload(payload: dict[str, Any], *, link: NoteLink | None = None) -> StyleSample:
    """Extract the reference note's shape.

    Deliberately keeps the title and body **separate** (unlike the detail stage, which
    joins them into source material to quote): for a style reference the structure is the
    point, and the model needs to see how long the title is against how the body runs.
    """
    sample = StyleSample(
        note_id=link.note_id if link else "",
        url=f"https://www.xiaohongshu.com/explore/{link.note_id}" if link else "",
    )
    items = dig(payload, XHS_ITEMS_PATH)
    if not isinstance(items, list) or not items:
        return sample
    card = dig(items[0], "note_card")
    if not isinstance(card, dict):
        return sample

    sample.title = as_text(card.get("title")) or ""
    sample.body = as_text(card.get("desc")) or ""
    sample.author = as_text(dig(card, "user.nickname")) or as_text(dig(card, "user.nick_name")) or ""
    sample.note_type = as_text(card.get("type")) or ""
    sample.likes = as_int(dig(card, "interact_info.liked_count"))
    sample.hashtags = [
        str(tag.get("name"))
        for tag in (card.get("tag_list") or [])
        if isinstance(tag, dict) and tag.get("name")
    ]
    sample.image_count = len(card.get("image_list") or [])
    return sample


def render_style_block(sample: StyleSample) -> str:
    """The reference as prompt text, with the no-copying rules attached."""
    if not (sample.title or sample.body):
        return ""
    paragraphs = len([line for line in sample.body.split("\n") if line.strip()])
    details = [
        f"参考笔记标题（{len(sample.title)} 字）：{sample.title}",
        f"参考笔记正文（{len(sample.body)} 字，{paragraphs} 段）：\n{sample.body[:1200]}",
    ]
    if sample.hashtags:
        details.append("它用的标签：" + "、".join(sample.hashtags[:10]))
    if sample.image_count:
        details.append(f"它配了 {sample.image_count} 张图")
    if sample.note_type:
        details.append(f"笔记类型：{sample.note_type}")
    return (
        "【风格参考笔记】下面是同领域一篇表现不错的小红书笔记，**只用来学它的形式**"
        "（标题长度、分段节奏、语气、emoji 与标签的用法）：\n"
        + "\n".join(details)
        + "\n\n风格规则（重要）：\n"
        "1. **绝对不要复制它的任何句子、说法或事实**——那是别人的内容，与本项目无关。\n"
        "2. 不要把它的作者经历、数据、产品名当成你的。\n"
        "3. 学的是**形式**：标题多长、正文分几段、每段多长、标签怎么放、语气如何。\n"
        "4. 如果它的风格与账号定位冲突（例如过度夸张），以账号定位为准。"
    )


async def build_style_reference(
    url: str, *, client: TikHubClient | None = None, settings: Any = None
) -> tuple[StyleSample | None, NoteLink, str]:
    """Resolve + fetch + extract in one call. Returns ``(sample, link, error)``."""
    link = resolve_note_link(url)
    if not link.resolved:
        return None, link, link.error
    payload, error = await fetch_note(link, client=client, settings=settings)
    if payload is None:
        return None, link, error
    sample = style_sample_from_payload(payload, link=link)
    if not (sample.title or sample.body):
        return None, link, "笔记内容为空（可能已被删除或需要登录）"
    return sample, link, ""


__all__ = [
    "NoteLink",
    "SHARE_HOSTS",
    "StyleSample",
    "build_style_reference",
    "fetch_note",
    "parse_note_target",
    "render_style_block",
    "resolve_note_link",
    "style_sample_from_payload",
]

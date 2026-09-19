"""Generating and storing promotion copy from a README (Phase 10).

The service layer behind ``POST /api/promo/readme``, the MCP tool and the CLI. It owns
the three steps that must happen in order:

1. **Get the text** — pasted, read from a local file, or fetched from a URL — with a
   size cap, because a 2 MB README is not a README.
2. **Digest it** to a bounded, structured view (see :mod:`app.services.ai.promo`).
3. **One DeepSeek call**, with the token usage recorded on the row.

Reading a local path is supported because that is how a developer actually has a README
checked out; the path is resolved and the size is checked before reading, and only
``.md``/``.txt``/``.markdown`` are accepted so this cannot be pointed at an arbitrary
file to exfiltrate it into a prompt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.models.readme_promo import ReadmePromoRecord
from app.services.ai.deepseek import DeepSeekClient, DeepSeekError, DeepSeekJSONError, extract_json
from app.services.ai.promo import (
    PROMO_SYSTEM_PROMPT,
    PromoResult,
    ReadmeDigest,
    build_promo_prompt,
    digest_readme,
    readme_fingerprint,
)

logger = logging.getLogger(__name__)

#: Largest README accepted, before digesting. Anything bigger is a mistake or a dump.
MAX_README_BYTES = 2 * 1024 * 1024
#: Extensions a local path may have. Reading arbitrary files into a prompt is a way to
#: leak a private key, so this is an allow-list.
ALLOWED_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
#: Extension-less documents that are genuinely READMEs. An empty suffix must **not** be
#: in the allow-list above: ``Path("id_rsa").suffix`` and ``Path(".env").suffix`` are
#: both ``""``, so ``""`` in that tuple (the first version) accepted every secret file
#: on the machine. Matching the stem by name keeps ``README`` working without that hole.
ALLOWED_STEMS = ("readme", "readme_en", "readme_zh", "readme_cn", "说明", "readme-dev")

#: A digest smaller than this carries no usable structure; sending it would be paying
#: for the model to say "I cannot tell".
MIN_USEFUL_DIGEST_CHARS = 200

#: Markers that mean "this is a web page, not a document".
_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body", "<div", "<script")
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)

#: Sentence-ending punctuation, used to tell prose from code.
_SENTENCE_RE = re.compile(r"[。！？；!?]|\.\s|\n")


def looks_like_html(content: str, content_type: str = "") -> bool:
    """True when the payload is a web page rather than README text.

    This guard exists because of a real incident: a 小红书 share link
    (``https://xhslink.cn/o/...``) was pasted into the URL field. It redirects to a
    **login page**, and the loader happily digested 35,865 characters of HTML/JS down to
    417 characters of noise and then paid for a generation that could only answer
    "the README does not say". The model behaved correctly; the input should never have
    been sent.
    """
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared in {"text/html", "application/xhtml+xml"}:
        return True
    head = (content or "")[:2000].lstrip().lower()
    return any(marker in head for marker in _HTML_MARKERS)


def html_title(content: str) -> str:
    """The page's ``<title>``, so the error can say what was actually fetched."""
    match = _HTML_TITLE_RE.search((content or "")[:8000])
    return match.group(1).strip()[:120] if match else ""


@dataclass
class PromoRunResult:
    """What one generation did."""

    ok: bool = False
    record_id: int | None = None
    project_name: str = ""
    digest: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    estimated_cny: float = 0.0
    model: str = ""
    attempts: int = 0
    error: str = ""
    #: Billed provider calls this run made (1 for a style reference, 0 otherwise).
    billed_calls: int = 0
    #: The reference note's shape, when one was used.
    style_reference: dict[str, Any] = field(default_factory=dict)
    #: How the link resolved, so a bad link is diagnosable.
    style_link: dict[str, Any] = field(default_factory=dict)
    #: HTTP status the API should answer with. Carried on the result so the route does
    #: not have to guess from the error text: a bad input is 422, a provider problem is
    #: 502, and matching on message substrings got that wrong (a contentless digest was
    #: reported as a provider failure).
    status: int = 422

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "record_id": self.record_id,
            "project_name": self.project_name,
            "digest": self.digest,
            "result": self.result,
            "tokens": self.tokens,
            "estimated_cny": round(self.estimated_cny, 4),
            "model": self.model,
            "attempts": self.attempts,
            "error": self.error,
            "billed_calls": self.billed_calls,
            "style_reference": self.style_reference,
            "style_link": self.style_link,
        }


def _prose_like(text: str) -> bool:
    """True when a block reads as an introduction rather than as code or a data dump.

    The "first paragraph" of *any* text is non-empty, so its mere presence proves
    nothing: a blob of ``var a=1;`` has a 360-character first paragraph. Prose has
    sentence punctuation; minified code and logs mostly do not. This is a heuristic and
    it is deliberately generous — it only has to separate "an introduction exists" from
    "there is no introduction at all".
    """
    stripped = (text or "").strip()
    if len(stripped) < 120:
        return False
    return len(_SENTENCE_RE.findall(stripped)) >= 1


def _has_usable_structure(digest: ReadmeDigest) -> bool:
    """Whether there is anything worth paying a model to write about."""
    return bool(
        digest.headings
        or digest.bullets
        or digest.code_hints
        or digest.sections
        or _prose_like(digest.pitch)
    )


def load_readme(
    *,
    text: str | None = None,
    path: str | None = None,
    url: str | None = None,
    timeout: float = 20.0,
) -> tuple[str, str, str]:
    """Return ``(content, source_kind, source_name)`` from exactly one source."""
    supplied = [name for name, value in (("text", text), ("path", path), ("url", url)) if value]
    if len(supplied) != 1:
        raise ValueError("provide exactly one of: text, path, url")

    if text:
        if len(text.encode("utf-8")) > MAX_README_BYTES:
            raise ValueError(f"README exceeds {MAX_README_BYTES} bytes")
        if looks_like_html(text):
            raise ValueError(
                "粘贴的内容看起来是网页 HTML 而不是 README"
                + (f"（页面标题：{html_title(text)}）" if html_title(text) else "")
                + "。请粘贴 README 的文本内容。"
            )
        return text, "text", "pasted text"

    if path:
        resolved = Path(path).expanduser()
        if not resolved.is_file():
            raise ValueError(f"not a file: {resolved}")
        suffix = resolved.suffix.lower()
        if suffix not in ALLOWED_SUFFIXES and resolved.stem.lower() not in ALLOWED_STEMS:
            raise ValueError(
                f"refusing to read {resolved.name!r}: expected one of {ALLOWED_SUFFIXES} "
                f"or a file named like {ALLOWED_STEMS[0]}"
            )
        if resolved.stat().st_size > MAX_README_BYTES:
            raise ValueError(f"README exceeds {MAX_README_BYTES} bytes")
        return resolved.read_text(encoding="utf-8", errors="replace"), "path", str(resolved)

    assert url is not None  # narrowed by the check above
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    # trust_env=False for the same reason the rest of the project uses it: the machine's
    # registry proxy turns local calls into 502s, and provider fetches go direct.
    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        response = client.get(url)
        response.raise_for_status()
        content = response.text
        final_url = str(response.url)
        content_type = response.headers.get("content-type", "")
    if len(content.encode("utf-8")) > MAX_README_BYTES:
        raise ValueError(f"README exceeds {MAX_README_BYTES} bytes")

    if looks_like_html(content, content_type):
        # Refused *before* the paid call. The old behaviour digested the page and then
        # paid the model to report that a login screen says nothing about the project.
        details = [f"Content-Type: {content_type or 'unknown'}"]
        if final_url != url:
            details.append(f"重定向到: {final_url[:180]}")
        title = html_title(content)
        if title:
            details.append(f"页面标题: {title}")
        raise ValueError(
            "这个网址返回的是**网页**而不是 README（" + "；".join(details) + "）。"
            "常见原因：分享短链或需要登录的页面（例如小红书/知乎链接）、或是仓库主页而"
            "非文件原始地址。请改用文件的 raw 链接（如 raw.githubusercontent.com），"
            "或直接把 README 内容粘贴进来。"
        )

    return content, "url", url


async def generate_promo(
    *,
    text: str | None = None,
    path: str | None = None,
    url: str | None = None,
    project_name: str = "",
    extra_note: str = "",
    settings: Settings | None = None,
    client: DeepSeekClient | None = None,
    store: bool = True,
    style_url: str | None = None,
) -> PromoRunResult:
    """Digest a README and generate the three promotion versions.

    **Spends DeepSeek tokens** — one call, bounded by the digest cap. A style reference
    additionally costs **one billed TikHub call**, and it is resolved and fetched *before*
    the model call: if the link cannot be used, the request fails without paying for a
    generation that would not have the style the caller asked for.
    """
    resolved = settings or get_settings()
    run = PromoRunResult()

    try:
        content, source_kind, source_name = load_readme(text=text, path=path, url=url)
    except (ValueError, OSError, httpx.HTTPError) as exc:
        run.error = f"could not read the README: {exc}"
        return run

    # --- style reference first: cheapest thing to fail on --------------------------
    style_block = ""
    if style_url:
        from app.services.ai.style_reference import build_style_reference, render_style_block

        sample, link, style_error = await build_style_reference(style_url, settings=resolved)
        run.style_link = link.as_dict()
        if sample is None:
            run.error = (
                f"风格参考笔记不可用：{style_error}。**没有调用模型，也没有产生费用。**"
            )
            run.status = 422
            return run
        run.style_reference = sample.as_dict()
        style_block = render_style_block(sample)
        run.billed_calls += 1
        logger.info(
            "promo: xiaohongshu note %s as a style reference (%d-char title, %d-char body)",
            sample.note_id,
            len(sample.title),
            len(sample.body),
        )

    digest: ReadmeDigest = digest_readme(content, project_name=project_name)
    run.digest = digest.as_dict()
    run.project_name = digest.project_name
    if not content.strip():
        run.error = "the README is empty"
        return run

    # Second gate, and the more general one: whatever the source, if the digest found no
    # usable structure there is nothing to promote and the call would be pure waste.
    if not _has_usable_structure(digest) or digest.sent_chars < MIN_USEFUL_DIGEST_CHARS:
        run.error = (
            "这份内容里没有可识别的 README 结构（标题/简介/功能条目/安装步骤都没有找到），"
            f"只提取到 {digest.sent_chars} 字符。**没有调用模型，也没有产生费用。**"
            "请确认输入的是项目 README 而不是网页、登录页或代码片段。"
        )
        logger.warning("promo: refusing a contentless digest (%d chars)", digest.sent_chars)
        return run

    owns_client = client is None
    active = client or DeepSeekClient(resolved)
    run.model = active.model
    try:
        payload, completion = await active.complete_json(
            system=PROMO_SYSTEM_PROMPT,
            user=build_promo_prompt(digest, extra_note=extra_note, style_block=style_block),
            temperature=0.6,
            max_tokens=resolved.rewrite_max_tokens,
        )
        run.attempts = 1
        run.tokens = completion.usage.as_dict()
        run.estimated_cny = completion.usage.estimated_cny()
        if completion.finish_reason == "length":
            run.error = (
                f"output was truncated at REWRITE_MAX_TOKENS={resolved.rewrite_max_tokens}; "
                "raise the limit rather than retrying with the same ceiling"
            )
            run.status = 502
            return run
        parsed = PromoResult.model_validate(payload)
    except DeepSeekJSONError as exc:
        run.error = f"unparseable response: {exc}"
        run.status = 502
        return run
    except DeepSeekError as exc:
        run.error = f"provider error: {exc}"
        run.status = 502
        return run
    except Exception as exc:  # noqa: BLE001 - validation problems are reported, not raised
        run.error = f"{type(exc).__name__}: {exc}"
        run.status = 502
        return run
    finally:
        if owns_client:
            await active.aclose()

    run.ok = True
    run.result = parsed.model_dump(mode="json")
    run.project_name = parsed.project_name or digest.project_name

    if store:
        try:
            run.record_id = await _store(
                resolved,
                parsed=parsed,
                digest=digest,
                content=content,
                source_kind=source_kind,
                source_name=source_name,
                payload=payload,
                model=active.model,
                tokens=run.tokens,
                style=run.style_reference,
            )
        except Exception as exc:  # noqa: BLE001 - a storage problem must not lose the copy
            logger.error("promo: generated but could not store: %s", exc)
            run.error = f"generated, but storing failed: {exc}"

    logger.info("promo generated for %r: %s", run.project_name, run.tokens)
    return run


async def _store(
    settings: Settings,
    *,
    parsed: PromoResult,
    digest: ReadmeDigest,
    content: str,
    source_kind: str,
    source_name: str,
    payload: dict[str, Any],
    model: str,
    tokens: dict[str, int],
    style: dict[str, Any] | None = None,
) -> int:
    """Persist one promotion set. Returns its id."""
    async with session_scope(settings) as session:
        record = ReadmePromoRecord(
            source_name=source_name[:255],
            source_kind=source_kind,
            readme_sha256=readme_fingerprint(content),
            readme_chars=len(content),
            sent_chars=digest.sent_chars,
            project_name=(parsed.project_name or digest.project_name)[:500],
            one_liner=parsed.one_liner,
            cover_text=parsed.cover_text,
            versions=parsed.versions.model_dump(mode="json"),
            source_points=list(parsed.source_points),
            unknowns=list(parsed.unknowns),
            image_ideas=list(parsed.image_ideas),
            digest=digest.as_dict(),
            style_reference=style or {},
            model=model,
            prompt_tokens=int(tokens.get("prompt_tokens", 0)),
            completion_tokens=int(tokens.get("completion_tokens", 0)),
            attempts=1,
            raw_response=payload,
        )
        session.add(record)
        await session.flush()
        return record.id


async def list_promos(
    settings: Settings | None = None, *, limit: int = 50, offset: int = 0
) -> tuple[list[ReadmePromoRecord], int]:
    """Stored promotions, newest first."""
    resolved = settings or get_settings()
    from sqlalchemy import func

    async with session_scope(resolved) as session:
        total = (
            await session.execute(select(func.count(ReadmePromoRecord.id)))
        ).scalar_one()
        rows = (
            await session.execute(
                select(ReadmePromoRecord)
                .order_by(ReadmePromoRecord.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    return list(rows), int(total)


async def get_promo(settings: Settings | None, promo_id: int) -> ReadmePromoRecord | None:
    resolved = settings or get_settings()
    async with session_scope(resolved) as session:
        return (
            await session.execute(
                select(ReadmePromoRecord).where(ReadmePromoRecord.id == promo_id)
            )
        ).scalars().first()


def promo_to_dict(record: ReadmePromoRecord) -> dict[str, Any]:
    """Serialise a stored promotion for the API and the MCP tool."""
    return {
        "id": record.id,
        "project_name": record.project_name,
        "one_liner": record.one_liner,
        "cover_text": record.cover_text,
        "versions": record.versions or {},
        "source_points": record.source_points or [],
        "unknowns": record.unknowns or [],
        "image_ideas": record.image_ideas or [],
        "source_name": record.source_name,
        "source_kind": record.source_kind,
        # The style reference the copy learned from, so a reviewer can check it was not
        # copied (the shape is recorded, not the other author's claims).
        "style_reference": record.style_reference or {},
        "readme_chars": record.readme_chars,
        "sent_chars": record.sent_chars,
        "truncated": record.readme_chars > record.sent_chars,
        "model": record.model,
        "tokens": {
            "prompt": record.prompt_tokens,
            "completion": record.completion_tokens,
        },
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


__all__ = [
    "ALLOWED_STEMS",
    "ALLOWED_SUFFIXES",
    "MAX_README_BYTES",
    "MIN_USEFUL_DIGEST_CHARS",
    "PromoRunResult",
    "generate_promo",
    "get_promo",
    "html_title",
    "list_promos",
    "load_readme",
    "looks_like_html",
    "promo_to_dict",
]

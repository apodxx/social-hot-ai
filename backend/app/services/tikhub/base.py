"""Shared adapter plumbing: the base class and the tolerant extractors.

TikHub's OpenAPI document describes every endpoint's *request* but publishes no
response schema or example — every 200 is a generic ``ResponseModel``. The
platform payload shapes therefore cannot be read off the docs, and this phase
refuses to invent them. Instead adapters extract through candidate key paths
and keep the original item in ``raw_data``; once a real response has been
captured into ``tests/fixtures/raw/``, the candidate lists are narrowed to the
confirmed paths.

Nothing here fabricates a value: a field the provider does not supply stays
``None``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Iterable, Sequence

from app.models.hot_content import HotContent, Platform
from app.services.tikhub.client import TikHubClient

logger = logging.getLogger(__name__)

#: Chinese/ASCII magnitude suffixes used by engagement counters ("1.2万", "3亿").
_MAGNITUDES: tuple[tuple[str, float], ...] = (
    ("亿", 1e8),
    ("万", 1e4),
    ("w", 1e4),
    ("W", 1e4),
    ("k", 1e3),
    ("K", 1e3),
)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def dig(obj: Any, path: str) -> Any:
    """Read a dotted path, supporting list indices: ``dig(d, "data.0.items")``."""
    current = obj
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def first_of(obj: Any, paths: Iterable[str]) -> Any:
    """First non-empty value among ``paths``."""
    for path in paths:
        value = dig(obj, path)
        if value not in (None, "", [], {}):
            return value
    return None


def as_text(value: Any) -> str | None:
    """Coerce to a trimmed string, or ``None`` when there is nothing usable."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def as_int(value: Any) -> int | None:
    """Parse a counter that may arrive as ``int``, ``"1,234"`` or ``"1.2万"``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "").replace("+", "")
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    number = float(match.group())
    for suffix, factor in _MAGNITUDES:
        if suffix in text:
            return int(number * factor)
    return int(number)


def as_datetime(value: Any) -> datetime | None:
    """Parse a timestamp that may be unix seconds, unix milliseconds, ISO, or text."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        if seconds <= 0:
            return None
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return as_datetime(int(text))
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # Platform timestamps are China-local; Phase 1 keeps them timezone-aware.
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed


def iter_dict_lists(node: Any, path: str = "") -> Iterable[tuple[str, list[Any]]]:
    """Yield every ``(path, list)`` of dicts found anywhere in ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            yield from iter_dict_lists(value, child)
    elif isinstance(node, list):
        if node and all(isinstance(item, dict) for item in node):
            yield path, node
        for index, value in enumerate(node):
            yield from iter_dict_lists(value, f"{path}.{index}")


def find_item_list(payload: dict[str, Any], *, path_hint: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Locate the longest list of dicts in ``payload`` — the hot items.

    Heuristic by necessity (no response schema is published). ``path_hint``
    lets an adapter prefer a known location once the real shape is confirmed.
    """
    for hint in path_hint:
        candidate = dig(payload, hint)
        if isinstance(candidate, list) and candidate and all(isinstance(i, dict) for i in candidate):
            logger.debug("item list found at hinted path %s (%d items)", hint, len(candidate))
            return candidate
    best_path, best_items = "", []
    for path, items in iter_dict_lists(payload):
        if len(items) > len(best_items):
            best_path, best_items = path, items
    if best_items:
        logger.debug("item list picked heuristically at %s (%d items)", best_path, len(best_items))
    return best_items


def looks_latin1_mangled(text: str | None) -> bool:
    """True when ``text`` looks like UTF-8 bytes that were decoded as Latin-1.

    A real hazard, not a hypothetical one: PowerShell's ``Invoke-WebRequest``
    decodes a body whose ``Content-Type`` carries no charset as ISO-8859-1, so
    capturing a JSON response that way turns every Chinese title into runs of
    Latin-1 accented characters. Provider text for these platforms is Chinese,
    so "high Latin-1 characters present, no CJK at all" means the decoding was
    wrong. (This is why captures must go through httpx — see
    ``scripts/discover_raw.py``.)
    """
    if not text:
        return False
    suspicious = sum(1 for character in text if 0xC0 <= ord(character) <= 0xFF)
    cjk = sum(1 for character in text if 0x4E00 <= ord(character) <= 0x9FFF)
    return suspicious > 0 and cjk == 0


def demangle_latin1(text: str) -> str | None:
    """Recover text that was UTF-8 decoded as Latin-1.

    Latin-1 maps all 256 byte values, so this re-encoding is lossless whenever
    the original damage was exactly that. ``None`` means the text is not
    recoverable this way.
    """
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None


class BasePlatformAdapter(ABC):
    """Shared plumbing for every adapter: helpers, and the client binding.

    Deliberately does **not** declare ``fetch_hot``. Search adapters share all of this
    plumbing but have nothing to do with hot-list retrieval, and forcing them to
    implement an unrelated method (or making them un-instantiable) would be a lie about
    what they are. The hot-list contract lives in :class:`HotListAdapter`.
    """

    platform: ClassVar[Platform]

    def __init__(self, client: TikHubClient) -> None:
        self.client = client

    # ------------------------------------------------------------------ helpers
    def _build(
        self,
        item: dict[str, Any],
        *,
        rank: int | None,
        content_id: str,
        title: str,
        **fields: Any,
    ) -> HotContent:
        """Assemble one item, deriving the stable id and keeping ``raw_data``.

        ``description`` is **not** backfilled from the title: a hot-search board
        carries no body, and duplicating the title would make "no body" and
        "body equals title" indistinguishable downstream.
        """
        payload = {
            "title": title,
            "description": fields.pop("description", None) or "",
            "rank": rank,
            "raw_data": item,
            **fields,
        }
        return HotContent(
            id=HotContent.make_id(self.platform, content_id),
            platform=self.platform,
            platform_content_id=content_id,
            **payload,
        )

    def _content_id(self, item: dict[str, Any], candidate_paths: Sequence[str], *, fallback: str) -> str:
        """Provider item id, falling back to a stable synthetic id.

        The fallback is a SHA-256 of the title: Python's ``hash()`` is salted
        per process, so using it would give the same item a different identity
        on every run and silently break deduplication.
        """
        value = first_of(item, candidate_paths)
        text = as_text(value)
        if text:
            return text
        digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:12]
        return f"synth-{digest}"


class HotListAdapter(BasePlatformAdapter):
    """An adapter that can read its platform's current hot list."""

    @abstractmethod
    async def fetch_hot(self, limit: int) -> list[HotContent]:
        """Return up to ``limit`` hot items for this platform."""

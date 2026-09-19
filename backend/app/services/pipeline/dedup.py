"""Deduplication layers 1-3: exact platform id, URL, and title similarity.

Layer 4 (cross-platform aggregation into ``TopicGroup``) lives in
:mod:`app.services.pipeline.topic_grouping`.

**Scoping is the subtle part, and getting it wrong loses data.** The spec asks
for two different things:

* layers 1-3 — "this is *the same item*", so collapse it;
* layer 4 — "these are *different items about the same topic*", typically on
  different platforms, so keep every one of them and group them.

Therefore layer 3 (title similarity) is scoped **per platform**. The same title
on Weibo and on Douyin is two different content items covering one topic — which
is exactly what layer 4 exists to aggregate — so collapsing them here would
throw away a platform's data point and leave nothing to aggregate. Layer 1 is
per-platform by construction, and layer 2 (URL) stays cross-platform because a
URL identifies one page regardless of which platform's endpoint returned it.

The other design choices:

* **Normalisation is pure and total.** ``normalize_title`` never raises and never
  returns a non-string, so it is safe on any provider payload — including the
  emoji- and full-width-heavy titles these platforms actually return.
* **Layer 3 is blocked by character bigrams.** Comparing every pair is
  quadratic; an inverted bigram index restricts comparison to titles sharing at
  least one two-character sequence, which for Chinese titles is cheap and
  effective. Survivors are scored with :func:`difflib.SequenceMatcher`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.models.hot_content import HotContent

logger = logging.getLogger(__name__)

#: Shortest normalized title worth a similarity comparison at all. Three, not
#: four: real Chinese hot words are often three characters ("九一八"), and with a
#: floor of four they would never match their own longer variants.
MIN_TITLE_LENGTH = 3

#: Longest normalized title kept (the DB column is String(512)).
MAX_TITLE_LENGTH = 512

_HASHTAG_RE = re.compile(r"#([^#]{1,40})#")
_KEEP_RE = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+", re.UNICODE)


class DedupLayer(str, Enum):
    """Which rule matched."""

    PLATFORM_CONTENT_ID = "platform_content_id"
    URL = "url"
    TITLE_SIMILARITY = "title_similarity"


@dataclass
class DuplicateMatch:
    """Why an incoming item was judged a duplicate."""

    layer: DedupLayer
    existing: HotContent
    key: str
    similarity: float | None = None


@dataclass
class DedupReport:
    """Outcome of one deduplication pass."""

    kept: list[HotContent] = field(default_factory=list)
    removed: list[tuple[HotContent, DuplicateMatch]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, int]:
        """``{"input": n, "kept": n, "removed_by_platform_content_id": n, ...}``."""
        return dict(self.counts)


def normalize_title(title: str | None) -> str:
    """Fold a title into its comparison key.

    NFKC (full-width to half-width, compatibility forms), case folding, hashtag
    markers unwrapped, then every non-word/non-CJK character dropped, so
    ``"#某某事件#"`` and ``"某某事件！！"`` compare equal.
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", str(title))
    text = _HASHTAG_RE.sub(r"\1", text)
    text = text.casefold()
    text = _KEEP_RE.sub("", text)
    return text[:MAX_TITLE_LENGTH]


#: Query parameters that describe a *share context* rather than the resource.
#: Dropping the whole query is wrong and was a real bug: Weibo's hot-search link
#: is ``/weibo?q=%23话题%23&Refer=top``, so a blanket strip canonicalised all 52
#: items to one URL and layer 2 then deleted 50 of them.
TRACKING_PARAMS = frozenset(
    {
        "refer",
        "refer_flag",
        "refer_wap",
        "band_rank",
        "sudaref",
        "share_token",
        "share_from",
        "spm",
        "scene",
        "is_all",
        "t",
        "from",
    }
)


def _is_tracking_param(key: str) -> bool:
    """True for a share/tracking parameter (including any ``utm_*``)."""
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith("utm_")


def normalize_url(url: str | None) -> str | None:
    """Fold a URL into its comparison key (scheme, host, path, resource query).

    Tracking parameters are dropped and the remaining ones are sorted, so two
    shares of one page compare equal; parameters that identify the resource —
    Weibo's ``q=``, for instance — are kept, so two different topics do not
    collapse into one. A missing scheme becomes ``https`` so the http/https
    variants of a page compare equal, and anything whose host is not a plausible
    hostname is rejected with ``None`` rather than stored as a key.
    """
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    try:
        parts = urlsplit(text if "//" in text else f"//{text}")
    except ValueError:
        return None
    netloc = parts.netloc
    if not netloc or any(character.isspace() for character in netloc):
        return None
    host = netloc.rsplit("@", 1)[-1].split(":")[0]
    if "." not in host and host != "localhost":
        return None
    path = parts.path.rstrip("/") or "/"
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if not _is_tracking_param(key)
    ]
    query = urlencode(sorted(kept))
    scheme = (parts.scheme or "https").lower()
    return urlunsplit((scheme, netloc.lower(), path, query, ""))[:1024]


def bigrams(text: str) -> set[str]:
    """Character bigrams of ``text`` (the whole string when too short)."""
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def title_similarity(left: str, right: str) -> float:
    """Similarity of two *normalized* titles in ``[0, 1]``.

    Containment counts as a match: "某某事件" inside "某某事件最新进展" is the same
    topic plus a qualifier, and a raw ratio would score it below threshold.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= MIN_TITLE_LENGTH and shorter in longer:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


@dataclass
class _TitleIndex:
    """Normalized titles of one platform, with a bigram blocking index."""

    entries: list[tuple[str, HotContent]] = field(default_factory=list)
    by_bigram: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))

    def add(self, normalized: str, item: HotContent) -> None:
        index = len(self.entries)
        self.entries.append((normalized, item))
        for gram in bigrams(normalized):
            self.by_bigram[gram].add(index)

    def best_match(self, normalized: str, threshold: float) -> tuple[float, HotContent, str] | None:
        candidates: set[int] = set()
        for gram in bigrams(normalized):
            candidates |= self.by_bigram.get(gram, set())
        best: tuple[float, HotContent, str] | None = None
        for candidate in candidates:
            other_key, other_item = self.entries[candidate]
            ratio = title_similarity(normalized, other_key)
            if ratio >= threshold and (best is None or ratio > best[0]):
                best = (ratio, other_item, other_key)
        return best


class DedupIndex:
    """In-memory index answering layers 1-3 for a set of known items."""

    def __init__(self, threshold: float = 0.82) -> None:
        self.threshold = threshold
        self._by_platform_id: dict[tuple[str, str], HotContent] = {}
        self._by_url: dict[str, HotContent] = {}
        self._titles: dict[str, _TitleIndex] = defaultdict(_TitleIndex)

    def __len__(self) -> int:
        return sum(len(title_index.entries) for title_index in self._titles.values())

    @property
    def platforms(self) -> set[str]:
        """Platforms currently indexed."""
        return set(self._titles)

    def add(self, item: HotContent) -> None:
        """Index one item. Earlier items always win a tie."""
        self._by_platform_id.setdefault(
            (item.platform.value, item.platform_content_id), item
        )
        url_key = normalize_url(item.url)
        if url_key:
            self._by_url.setdefault(url_key, item)
        title_key = normalize_title(item.title)
        if len(title_key) >= MIN_TITLE_LENGTH:
            self._titles[item.platform.value].add(title_key, item)

    def extend(self, items: Iterable[HotContent]) -> None:
        """Index many items."""
        for item in items:
            self.add(item)

    def find(self, item: HotContent) -> DuplicateMatch | None:
        """The first duplicate of ``item``, testing layers 1, 2 then 3.

        Layer 3 compares only within ``item.platform`` — see the module
        docstring for why that scoping is load-bearing.
        """
        exact = self._by_platform_id.get((item.platform.value, item.platform_content_id))
        if exact is not None:
            return DuplicateMatch(
                layer=DedupLayer.PLATFORM_CONTENT_ID,
                existing=exact,
                key=item.platform_content_id,
            )

        url_key = normalize_url(item.url)
        if url_key:
            same_url = self._by_url.get(url_key)
            if same_url is not None:
                return DuplicateMatch(layer=DedupLayer.URL, existing=same_url, key=url_key)

        title_key = normalize_title(item.title)
        if len(title_key) < MIN_TITLE_LENGTH:
            return None
        title_index = self._titles.get(item.platform.value)
        if title_index is None:
            return None
        best = title_index.best_match(title_key, self.threshold)
        if best is None:
            return None
        return DuplicateMatch(
            layer=DedupLayer.TITLE_SIMILARITY,
            existing=best[1],
            key=best[2],
            similarity=round(best[0], 4),
        )


def deduplicate(
    items: Sequence[HotContent],
    *,
    threshold: float = 0.82,
    index: DedupIndex | None = None,
) -> DedupReport:
    """Deduplicate ``items`` against ``index`` (and against each other).

    ``index`` carries previously known items — the recent database window — so a
    run also collapses against history, not only within itself.
    """
    working = index if index is not None else DedupIndex(threshold=threshold)
    report = DedupReport(counts={"input": len(items), "kept": 0, "removed": 0})
    for item in items:
        match = working.find(item)
        if match is None:
            working.add(item)
            report.kept.append(item)
            continue
        report.removed.append((item, match))
        counter = f"removed_by_{match.layer.value}"
        report.counts[counter] = report.counts.get(counter, 0) + 1
    report.counts["kept"] = len(report.kept)
    report.counts["removed"] = len(report.removed)
    logger.info(
        "dedup: %d in, %d kept, %d removed %s",
        report.counts["input"],
        report.counts["kept"],
        report.counts["removed"],
        {k: v for k, v in report.counts.items() if k.startswith("removed_by_")},
    )
    return report

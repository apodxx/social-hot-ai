"""Deterministic pre-filter: the cheap gate in front of every paid token.

The spec's cost rule is explicit — 150 items must not all reach DeepSeek — and
its section 十二 lists what to remove first: obvious advertising, obvious
marketing, empty content, duplicates, abnormal counters, unreachable items, and
obviously low-quality titles. All of that is decidable in code, so it runs here,
for free, before the model is asked anything.

Two honest notes:

* **"无法访问" is not implemented as a reachability check.** Verifying that a URL
  resolves would cost a network round trip per item (and TikHub calls for the
  platforms that need them), which defeats the purpose of a free filter. What is
  checked is structural: an unusable URL cannot survive ``normalize_url``.
* **Every rule is explainable and reversible.** The report counts drops per
  reason so a wrong rule is visible in the run output instead of silently
  shrinking the input.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.models.hot_content import HotContent
from app.services.pipeline.dedup import MIN_TITLE_LENGTH, normalize_title
from app.services.pipeline.topic_grouping import platform_name

logger = logging.getLogger(__name__)

#: Marketing / traffic-farming markers. Chinese platforms make these easy to spot.
SPAM_MARKERS: tuple[str, ...] = (
    "加微信",
    "加v",
    "加V",
    "私信我",
    "私聊",
    "代购",
    "返现",
    "刷单",
    "兼职日结",
    "日入过万",
    "免费领取",
    "点击链接",
    "优惠券",
    "招代理",
    "招商加盟",
    "微商",
    "引流",
    "带单",
    "稳赚",
    "包过",
    "低价出",
    "出售账号",
)

#: Contact details in a title are marketing, not a hot topic.
_CONTACT_RE = re.compile(
    r"(?:微信|vx|wx|qq|q群)\s*[:：]?\s*[\w-]{4,}|(?:\+?86)?1[3-9]\d{9}", re.IGNORECASE
)

#: A title that is only punctuation/emoji carries no topic.
_MEANINGFUL_RE = re.compile(r"[\w\u3400-\u4dbf\u4e00-\u9fff]")

#: Filler runs like "!!!!!!" or "aaaaaa" are noise. Restricted to non-CJK on
#: purpose: Chinese titles use repeated characters for emphasis, and a real
#: capture showed the CJK-inclusive version rejecting the perfectly good
#: "手把手教你做超长长长长长蛋挞" as low quality.
_REPEAT_RE = re.compile(r"([^\w\u3400-\u4dbf\u4e00-\u9fff])\1{4,}|([a-zA-Z])\2{4,}")


@dataclass
class FilterReport:
    """What the rule filter kept and why it dropped the rest."""

    kept: list[HotContent] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)
    dropped: list[tuple[HotContent, str]] = field(default_factory=list)

    def add_drop(self, item: HotContent, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        self.dropped.append((item, reason))

    def summary(self) -> dict[str, int]:
        return {"input": len(self.kept) + len(self.dropped), "kept": len(self.kept), **self.reasons}


def rejection_reason(item: HotContent) -> str | None:
    """Why ``item`` should not be analysed, or ``None`` to keep it."""
    title = (item.title or "").strip()
    normalized = normalize_title(title)

    if not title or not _MEANINGFUL_RE.search(title):
        return "empty_content"
    if len(normalized) < MIN_TITLE_LENGTH:
        return "low_quality_short_title"
    if _REPEAT_RE.search(title):
        return "low_quality_repeated_chars"

    lowered = title.lower()
    for marker in SPAM_MARKERS:
        if marker.lower() in lowered:
            return "advertising"
    if _CONTACT_RE.search(title):
        return "marketing_contact"

    # Abnormal counters: a real board never has 0 heat at a non-top position.
    # ``None`` means "the provider does not supply it" (Weibo) and is valid.
    if item.hot_value is not None and item.hot_value <= 0 and (item.rank or 0) > 1:
        return "abnormal_hot_value"
    for name in ("likes", "comments", "shares", "collects"):
        value = getattr(item, name, None)
        if value is not None and value < 0:
            return f"abnormal_{name}"

    return None


def rule_filter(items: Sequence[HotContent]) -> FilterReport:
    """Apply every deterministic rule; returns the survivors and the counts."""
    report = FilterReport()
    for item in items:
        reason = rejection_reason(item)
        if reason is None:
            report.kept.append(item)
        else:
            report.add_drop(item, reason)
    logger.info("rule filter: %s", report.summary())
    return report


def _priority(item: Any) -> tuple[int, int, int]:
    """Sort key: hotter first, then better rank, then platform for stability.

    Uses :func:`platform_name` because these rules run over both adapter output
    (a ``Platform`` enum) and database rows (a plain string).
    """
    return (
        -(item.hot_value or 0),
        item.rank if item.rank is not None else 10_000,
        hash(platform_name(item)) % 997,
    )


def select_candidates(items: Iterable[Any], limit: int) -> list[Any]:
    """Choose up to ``limit`` items, keeping every platform represented.

    A plain global sort by heat would let one platform's 300-item board crowd out
    the other two. This round-robins across platforms in heat order, so the paid
    analysis always sees a balanced sample.
    """
    by_platform: dict[str, list[Any]] = {}
    for item in items:
        by_platform.setdefault(platform_name(item), []).append(item)
    for bucket in by_platform.values():
        bucket.sort(key=_priority)

    platforms = sorted(by_platform)
    selected: list[Any] = []
    index = 0
    while len(selected) < limit:
        progressed = False
        for platform in platforms:
            bucket = by_platform[platform]
            if index < len(bucket):
                selected.append(bucket[index])
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
        index += 1
    return selected

"""Domain relevance scoring: the free gate that decides what the paid step sees.

Why this module exists. Selection used to be pure heat order: ``select_candidates``
sorted by ``hot_value`` with platform balance, so the 30 items sent to DeepSeek were
whatever was trending. The hot boards are dominated by entertainment, which meant an
account about technology and programming paid to analyse celebrity news — and the
account profile only got a say at scoring time, *after* the money was spent.

Two honest limits, stated because they decide how the feature is used:

* **This can only reorder and remove; it cannot create.** A board with no technology
  items yields no technology items. That is why ``WATCH_KEYWORDS`` (paid searches)
  exists alongside it.
* **A stored score would go stale.** The keyword list is edited from the settings page,
  so a column would need a backfill on every change and could disagree with the
  configured list. Relevance is therefore computed on read — matching a few hundred
  titles in Python is free, and it can never contradict the current configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Title matches count for more than body matches: a hot *word* is all title, and a
#: search hit's body often mentions the topic in passing.
TITLE_WEIGHT = 3
BODY_WEIGHT = 1
#: A matched interest keyword is worth more than a penalised negative one, so a title
#: that mentions both AI and a celebrity still ranks above an unrelated one.
NEGATIVE_PENALTY = 4


@dataclass
class InterestScore:
    """Why an item scored what it scored. Kept so the number is auditable."""

    score: int = 0
    matched: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)

    @property
    def is_relevant(self) -> bool:
        return self.score > 0

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "matched": self.matched, "negative": self.negative}


@dataclass(frozen=True)
class InterestProfile:
    """The keyword sets, compiled once per run rather than per item."""

    keywords: tuple[str, ...]
    negatives: tuple[str, ...]
    only: bool

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "InterestProfile":
        resolved = settings or get_settings()
        return cls(
            keywords=tuple(resolved.interest_keyword_list),
            negatives=tuple(resolved.interest_negative_list),
            only=bool(resolved.interest_only),
        )

    @property
    def configured(self) -> bool:
        return bool(self.keywords)


def _texts(item: Any) -> tuple[str, str]:
    """``(title, body)`` lowercased, tolerating both domain objects and DB rows."""
    title = str(getattr(item, "title", "") or "")
    body = str(getattr(item, "description", "") or "")
    return title.lower(), body.lower()


def score_item(item: Any, profile: InterestProfile) -> InterestScore:
    """Score one item against the profile. Pure, cheap, no I/O."""
    title, body = _texts(item)
    result = InterestScore()
    for keyword in profile.keywords:
        needle = keyword.lower()
        in_title = needle in title
        in_body = needle in body
        if not (in_title or in_body):
            continue
        result.matched.append(keyword)
        result.score += TITLE_WEIGHT if in_title else BODY_WEIGHT
    for keyword in profile.negatives:
        needle = keyword.lower()
        if needle in title or needle in body:
            result.negative.append(keyword)
            result.score -= NEGATIVE_PENALTY
    return result


def annotate(items: Iterable[Any], profile: InterestProfile) -> dict[int, InterestScore]:
    """Scores keyed by ``id(item)`` for the given items."""
    return {id(item): score_item(item, profile) for item in items}


def _heat(item: Any) -> float:
    return float(getattr(item, "hot_value", 0) or 0)


def select_relevant_candidates(
    items: Sequence[Any],
    limit: int,
    *,
    profile: InterestProfile | None = None,
    scores: dict[int, InterestScore] | None = None,
) -> list[Any]:
    """Pick up to ``limit`` items, most relevant first, keeping platforms balanced.

    Ordering is ``(interest score, heat, rank)``. Platform balance is preserved from
    the previous implementation — a plain global sort lets one platform's 300-item
    board crowd out the others — but within each platform the relevant items now come
    first, which is the entire point.

    When ``profile.only`` is set, items scoring at or below zero are dropped; that is
    the switch that stops paying to analyse entertainment.
    """
    resolved_profile = profile or InterestProfile.from_settings()
    if not resolved_profile.configured:
        # Nothing configured: behave exactly as before rather than silently changing
        # what the paid step sees for someone who never opted in.
        from app.services.pipeline.rule_filter import select_candidates

        return select_candidates(items, limit)

    scored = scores if scores is not None else annotate(items, resolved_profile)
    kept = [item for item in items if not resolved_profile.only or scored[id(item)].score > 0]
    dropped = len(items) - len(kept)

    by_platform: dict[str, list[Any]] = {}
    for item in kept:
        by_platform.setdefault(_platform_of(item), []).append(item)
    for bucket in by_platform.values():
        bucket.sort(
            key=lambda item: (
                -scored[id(item)].score,
                -_heat(item),
                getattr(item, "rank", None) if getattr(item, "rank", None) is not None else 10_000,
            )
        )

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

    if dropped:
        logger.info(
            "interest filter: dropped %d/%d items matching no interest keyword",
            dropped,
            len(items),
        )
    return selected


def _platform_of(item: Any) -> str:
    from app.services.pipeline.topic_grouping import platform_name

    return platform_name(item)


def relevance_summary(items: Sequence[Any], profile: InterestProfile | None = None) -> dict[str, Any]:
    """Counts per matched keyword, for the dashboard and the run report."""
    resolved = profile or InterestProfile.from_settings()
    matched: dict[str, int] = {}
    relevant = 0
    for item in items:
        score = score_item(item, resolved)
        if score.is_relevant:
            relevant += 1
        for keyword in score.matched:
            matched[keyword] = matched.get(keyword, 0) + 1
    top = sorted(matched.items(), key=lambda pair: -pair[1])[:12]
    return {
        "considered": len(items),
        "relevant": relevant,
        "coverage": round(relevant / len(items), 3) if items else 0.0,
        "top_keywords": [{"keyword": keyword, "count": count} for keyword, count in top],
    }


__all__ = [
    "InterestProfile",
    "InterestScore",
    "annotate",
    "relevance_summary",
    "score_item",
    "select_relevant_candidates",
]

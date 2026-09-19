"""Deduplication layer 4: cross-platform topic aggregation.

Items that survive layers 1-3 are clustered by title similarity **across
platforms**: the same event appearing on Weibo, Douyin and Xiaohongshu becomes
one topic with three member items, which is the spec's ``TopicGroup``
("微博/抖音/小红书 某某事件 => 同一个 Topic").

Clustering is single-link (union-find) over pairwise similarity, with the same
bigram blocking as layer 3. A cluster becomes a group only when it has at least
``min_group_size`` members — a lone item is not a "cross-platform topic", and
creating one group per item would make the table meaningless.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.services.pipeline.dedup import (
    MIN_TITLE_LENGTH,
    bigrams,
    normalize_title,
    title_similarity,
)

logger = logging.getLogger(__name__)


@dataclass
class TopicCluster:
    """One aggregated topic and its member items."""

    topic: str
    topic_normalized: str
    platforms: list[str]
    members: list[Any] = field(default_factory=list)

    @property
    def size(self) -> int:
        """Number of member items — the spec's ``related_contents``."""
        return len(self.members)


def platform_name(item: Any) -> str:
    """Platform of a domain item or a DB row, as a plain string."""
    value = getattr(item, "platform", "")
    return getattr(value, "value", None) or str(value)


def _representative(members: Sequence[Any]) -> Any:
    """The member that best names the cluster.

    Highest ``hot_value`` wins; length of title breaks ties, then insertion
    order. No provider-supplied value is invented — only chosen.
    """
    return max(
        members,
        key=lambda member: (
            getattr(member, "hot_value", None) or 0,
            len(getattr(member, "title", "") or ""),
        ),
    )


def cluster_topics(
    items: Sequence[Any],
    *,
    threshold: float = 0.82,
    min_group_size: int = 2,
) -> tuple[list[TopicCluster], int]:
    """Cluster ``items`` into cross-platform topics.

    :returns: ``(clusters, ungrouped_count)`` where ``ungrouped_count`` is the
        number of items that stayed outside every cluster.
    """
    total = len(items)
    if total == 0:
        return [], 0

    normalized = [normalize_title(getattr(item, "title", "") or "") for item in items]

    parent = list(range(total))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_bigram: dict[str, list[int]] = defaultdict(list)
    for index, key in enumerate(normalized):
        if len(key) >= MIN_TITLE_LENGTH:
            for gram in bigrams(key):
                by_bigram[gram].append(index)

    compared: set[tuple[int, int]] = set()
    for index, key in enumerate(normalized):
        if len(key) < MIN_TITLE_LENGTH:
            continue
        candidates: set[int] = set()
        for gram in bigrams(key):
            candidates.update(by_bigram.get(gram, ()))
        for other in candidates:
            if other <= index:
                continue
            pair = (index, other)
            if pair in compared:
                continue
            compared.add(pair)
            if title_similarity(key, normalized[other]) >= threshold:
                union(index, other)

    grouped: dict[int, list[Any]] = defaultdict(list)
    for index, item in enumerate(items):
        grouped[find(index)].append(item)

    clusters: list[TopicCluster] = []
    ungrouped = 0
    for members in grouped.values():
        if len(members) < min_group_size:
            ungrouped += len(members)
            continue
        representative = _representative(members)
        topic = (getattr(representative, "title", "") or "").strip()
        clusters.append(
            TopicCluster(
                topic=topic,
                topic_normalized=normalize_title(topic),
                platforms=sorted({platform_name(member) for member in members}),
                members=list(members),
            )
        )

    clusters.sort(key=lambda cluster: (-cluster.size, cluster.topic))
    logger.info(
        "topic grouping: %d items -> %d topics (%d items ungrouped, threshold=%.2f)",
        total,
        len(clusters),
        ungrouped,
        threshold,
    )
    return clusters, ungrouped


def cluster_summary(clusters: Iterable[TopicCluster]) -> dict[str, int]:
    """Counts used by the API and the run report."""
    clusters = list(clusters)
    return {
        "topics": len(clusters),
        "grouped_items": sum(cluster.size for cluster in clusters),
        "cross_platform_topics": sum(1 for cluster in clusters if len(cluster.platforms) > 1),
    }

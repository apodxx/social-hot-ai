"""Make the gap between "collected" and "analysed" impossible to miss (Phase 10).

Found by the operator asking a fair question: they searched, got 191 rows, and saw
"未分析" down the whole column. The analysis was not broken — nothing had run it. A
manual search only collects; the ``analyze`` stage lives in the pipeline, and with the
scheduler off nothing was going to reach those rows.

Two changes, because the problem has two halves:

* the search/watch endpoints can run the analysis straight after collecting
  (``analyze_after``), so one action produces a result the operator can read;
* ``GET /api/hot/pending`` reports how many stored rows have no analysis and how many of
  those would actually be sent (the interest filter decides), so the UI can offer the
  next step with a real number instead of a vague "run analysis".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.db.database import session_scope
from app.services.pipeline.interest import InterestProfile, score_item

logger = logging.getLogger(__name__)


@dataclass
class PendingAnalysis:
    """What is waiting to be analysed, and what the interest filter would do with it."""

    stored_rows: int = 0
    analysed_rows: int = 0
    pending_rows: int = 0
    eligible_rows: int = 0
    would_be_sent: int = 0
    candidate_limit: int = 0
    interest_only: bool = False
    estimated_cny: float = 0.0
    note: str = ""
    sample: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stored_rows": self.stored_rows,
            "analysed_rows": self.analysed_rows,
            "pending_rows": self.pending_rows,
            "eligible_rows": self.eligible_rows,
            "would_be_sent": self.would_be_sent,
            "candidate_limit": self.candidate_limit,
            "interest_only": self.interest_only,
            "estimated_cny": round(self.estimated_cny, 4),
            "note": self.note,
            "sample": self.sample,
        }


async def pending_analysis(settings: Settings | None = None) -> PendingAnalysis:
    """Count what an ``analyze`` run would actually do. **Free** — reads only."""
    from sqlalchemy import func, select

    from app.models.ai_analysis import AiAnalysisRecord
    from app.models.hot_content import HotContentRecord

    resolved = settings or get_settings()
    result = PendingAnalysis(
        candidate_limit=resolved.analysis_max_candidates,
        interest_only=resolved.interest_only,
    )

    async with session_scope(resolved) as session:
        result.stored_rows = (
            await session.execute(select(func.count(HotContentRecord.id)))
        ).scalar_one()
        result.analysed_rows = (
            await session.execute(select(func.count(AiAnalysisRecord.id)))
        ).scalar_one()
        pending = (
            await session.execute(
                select(HotContentRecord).outerjoin(
                    AiAnalysisRecord, AiAnalysisRecord.hot_content_id == HotContentRecord.id
                ).where(AiAnalysisRecord.id.is_(None))
            )
        ).scalars().all()

    result.pending_rows = len(pending)

    profile = InterestProfile.from_settings(resolved)
    if profile.configured:
        scored = [(row, score_item(row, profile)) for row in pending]
        relevant = [pair for pair in scored if pair[1].score > 0]
        result.eligible_rows = len(relevant) if profile.only else len(scored)
        ranked = sorted(
            relevant, key=lambda pair: (-pair[1].score, -(pair[0].hot_value or 0))
        )
    else:
        result.eligible_rows = len(pending)
        ranked = sorted(pending, key=lambda row: -(row.hot_value or 0))

    result.would_be_sent = min(result.eligible_rows, result.candidate_limit)
    # Rough per-item cost measured on this project: a 30-item batch ran about 10.5k
    # tokens total (~¥0.07), i.e. ~2.3 分 per item.
    result.estimated_cny = round(result.would_be_sent * 0.0023, 4)

    result.sample = [
        {
            "id": row.id,
            "platform": row.platform,
            "title": (row.title or "")[:40],
            "origin": row.origin,
            "image_count": row.image_count or 0,
            "interest_score": score.score if hasattr(score, "score") else 0,
        }
        for row, score in ranked[:8]
    ] if profile.configured else [
        {"id": row.id, "platform": row.platform, "title": (row.title or "")[:40]} for row in ranked[:8]
    ]

    if not result.pending_rows:
        result.note = "所有已入库内容都已分析过"
    elif not result.would_be_sent:
        result.note = (
            "没有等待分析的内容通过领域过滤（INTEREST_ONLY=true）。"
            "先跑「领域搜索」补充相关内容，或放宽 INTEREST_KEYWORDS。"
        )
    elif result.eligible_rows > result.candidate_limit:
        result.note = (
            f"有 {result.eligible_rows} 条符合领域，本轮只送前 {result.candidate_limit} 条"
            "（按领域相关性排序）；再跑一次会继续处理剩下的"
        )
    else:
        result.note = f"下一轮会分析 {result.would_be_sent} 条"
    return result


async def run_analysis_after_collection(
    settings: Settings | None = None, *, limit: int | None = None
) -> dict[str, Any]:
    """Run the analysis stage on demand. **Spends DeepSeek tokens.**

    Used by the search/watch endpoints when the caller asks for it, and by the
    standalone "分析新内容" action.
    """
    resolved = settings or get_settings()
    from app.services.ai.analyzer import run_analysis

    logger.warning("running analysis on demand — billed DeepSeek calls")
    result = await run_analysis(limit=limit, settings=resolved)
    return result.as_dict()


__all__ = ["PendingAnalysis", "pending_analysis", "run_analysis_after_collection"]

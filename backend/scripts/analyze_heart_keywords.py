"""分析「C语言爱心」那条：提取关键词，并找出库里类似的内容。

只读数据库，免费。

    python scripts/analyze_heart_keywords.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select

from app.core.config import get_settings  # noqa: E402
from app.db.database import session_scope  # noqa: E402
from app.models.ai_rewrite import AiRewriteRecord  # noqa: E402
from app.models.hot_content import HotContentRecord  # noqa: E402

TARGET_ID = 488
#: 编程类内容的通用标签——用于在库里找"同类"。
PROGRAM_HINTS = (
    "c语言", "python", "编程", "代码", "程序员", "计算机", "算法",
    "源码", "程序设计", "编程学习", "计算机专业",
)


async def main() -> int:
    settings = get_settings()
    async with session_scope(settings) as session:
        target = (
            await session.execute(
                select(HotContentRecord).where(HotContentRecord.id == TARGET_ID)
            )
        ).scalars().first()
        if target is None:
            print(f"找不到 id={TARGET_ID}")
            return 1

        print("=" * 66)
        print(f"目标内容  id={target.id}  [{target.platform}]")
        print(f"标题：{target.title}")
        print(f"正文：{target.description}")
        print(f"链接：{target.url}")
        print()

        # 1) 从标题+正文里抽标签关键词
        text = f"{target.title}\n{target.description}"
        tags = re.findall(r"#([^\s#]+)", text)
        print(f"原文里的标签（{len(tags)} 个）：")
        for tag in tags:
            print(f"  #{tag}")
        print()

        # 2) 标题分词：抽出实词（中文按 2-4 字切，够用就行）
        title_words = [
            word
            for word in re.findall(r"[A-Za-z]{2,}|[\u4e00-\u9fa5]{2,4}", target.title)
            if word not in ("怎么", "效果", "什么", "可以", "如何")
        ]
        print(f"标题里可作检索词的部分：{title_words}")
        print()

        # 3) 这条有没有二创
        rewrite = (
            await session.execute(
                select(AiRewriteRecord).where(AiRewriteRecord.hot_content_id == TARGET_ID)
            )
        ).scalars().first()
        print(f"是否已二创：{'是' if rewrite else '否'}")
        if rewrite:
            print(f"  状态：{rewrite.status}")
            if rewrite.xiaohongshu_title:
                print(f"  小红书标题：{rewrite.xiaohongshu_title}")
        print()

        # 4) 库里同类内容——按这些关键词找
        rows = (
            await session.execute(
                select(HotContentRecord)
                .where(
                    HotContentRecord.id != TARGET_ID,
                    *[
                        HotContentRecord.title.like(f"%{hint}%")
                        for hint in ("爱心",)
                    ],
                )
                .order_by(HotContentRecord.id.desc())
                .limit(20)
            )
        ).scalars().all()
        print(f"库里标题含「爱心」的同类内容：{len(rows)} 条")
        for record in rows:
            print(f"  id={record.id} [{record.platform}] {(record.title or '')[:52]}")

        # 5) 更宽的编程类统计，看这个领域库里有多少存货
        print()
        counts: Counter[str] = Counter()
        for hint in PROGRAM_HINTS:
            total = (
                await session.execute(
                    select(HotContentRecord.id).where(
                        HotContentRecord.title.like(f"%{hint}%")
                    )
                )
            ).scalars().all()
            if total:
                counts[hint] = len(total)
        print("库里编程相关内容的库存：")
        for hint, number in counts.most_common():
            print(f"  {hint}: {number} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

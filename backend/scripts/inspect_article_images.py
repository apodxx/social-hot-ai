"""诊断：文章配图到底搜到了什么、和主题有多相关。

背景：生成文章时用主题词调一次小红书图片搜索，拿回来的其实是一个个**笔记**，
配图是那些笔记的**封面**。所以"搜到图"不等于"图与主题相关"——封面由笔记作者决定，
可能是一张书桌、一份手写笔记、甚至自拍。

这个脚本把每篇文章配图的来源笔记标题打出来，用来看相关度差在哪。

    python scripts/inspect_article_images.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.knowledge_service import list_articles  # noqa: E402


async def main() -> int:
    settings = get_settings()
    rows, _total = await list_articles(settings, limit=3)
    if not rows:
        print("库里还没有文章")
        return 1

    for record in rows:
        images = record.images or []
        print("=" * 70)
        print(f"id={record.id} 《{record.title}》")
        print(f"主题词：{record.topic}   配图 {len(images)} 张")
        print()
        hit = 0
        for index, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                continue
            note_title = str(image.get("title") or "")
            desc = str(image.get("description") or "")
            author = str(image.get("author") or "")
            # 简单的相关度判据：来源笔记的标题或正文里是否出现主题词。
            relevant = record.topic in note_title or record.topic in desc
            hit += 1 if relevant else 0
            flag = "相关" if relevant else "不相关"
            print(f"  [{index}] {flag}  来源笔记：{note_title[:44]}")
            print(f"        作者：{author[:16]}   出处描述：{desc[:60]}")
        print()
        print(f"  -> 标题/正文里出现主题词「{record.topic}」的：{hit}/{len(images)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""对比新旧提示词的改写质量：看**原文的具体信息（型号/价格）有没有被保留**。

用真实的 OCR 素材（那条抖音图文笔记的内容）。花费约 ¥0.002。

    python scripts/try_convert_quality.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.agent import convert_text_to_note  # noqa: E402

#: 真实 OCR 输出（截取）：这条笔记的价值**全在型号和价格上**。
MATERIAL = """（以下是一条抖音图文笔记的内容）

作者写的文案：学生党怎么选笔记本？全价位保姆级推荐！专门给你们整理的轻薄本推荐来啦！
轻便好带、续航够用、上课学习、追剧剪视频全能搞定，从性价比到旗舰款，闭眼选不踩雷！
评论区留下你的预算+专业，我直接帮你挑最合适的！#学生党笔记本推荐 #轻薄本推荐

图片上的文字（OCR）：
3500以下
I：适合进行简单办公。
II: 对品控做工要求不高的实用性用户。
荣耀 MagicBookX14锐龙版 R7 7640HS 2718元起
联想小新15 2024 i5 13420H 3119元起
无畏14X R7 8745H 3279元起
宏碁非凡Goopro
惠普战99锐龙版 i5 13500H 3359元起
3500-4500
I：性价比不错，且配置缩水不多。
II: 比较适合学习工作，实用性强。
惠普战66七代锐龙版 R7 7735U 3599元起
联想 ThinkBook SE
"""

#: 原文里的关键事实，改写后应当保留。
KEY_FACTS = [
    "2718",
    "3119",
    "3279",
    "3359",
    "3599",
    "MagicBookX14",
    "小新15",
    "无畏14X",
    "战99",
    "战66",
]


async def main() -> int:
    settings = get_settings()
    result = await convert_text_to_note(MATERIAL, platform="xiaohongshu", settings=settings)
    if not result.ok:
        print(f"失败：{result.error}")
        return 1

    print("=" * 66)
    print(result.text)
    print("=" * 66)
    print()
    print(f"字数 {len(result.text)}   花费 ¥{result.usage_cny:.4f}")
    print()
    kept = [fact for fact in KEY_FACTS if fact in result.text]
    lost = [fact for fact in KEY_FACTS if fact not in result.text]
    print(f"原文关键信息保留 {len(kept)}/{len(KEY_FACTS)}")
    print(f"  保留：{kept}")
    print(f"  丢失：{lost}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

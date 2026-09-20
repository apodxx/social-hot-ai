"""复现「OCR 成功但改写失败：模型没有输出内容（finish_reason=stop）」。

用真实的 OCR 输出当输入（约 700 字），看 DeepSeek 到底返回了什么。
花 1 次 DeepSeek 调用（约 ¥0.002-0.01）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

#: 用户实际遇到的那次 OCR 输出（截取到出错前的样子）。
OCR_TEXT = """! 诗歌鉴赏要积累熟悉常见诗人的典型风格和一些常见意象，这样就算遇到自己不熟悉的作品，
也可以凭借作者和意向分析出诗歌要表达的情感！现代文阅读比较简单，像主观题一般都有固定框架，
可以积累像"手法+内容+作用"的模板
! 作文不会写就背模板!背感谢信、申请信等万能开头结尾和高级替换词，写的时候直接套用就行，
最好每周保持2-3篇的写作量，写完对照范文修改语法和逻辑，这样提分会比较快
- 语文 ! 文学常识很拉分，一定要多背多记！中外文学常识的分值比例是9：1，所以备考重点记那些
作家的朝代、代表作品、文学流派及风格之类的，怕记混可以按照朝代时间线梳理积累
! 数制转换、硬件系统、网络协议这些比较抽象，可以用对比记忆法、图表辅助法等帮助理解记忆，
试着列个表格去对比RAM和ROM的特性，用拓扑图来理解网络结构，比死记硬背效果要好"""


async def main() -> int:
    from app.services.ai.agent import CONVERT_SYSTEM_PROMPT, PLATFORM_NAMES
    from app.services.ai.deepseek import DeepSeekClient

    settings = get_settings()
    client = DeepSeekClient(settings)
    try:
        material = f"（以下内容来自图片 OCR 识别）\n\n{OCR_TEXT}"
        user = f"目标平台：{PLATFORM_NAMES['xiaohongshu']}\n\n原始材料：\n{material}"
        bot = (await client.list_models())[:5]
        print(f"可用模型（前 5）：{bot}")
        print(f"当前模型：{settings.deepseek_model}")
        print(f"输入长度：{len(user)} 字符")
        print()
        # 用与 convert_text_to_note 完全一样的参数。
        result = await client.chat(
            [
                {"role": "system", "content": CONVERT_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            json_mode=False,
            temperature=0.8,
            max_tokens=4096,
        )
        print(f"finish_reason = {result.finish_reason!r}")
        print(f"len(content)  = {len(result.content)}")
        print(f"usage         = prompt={result.usage.prompt_tokens} "
              f"completion={result.usage.completion_tokens} total={result.usage.total_tokens}")
        print()
        print("content 前 200 字：")
        print(result.content[:200] or "(空)")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""抓一次模型原始响应，用于离线开发 JSON 容错（1 次调用，约 ¥0.02）。

为什么需要：报错只说"不是合法 JSON"，而**只有拿到真实字节才能确定是哪种语法问题**
（未转义的换行？ASCII 引号？尾逗号？）。抓一次存盘，之后所有修复尝试都是离线的。

    python scripts/capture_broken_json.py 神经网络
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.ai.deepseek import DeepSeekClient  # noqa: E402
from app.services.ai.knowledge import ARTICLE_SYSTEM_PROMPT, build_article_prompt  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "broken_article_raw.txt"


def diagnose(text: str) -> None:
    """报告 JSON 到底哪里坏了。"""
    print(f"长度: {len(text)} 字符")
    print(f"结尾 60 字符: {text[-60:]!r}")
    try:
        json.loads(text)
        print("直接解析: 成功（那问题不在内容本身）")
        return
    except json.JSONDecodeError as exc:
        print(f"直接解析失败: {exc}")
        if exc.pos < len(text):
            start = max(0, exc.pos - 70)
            print(f"  出错位置附近: {text[start:exc.pos + 40]!r}")

    # 逐个试候选修法
    candidates: list[tuple[str, str]] = [
        ("strict=False（允许字符串内出现控制字符如真实换行）", "strict_false"),
        ("去掉尾逗号", "trailing_comma"),
    ]
    try:
        json.loads(text, strict=False)
        print("  ✅ strict=False 可解析 —— 病因是**字符串里有未转义的控制字符（换行/制表）**")
    except json.JSONDecodeError as exc:
        print(f"  strict=False 仍失败: {exc}")

    if "```" in text:
        print("  含代码块围栏")
    # 统计字符串内出现的裸引号：粗看有多少个 "
    print(f"  裸双引号数量: {text.count(chr(34))}")
    print(f"  真实换行数量: {text.count(chr(10))}")


async def main(topic: str) -> int:
    settings = get_settings()
    client = DeepSeekClient(settings)
    print(f"请求「{topic}」的文章（不解析，直接拿原始文本）…")
    result = await client.chat(
        [
            {"role": "system", "content": ARTICLE_SYSTEM_PROMPT},
            {"role": "user", "content": build_article_prompt(topic)},
        ],
        json_mode=True,
        temperature=0.7,
        max_tokens=settings.article_max_tokens,
    )
    await client.aclose()

    print(f"finish_reason={result.finish_reason!r} tokens={result.usage.completion_tokens}")
    print()
    diagnose(result.content)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(result.content, encoding="utf-8")
    print()
    print(f"原始响应已存到 {OUT}（之后离线调试用，不再花钱）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "神经网络")))

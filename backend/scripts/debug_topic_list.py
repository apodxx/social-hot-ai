"""诊断：选题清单为什么是 0 个。打印模型原始返回。花约 ¥0.01。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402


async def main() -> int:
    from app.services.ai.agent import TOPIC_LIST_SYSTEM
    from app.services.ai.deepseek import DeepSeekClient

    settings = get_settings()
    client = DeepSeekClient(settings)
    try:
        result = await client.chat(
            [
                {"role": "system", "content": TOPIC_LIST_SYSTEM},
                {"role": "user", "content": "请给出这 20 个选题。"},
            ],
            json_mode=False,
            temperature=0.9,
            max_tokens=2048,
        )
        print(f"finish_reason = {result.finish_reason!r}")
        print(f"len(content)  = {len(result.content)}")
        print(f"usage         = prompt={result.usage.prompt_tokens} "
              f"completion={result.usage.completion_tokens}")
        print(f"tool_calls    = {len(result.tool_calls)}")
        print()
        print("--- content 前 400 字 ---")
        print(result.content[:400] or "(空)")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

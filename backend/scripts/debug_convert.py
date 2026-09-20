"""调试：``convert_text_to_note`` 为什么返回空。打印原始 completion 的元信息。

只调用一次 DeepSeek（约 ¥0.01）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402


async def main() -> int:
    from app.services.ai.agent import CONVERT_SYSTEM_PROMPT, PLATFORM_NAMES
    from app.services.ai.deepseek import DeepSeekClient

    settings = get_settings()
    material = "树莓派 5 换上了 PCIe 接口，SSD 速度翻了五倍"
    client = DeepSeekClient(settings)
    try:
        result = await client.chat(
            [
                {"role": "system", "content": CONVERT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"目标平台：{PLATFORM_NAMES['xiaohongshu']}\n\n原始材料：\n{material}",
                },
            ],
            json_mode=False,
            temperature=0.8,
            max_tokens=1200,
        )
        print(f"model         = {result.model}")
        print(f"finish_reason = {result.finish_reason!r}")
        print(f"content 长度  = {len(result.content)}")
        print(f"usage         = prompt={result.usage.prompt_tokens} "
              f"completion={result.usage.completion_tokens} total={result.usage.total_tokens}")
        print(f"tool_calls    = {result.tool_calls}")
        print()
        print("content 前 300 字：")
        print(result.content[:300] or "(空)")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

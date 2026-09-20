"""逐行复刻 ``convert_text_to_note`` 的函数体，逐步打印，定位差异。

与 ``debug_convert.py`` 的唯一区别是这里用**完全相同的代码路径**（同一个提示词常量、
同样的参数），但把每一步都打出来。
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
    source = "树莓派 5 换上了 PCIe 接口，SSD 速度翻了五倍"
    platform = "xiaohongshu"

    material = (source or "").strip()
    wanted = platform if platform in PLATFORM_NAMES else "xiaohongshu"
    print(f"material      = {material!r}")
    print(f"wanted        = {wanted!r}")
    print(f"is url        = {material.startswith(('http://', 'https://'))}")

    user_message = f"目标平台：{PLATFORM_NAMES[wanted]}\n\n原始材料：\n{material}"
    print(f"user_message  = {user_message!r}")
    print(f"system 长度   = {len(CONVERT_SYSTEM_PROMPT)}")

    client = DeepSeekClient(settings)
    try:
        result = await client.chat(
            [
                {"role": "system", "content": CONVERT_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            json_mode=False,
            temperature=0.8,
            max_tokens=1200,
        )
        print()
        print(f"finish_reason = {result.finish_reason!r}")
        print(f"len(content)  = {len(result.content)}")
        print(f"tool_calls    = {len(result.tool_calls)}")
        print(f"usage         = {result.usage.total_tokens}")
        print(f"repr 前 60    = {result.content[:60]!r}")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

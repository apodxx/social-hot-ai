"""验证「有图用原图、只有视频才生图」这条规则落到代码里了。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app.services.ai.agent as agent  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "qq_bot", Path(__file__).resolve().parent / "qq_bot.py"
)
bot = importlib.util.module_from_spec(spec)
sys.modules["qq_bot"] = bot
spec.loader.exec_module(bot)

source = (Path(__file__).resolve().parent / "qq_bot.py").read_text(encoding="utf-8")
checks = [
    ("Conversion 有 kind 字段", "kind" in agent.Conversion.__dataclass_fields__),
    ("qq_bot 有「只有视频才生图」分支", 'if conversion.kind == "video":' in source),
    ("生图工具描述里劝退带图链接", "不要用它" in agent.find_tool("generate_images").description),
    ("抖音分支记录 kind", 'kind = "video" if material_result.kind == "video"' in (
        Path(__file__).resolve().parents[1] / "app" / "services" / "ai" / "agent.py"
    ).read_text(encoding="utf-8")),
]
for label, ok in checks:
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}")
print()
print("全部通过 ✅" if all(ok for _, ok in checks) else "有未通过项 ❌")

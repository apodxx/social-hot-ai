"""实测 DashScope 的 OCR / Omni 端点（不装 openai SDK，直接用 httpx）。

两个端点不一样，别混：
  * OCR 用的是**私有部署**域名（``ws-*.cn-beijing.maas.aliyuncs.com``），
    和标准 ``dashscope.aliyuncs.com`` 不同；
  * Omni 用标准域名。

    python scripts/try_vision_models.py ocr
    python scripts/try_vision_models.py omni
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

#: 用户给的示例图（阿里云文档里的标准测试图）。
SAMPLE_IMAGE = (
    "https://help-static-aliyun-doc.aliyuncs.com/file-manage-files/zh-CN/20241108/ctdzex/biaozhun.jpg"
)
OCR_BASE = "https://ws-hqi61ztzpsmcrcj9.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
OMNI_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


async def call(base: str, model: str, messages: list[dict], api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        response = await client.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages},
        )
    try:
        return {"status": response.status_code, "body": response.json()}
    except Exception:  # noqa: BLE001
        return {"status": response.status_code, "body": response.text[:400]}


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ocr"
    settings = get_settings()
    key = settings.dashscope_api_key
    if not key:
        print("DASHSCOPE_API_KEY 未配置")
        return 1
    print(f"key 长度 {len(key)}，前缀 {key[:6]}…")
    print()

    if mode == "ocr":
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": SAMPLE_IMAGE}},
                    {"type": "text", "text": "请仅输出图像中的文本内容。"},
                ],
            }
        ]
        for model in ("qwen3.5-ocr", "qwen-vl-ocr", "qwen-vl-ocr-latest"):
            result = await call(OCR_BASE, model, messages, key)
            print(f"--- OCR 端点 / {model} -> HTTP {result['status']}")
            body = result["body"]
            if isinstance(body, dict):
                if body.get("choices"):
                    text = (body["choices"][0].get("message") or {}).get("content") or ""
                    print(f"    识别结果（前 200 字）：{text[:200]!r}")
                    print(f"    usage: {body.get('usage')}")
                else:
                    print(f"    {json.dumps(body, ensure_ascii=False)[:300]}")
            else:
                print(f"    {body}")
            print()
    else:
        messages = [{"role": "user", "content": "你是谁？"}]
        result = await call(OMNI_BASE, "qwen3.8-omni-flash", messages, key)
        print(f"--- Omni / qwen3.8-omni-flash -> HTTP {result['status']}")
        body = result["body"]
        if isinstance(body, dict) and body.get("choices"):
            print(f"    {(body['choices'][0].get('message') or {}).get('content','')[:200]!r}")
            print(f"    usage: {body.get('usage')}")
        else:
            print(f"    {json.dumps(body, ensure_ascii=False)[:400] if isinstance(body, dict) else body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

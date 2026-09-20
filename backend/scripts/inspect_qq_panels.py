"""查看现有指令面板的**原始返回**，确认数据结构。只读，不改动任何配置。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

BASE = "https://api.sgroup.qq.com"


async def main() -> int:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        token = (
            await client.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": settings.qq_app_id, "clientSecret": settings.qq_app_secret},
            )
        ).json()["access_token"]
        headers = {"Authorization": f"QQBot {token}", "User-Agent": "SocialHotAI/1.0 (QQBot)"}

        for scope in ("group", "c2c"):
            response = await client.get(
                f"{BASE}/v2/panels", params={"scope": scope}, headers=headers
            )
            print(f"=== scope={scope}  HTTP {response.status_code} ===")
            try:
                print(json.dumps(response.json(), ensure_ascii=False, indent=2)[:900])
            except Exception:  # noqa: BLE001
                print(response.text[:400])
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

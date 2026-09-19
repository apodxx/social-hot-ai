"""Report the TikHub account balance/usage — a free metadata call.

Run before planning any billed work so the budget is a number, not a guess:

    python scripts/check_tikhub_balance.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402

PATH = "/api/v1/tikhub/user/get_user_info"


def main() -> int:
    settings = get_settings()
    if not settings.tikhub_configured:
        print("TIKHUB_API_KEY is not configured")
        return 1
    url = f"{settings.tikhub_base_url.rstrip('/')}{PATH}"
    # Free metadata endpoint; the app already uses it as its health probe.
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        response = client.get(
            url, headers={"Authorization": f"Bearer {settings.tikhub_api_key}"}
        )
    print("HTTP", response.status_code)
    try:
        payload = response.json()
    except ValueError:
        print(response.text[:400])
        return 1

    # Print the interesting keys without dumping whatever else the provider returns.
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    interesting = {
        key: value
        for key, value in (data or {}).items()
        if any(
            token in key.lower()
            for token in ("balance", "quota", "credit", "usd", "amount", "expire", "email", "api_key_name")
        )
    }
    print(json.dumps(interesting or data, ensure_ascii=False, indent=2)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

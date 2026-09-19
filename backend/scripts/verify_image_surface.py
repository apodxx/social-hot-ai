"""Verify the image-generation surface end to end (the free parts).

Reads back what a real generation produced: the audit row, the spend total, and whether
the file is actually served. Run after a generation, or any time to check configuration.

    python scripts/verify_image_surface.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402

BASE = "http://127.0.0.1:8000"
CLIENT = httpx.Client(base_url=BASE, timeout=60.0, trust_env=False)

#: Stored paths are relative to the *project* root (``media/xx/yy.png``), so they must be
#: resolved against it rather than the current working directory — otherwise this script
#: reports a missing file purely because it was run from ``backend/``.
PROJECT_ROOT = get_settings().media_root_path.parent

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(name)


def main() -> int:
    estimate = CLIENT.get("/api/image/estimate").json()
    print("=== pricing policy ===")
    print(f"  model / size      : {estimate['model']} / {estimate['size']}")
    print(f"  price per image   : ${estimate['price_usd_per_image']:.5f} = CNY {estimate['price_cny_per_image']}")
    print(f"  max per run       : {estimate['max_per_run']}")
    check("the provider is configured", bool(estimate["configured"]))
    check("generation is enabled", bool(estimate["enabled"]))
    # The policy the operator asked for: one image, 1K tier.
    width, _, height = estimate["size"].partition("*")
    check(
        "the configured size is inside the 1K tier",
        int(width) * int(height) <= 2_250_000,
        f"{int(width) * int(height)} px",
    )
    check(
        "the price is the 1K price",
        abs(estimate["price_usd_per_image"] - 0.03438) < 1e-6,
        f"${estimate['price_usd_per_image']}",
    )

    generations = CLIENT.get("/api/image/generations", params={"limit": 5}).json()
    print("\n=== stored generations ===")
    print(f"  total: {generations['total']}  spent in this page: ${generations['estimated_usd_in_page']}")
    check("the spend field is present", "estimated_usd_in_page" in generations)

    if not generations["items"]:
        print("\n  (none yet — the UI's 图片二创 card is where the first one comes from)")
        return 0 if not failures else 1

    for item in generations["items"][:3]:
        usage = item.get("usage") or {}
        print(
            f"  id={item['id']} {item['status']} {item['size']} "
            f"out={usage.get('output_image_count')} ({usage.get('output_image_type')}) "
            f"${item['estimated_usd']} refs={len(item['reference_paths'])}"
        )
        check(
            f"generation {item['id']} produced exactly one image",
            int(usage.get("output_image_count") or 0) == 1,
            str(usage.get("output_image_count")),
        )
        check(
            f"generation {item['id']} was billed in the 1K tier",
            "1k" in str(usage.get("output_image_type") or "").lower(),
            str(usage.get("output_image_type")),
        )

        for path in item["local_paths"]:
            local = PROJECT_ROOT / path
            check(f"{local.name} exists on disk", local.is_file())
            if local.is_file():
                data = local.read_bytes()
                check(f"{local.name} is a real PNG", data[:8] == b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", data[16:24])
                check(
                    f"{local.name} is {width}x{height}",
                    width * height <= 2_250_000,
                    f"{width * height} px",
                )
            served = CLIENT.get(f"/{path}")
            check(
                f"/{path} is served",
                served.status_code == 200 and len(served.content) > 1000,
                f"HTTP {served.status_code}, {len(served.content)} bytes",
            )

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed (nothing was spent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

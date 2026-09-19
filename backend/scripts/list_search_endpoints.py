"""Which TikHub endpoints support searching by keyword, per platform?

Section 1 of the user's question ("can I search for topics I care about") depends on
what the provider actually offers, so this reads TikHub's published OpenAPI document
and prints the search-capable paths. The document itself is free and needs no key.

The filtered result is saved to ``docs/tikhub_search_endpoints.txt`` so the answer is
reviewable later without another network call.

    python scripts/list_search_endpoints.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

SPEC_URL = "https://api.tikhub.io/openapi.json"
OUTPUT = Path(__file__).resolve().parents[2] / "docs" / "tikhub_search_endpoints.txt"

#: Words that mean "this endpoint takes a query and returns matching content".
SEARCH_TOKENS = ("search", "keyword", "query")

#: Endpoints whose parameters include one of these are searchable by construction.
QUERY_PARAM_NAMES = {"keyword", "query", "q", "search_word", "word", "kw"}


def main() -> int:
    print(f"fetching {SPEC_URL} (free, no key)...")
    # trust_env=False for the same reason the app uses it: the machine's registry
    # proxy turns local calls into 502s, and this project talks to TikHub directly.
    with httpx.Client(timeout=60.0, trust_env=False, follow_redirects=True) as client:
        response = client.get(SPEC_URL)
    response.raise_for_status()
    spec = response.json()
    paths = spec.get("paths", {})
    print(f"document lists {len(paths)} paths\n")

    lines: list[str] = []
    for platform in ("xiaohongshu", "weibo", "douyin"):
        matches: list[tuple[str, str]] = []
        for path, operations in sorted(paths.items()):
            if f"/{platform}/" not in path:
                continue
            name_hit = any(token in path.lower() for token in SEARCH_TOKENS)
            param_hit = False
            params_seen: list[str] = []
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                for parameter in operation.get("parameters") or []:
                    if not isinstance(parameter, dict):
                        continue
                    param_name = str(parameter.get("name", "")).lower()
                    params_seen.append(param_name)
                    if param_name in QUERY_PARAM_NAMES:
                        param_hit = True
            if name_hit or param_hit:
                reason = []
                if name_hit:
                    reason.append("path")
                if param_hit:
                    reason.append("param:" + ",".join(sorted(set(params_seen) & QUERY_PARAM_NAMES)))
                matches.append((path, " ".join(reason)))

        lines.append(f"=== {platform}: {len(matches)} searchable paths")
        print(f"=== {platform}: {len(matches)} searchable paths")
        for path, reason in matches:
            lines.append(f"  {path}   [{reason}]")
            print(f"  {path}   [{reason}]")
        lines.append("")
        print()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"saved to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Save TikHub's OpenAPI document locally and inspect any endpoint's parameters.

TikHub publishes no response schemas, but it does publish request parameters. Reading
them offline avoids guessing at required arguments — and a billed call that fails
argument validation is money spent on nothing.

    python scripts/inspect_tikhub_endpoint.py                       # save + summary
    python scripts/inspect_tikhub_endpoint.py /api/v1/weibo/web_v2/fetch_pic_search
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

SPEC_URL = "https://api.tikhub.io/openapi.json"
SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "tikhub_openapi.json"


def load_spec(refresh: bool = False) -> dict[str, Any]:
    """The cached document, fetched once and reused (the fetch itself is free)."""
    if SPEC_PATH.exists() and not refresh:
        return json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    print(f"fetching {SPEC_URL} ...")
    with httpx.Client(timeout=90.0, trust_env=False, follow_redirects=True) as client:
        response = client.get(SPEC_URL)
    response.raise_for_status()
    spec = response.json()
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    print(f"saved {SPEC_PATH} ({SPEC_PATH.stat().st_size // 1024} KB)")
    return spec


def describe_endpoint(spec: dict[str, Any], path: str) -> None:
    operations = spec.get("paths", {}).get(path)
    if operations is None:
        print(f"{path}: NOT FOUND in the document")
        return
    for method, operation in operations.items():
        if not isinstance(operation, dict):
            continue
        print(f"\n{method.upper()} {path}")
        summary = operation.get("summary") or operation.get("description") or ""
        print(f"  summary: {str(summary).strip()[:200]}")
        parameters = operation.get("parameters") or []
        if not parameters:
            print("  parameters: none declared")
        for parameter in parameters:
            if not isinstance(parameter, dict):
                continue
            schema = parameter.get("schema") or {}
            kind = schema.get("type") or schema.get("anyOf") or schema.get("$ref") or "?"
            if isinstance(kind, list):
                kind = "/".join(str(item.get("type", item)) for item in kind)
            default = schema.get("default")
            enum = schema.get("enum")
            required = "REQUIRED" if parameter.get("required") else "optional"
            extra = ""
            if default is not None:
                extra += f" default={default!r}"
            if enum:
                extra += f" enum={enum}"
            print(
                f"  - {parameter.get('name')} ({kind}, {required}, in={parameter.get('in')})"
                f"{extra}"
            )
            description = (parameter.get("description") or "").strip().replace("\n", " ")
            if description:
                print(f"      {description[:160]}")
        request_body = operation.get("requestBody")
        if request_body:
            print(f"  requestBody: {json.dumps(request_body, ensure_ascii=False)[:400]}")


def main() -> int:
    wanted = sys.argv[1:]
    spec = load_spec(refresh="--refresh" in wanted)
    paths = spec.get("paths", {})
    print(f"document: {len(paths)} paths, saved at {SPEC_PATH}")

    if not wanted:
        return 0

    for path in [item for item in wanted if item.startswith("/")]:
        describe_endpoint(spec, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

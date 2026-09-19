"""Print the API route table.

The frontend has to call exactly the paths the backend exposes, and a route that
was renamed during refactoring would otherwise only surface as a 404 in the
browser. Run this after touching any router:

    python scripts/list_routes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.main import create_app  # noqa: E402


def main() -> int:
    app = create_app()
    # Read the generated schema rather than walking ``app.routes``: this Starlette
    # version wraps included routers in an opaque ``_IncludedRouter`` container, so
    # the flat route list is empty even though every endpoint exists. The OpenAPI
    # paths are also exactly what the frontend can call.
    schema = app.openapi()
    rows: list[tuple[str, str, str]] = []
    for path, operations in schema.get("paths", {}).items():
        if not path.startswith("/api"):
            continue
        verbs = ",".join(
            sorted(verb.upper() for verb in operations if verb in {"get", "post", "put", "delete", "patch"})
        )
        summary = ""
        for operation in operations.values():
            if isinstance(operation, dict) and operation.get("summary"):
                summary = str(operation["summary"])
                break
        rows.append((path, verbs, summary))

    width = max((len(path) for path, _, _ in rows), default=0)
    for path, verbs, summary in sorted(rows):
        print(f"{path:<{width}}  {verbs:<10}  {summary}")
    print(f"\n{len(rows)} API routes")

    billed = [path for path, verbs, _ in rows if "POST" in verbs and path.startswith("/api")]
    print("billed (POST) endpoints: " + (", ".join(sorted(billed)) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

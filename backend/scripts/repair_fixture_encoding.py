"""Repair Latin-1-mangled UTF-8 in captured fixtures, without re-calling TikHub.

Background: capturing a JSON response with PowerShell's ``Invoke-WebRequest``
decodes the body as ISO-8859-1 when the ``Content-Type`` carries no charset, so
Chinese text arrived as runs of Latin-1 accented characters. Because Latin-1 maps
all 256 byte values, the original bytes are recoverable exactly — which is why
this costs nothing, unlike re-running the capture.

The correct capture path is ``scripts/discover_raw.py`` (httpx, true UTF-8). This
script exists to repair files already captured the wrong way, and reports every
file it leaves alone so nothing changes silently.

Usage::

    python scripts/repair_fixture_encoding.py --check     # report only
    python scripts/repair_fixture_encoding.py --apply     # rewrite damaged files
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.tikhub.base import demangle_latin1, looks_latin1_mangled  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "raw"


def walk_strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(json-ish path, value)`` for every string in ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk_strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def repair_tree(node: Any) -> tuple[Any, int, int]:
    """Return ``(repaired_node, damaged, unrecoverable)``."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        damaged = unrecoverable = 0
        for key, value in node.items():
            fixed, child_damaged, child_unrecoverable = repair_tree(value)
            out[key] = fixed
            damaged += child_damaged
            unrecoverable += child_unrecoverable
        return out, damaged, unrecoverable
    if isinstance(node, list):
        items = []
        damaged = unrecoverable = 0
        for value in node:
            fixed, child_damaged, child_unrecoverable = repair_tree(value)
            items.append(fixed)
            damaged += child_damaged
            unrecoverable += child_unrecoverable
        return items, damaged, unrecoverable
    if isinstance(node, str) and looks_latin1_mangled(node):
        recovered = demangle_latin1(node)
        if recovered is None:
            return node, 1, 1
        return recovered, 1, 0
    return node, 0, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report damage, change nothing")
    group.add_argument("--apply", action="store_true", help="rewrite the damaged files")
    args = parser.parse_args()

    files = sorted(FIXTURE_DIR.glob("*.json"))
    if not files:
        print(f"no fixtures in {FIXTURE_DIR}")
        return 1

    total_damaged = 0
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        repaired, damaged, unrecoverable = repair_tree(payload)
        total_damaged += damaged
        if damaged == 0:
            print(f"{path.name}: clean")
            continue
        note = f"{damaged} mangled string(s)"
        if unrecoverable:
            note += f", {unrecoverable} NOT recoverable"
        print(f"{path.name}: {note}")
        if args.apply and unrecoverable == 0:
            path.write_text(
                json.dumps(repaired, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"  repaired -> {path.name}")
        elif args.apply:
            print("  left alone: partial damage is not rewritten automatically")

    print(f"\ntotal mangled strings found: {total_damaged}")
    if args.check and total_damaged:
        print("re-run with --apply to repair")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

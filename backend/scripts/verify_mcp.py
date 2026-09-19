"""Verify the live MCP endpoint over real HTTP, exactly as a client would use it.

An in-process test proves the tools work; only a socket proves the *endpoint* works
(mount path, trailing slash, Host validation, session-manager lifespan). This script
also never calls a billed tool: it lists them and reports what it found.

    python scripts/verify_mcp.py [base_url]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The Windows registry proxy (ProxyEnable=1 / ProxyServer=127.0.0.1:7890) is visible to
# httpx through urllib.getproxies(), which turns a call to 127.0.0.1 into a 502 with an
# empty body and *nothing in the server log* — the request never arrives. The MCP SDK's
# HTTP client honours the environment, so localhost has to be exempted explicitly.
# Exception: Node does not read the registry proxy at all, so the DSH client is fine.
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")

BASE = "http://127.0.0.1:8000/mcp/"
if len(sys.argv) > 1:
    BASE = sys.argv[1].rstrip("/") + "/"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(name)


async def main() -> int:
    from mcp.client import Client

    print(f"connecting to {BASE}")
    # `Client(<url>)` drives the streamable-HTTP transport itself.
    async with Client(BASE, read_timeout_seconds=60) as client:
        listed = await client.list_tools()

    names = sorted(tool.name for tool in listed.tools)
    print(f"\ntools ({len(names)}):")
    for tool in sorted(listed.tools, key=lambda item: item.name):
        description = (tool.description or "").replace("\n", " ")
        billed = "\u8ba1\u8d39" in description
        print(f"  {'[计费]' if billed else '[免费]'} {tool.name}: {description[:60]}")

    # A range, not an exact number: the point is that the server exposes a plausible
    # surface, and this check should not fail merely because a tool was added. The
    # free/billed split is asserted separately and precisely.
    check("a meaningful number of tools is exposed", 8 <= len(names) <= 30, f"{len(names)} tools")
    for expected in ("get_stats", "list_hot", "get_rewrite", "list_tasks", "get_schedule"):
        check(f"{expected} is exposed", expected in names)
    billed_names = [n for n in names if n.startswith("run_")]
    check("billed tools are present", bool(billed_names), ", ".join(billed_names))

    # --- a real call through the socket ---------------------------------------
    async with Client(BASE, read_timeout_seconds=60) as client:
        result = await client.call_tool("get_stats", {"today": False})
    check("get_stats returned without error", result.is_error is False)
    payload = json.loads(result.content[0].text)
    check(
        "stats carry the expected counters",
        {"total_items", "analyses", "rewrites", "needs_review"} <= set(payload),
        json.dumps(
            {key: payload[key] for key in ("total_items", "analyses", "rewrites", "needs_review")},
            ensure_ascii=False,
        ),
    )

    async with Client(BASE, read_timeout_seconds=60) as client:
        result = await client.call_tool("list_hot", {"limit": 2})
    payload = json.loads(result.content[0].text)
    check("list_hot returns rows", payload["returned"] >= 1, f"total={payload['total']}")
    if payload["items"]:
        first = payload["items"][0]
        check("titles survive the round trip", "\ufffd" not in first["title"], first["title"])
        check("raw payloads stay out of context", "raw_data" not in first)

    # --- Host validation is a security property, so verify it live ------------
    import httpx

    async with httpx.AsyncClient(trust_env=False, timeout=30) as raw:
        response = await raw.post(
            BASE,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "host-check", "version": "1"},
                },
            },
            headers={"Host": "evil.example.com", "Accept": "application/json, text/event-stream"},
        )
    check(
        "a foreign Host header is rejected",
        response.status_code == 421,
        f"HTTP {response.status_code}",
    )

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed (no billed tool was called)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

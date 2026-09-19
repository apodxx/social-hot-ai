"""Prove whether the local MCP endpoint is being intercepted by the system proxy.

The registry proxy (ProxyEnable=1 / ProxyServer=127.0.0.1:7890) makes httpx send
localhost requests through a proxy that cannot serve them. The observable signature is
a failure with nothing at all in the server log — the request never arrives.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "proxy-check", "version": "1"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream"}

for trust_env in (True, False):
    try:
        with httpx.Client(trust_env=trust_env, timeout=15) as client:
            response = client.post("http://127.0.0.1:8000/mcp/", json=BODY, headers=HEADERS)
        print(f"trust_env={trust_env!s:<5} -> HTTP {response.status_code}, {len(response.content)} bytes")
    except Exception as exc:  # noqa: BLE001 - the failure mode is the finding
        print(f"trust_env={trust_env!s:<5} -> {type(exc).__name__}: {exc}")

print()
print("proxies httpx picks up from the environment/registry:")
with httpx.Client() as client:
    print("  ", client._mounts and "see below" or "n/a")
import urllib.request  # noqa: E402

print("  urllib.getproxies():", urllib.request.getproxies())

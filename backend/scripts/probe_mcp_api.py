"""Probe the two mounting details that decide whether the endpoint works at all."""

from __future__ import annotations

import inspect
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mcp.server.mcpserver import MCPServer  # noqa: E402

server = MCPServer("probe")


@server.tool()
def ping(value: str = "hi") -> str:
    """A trivial tool, only to force the registry to build."""
    return value


app = server.streamable_http_app(streamable_http_path="/")
print("sub-app:", type(app).__name__)
print("routes:", [getattr(route, "path", None) for route in app.routes])
print("lifespan_context on router:", hasattr(app.router, "lifespan_context"))
print("lifespan_context signature:", inspect.signature(app.router.lifespan_context))
print("session_manager now available:", type(server.session_manager).__name__)

# What the session manager needs to be started.
manager = server.session_manager
print("session manager class:", type(manager).__name__)
for attribute in ("run", "start", "shutdown"):
    member = getattr(manager, attribute, None)
    print(f"  {attribute}: {'yes' if callable(member) else 'no'}")

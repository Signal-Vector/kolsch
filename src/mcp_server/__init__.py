"""Kolsch MCP operator server — Surface B walking skeleton.

Exposes a small, read-only slice of the platform runtime over an MCP
stdio server (`kolsch mcp serve`) so an external agent (Claude Code in
headless mode via `claude -p --mcp-config`) can orchestrate and observe
kolsch without booting a workflow. Distinct from
[`src/tools/mcp.py`][tools.mcp], which is the *client*-direction
`MCPServer` Protocol kolsch consumes when it dispatches to a remote MCP
tool from inside an exchange; this package is the *server* side.

Phase 1 exposes exactly one tool, `list_sessions`, which mirrors the
existing `GET /sessions` FastAPI surface. Write/execute tools and the
authz gate are Phase 2 per `process/spec-105-mcp-operator-server.md`.

Package naming note: the spec called for `src/mcp/`, but the MCP
Python SDK also ships a top-level `mcp` package (imported via
`mcp.server.fastmcp.FastMCP`), and Python's import resolver would
shadow one with the other (site-packages precedes `src/` on sys.path).
Renamed to `src/mcp_server/` to avoid the collision; the user-facing
`kolsch mcp serve` subcommand name is unchanged.
"""

from __future__ import annotations

from mcp_server.server import build_server, run_stdio
from mcp_server.views import session_view

__all__ = ["build_server", "run_stdio", "session_view"]

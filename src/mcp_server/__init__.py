"""Kolsch MCP operator server — Surface B walking skeleton.

Exposes a small, read-only slice of the platform runtime over an MCP
stdio server (`kolsch mcp serve`) so an external agent (Claude Code in
headless mode via `claude -p --mcp-config`) can orchestrate and observe
kolsch without booting a workflow. Distinct from
[`src/tools/mcp.py`][tools.mcp], which is the *client*-direction
`MCPServer` Protocol kolsch consumes when it dispatches to a remote MCP
tool from inside an exchange; this package is the *server* side.

Two read-only tools ship today: ``list_sessions`` (spec-105) and
``read_session_events`` (spec-111). Both mirror existing FastAPI
surfaces (``GET /sessions`` and ``GET /sessions/{id}/events``
respectively). Write/execute tools and the authz gate are Phase 2 per
``process/spec-105-mcp-operator-server.md``.

Session-view helpers moved to :mod:`api.views` in spec-111 (single
source of truth for both the FastAPI and MCP surfaces); import
``session_view`` from there rather than through this package.

Package naming note: the spec called for `src/mcp/`, but the MCP
Python SDK also ships a top-level `mcp` package (imported via
`mcp.server.fastmcp.FastMCP`). Two concrete risks with a `src/mcp/`
name: (a) intra-package imports — inside `src/mcp/server.py`, an
`import mcp.views` intended to reach a sibling module would in fact
resolve to the SDK (whose `views` submodule doesn't exist), silently
breaking; (b) readability — two `mcp`s in the same repo, one an SDK
we import from and one a local package, is confusing to anyone
skimming the diff. Note that Python's import machinery would NOT
silently shadow the SDK the way a naive reading suggests
(`site-packages/` precedes `src/` on sys.path), but the two risks
above are enough. Renamed to `src/mcp_server/` to sidestep both; the
user-facing `kolsch mcp serve` subcommand name is unchanged.
"""

from __future__ import annotations

from mcp_server.server import build_server, run_stdio

__all__ = ["build_server", "run_stdio"]

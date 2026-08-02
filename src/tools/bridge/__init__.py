"""MCP tool bridge — external agents call kolsch tools over MCP (spec-120).

Two halves, one trust boundary:

- [`ToolBridgeServer`][tools.bridge.server.ToolBridgeServer] lives in
  the running kolsch process, bound to one exchange's context
  (`agent_id`/`session_id`/`thread_id` + the live `ToolsSubsystem` +
  an emit sink). It serves `bind`/`list_tools`/`call_tool` over a
  private Unix socket and routes every `call_tool` through T1's
  [`invoke_observed`][tools.observed.invoke_observed], so authz,
  sandbox, and event emission are identical to an in-engine call.
- [`run_bridge_stdio`][tools.bridge.proxy.run_bridge_stdio] backs the
  `kolsch mcp bridge` subcommand: a stateless stdio MCP server the
  external harness spawns, forwarding JSON-RPC requests to the socket.

[`make_bridge_emit_sink`][tools.bridge.sink.make_bridge_emit_sink]
builds the bridge's queue-less, trace-aware event sink — same store,
same per-thread lock, same bus as the engine's `_emit`, minus the
live-UI queue (a headless MCP caller has no chat surface).

Denials are SANITIZED: the external wire only ever sees constant
generic literals — never `recovery_hint`, `Error` detail, matched-rule
refs, or the who-chain.
"""

from tools.bridge.proxy import run_bridge_stdio
from tools.bridge.server import (
    MAX_FRAME_BYTES,
    ToolBridgeServer,
    build_wire_catalog,
    mcp_wire_name,
)
from tools.bridge.sink import make_bridge_emit_sink

__all__ = [
    "MAX_FRAME_BYTES",
    "ToolBridgeServer",
    "build_wire_catalog",
    "make_bridge_emit_sink",
    "mcp_wire_name",
    "run_bridge_stdio",
]

"""MCP server lifecycle protocol.

The [`MCPServer`][tools.mcp.MCPServer] Protocol is what the Tools
subsystem expects; v1 supplies it via injection so tests can
substitute an in-memory stub. Real MCP wire-protocol (subprocess plus
JSON-RPC over stdio) is deferred — when it lands, it will be a
concrete class implementing this Protocol with no changes upstream.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MCPServer(Protocol):
    """The contract every MCP server implementation honours.

    Methods are async — the platform is async-first throughout. The
    protocol is intentionally narrow: start/stop lifecycle,
    `list_tools` for introspection, `invoke` for dispatch.
    """

    name: str

    async def start(self) -> None:
        """Start the underlying server (e.g., spawn a subprocess).

        Idempotent — the subsystem guards against double-start at the
        registration layer, but a hot-reload variant might call this
        repeatedly; implementations stay safe.
        """

    async def stop(self) -> None:
        """Tear the server down. Idempotent."""

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return tool metadata for registration with the framework.

        Each entry shape mirrors what the MCP protocol returns:
        `{"name": str, "description": str, ...}`. The Tools subsystem
        maps these to the authz action string
        `mcp.<server>.<tool>`.
        """

    async def invoke(self, tool_name: str, args: dict[str, Any]) -> Any:  # noqa: ANN401
        """Forward a tool call to the server and return its result.

        `args` is already validated by the dispatch flow. v1 passes
        the dict through; mirroring MCP server schemas as Pydantic
        models at registration time is a follow-up.
        """

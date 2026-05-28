"""Tools subsystem errors — spec §4.5 (incl. MCP)."""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class ToolUnauthorized(PlatformError):
    """Authorization denied a tool call (Diagnostic — exchange continues)."""

    code = "tool.unauthorized"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        tool_name: str,
        action: str,
        principal: str,
        matched_rule: dict[str, Any] | None = None,
        *,
        where: str = "tools.dispatch.invoke",
    ) -> None:
        super().__init__(
            message=f"Authorization denied for tool {tool_name!r}",
            where=where,
            detail={
                "tool_name": tool_name,
                "action": action,
                "principal": principal,
                "matched_rule": matched_rule,
            },
        )


class ToolNotFound(PlatformError):
    """A declared or referenced tool does not exist in the agent's catalog."""

    code = "tool.not_found"
    level = Level.FATAL

    def __init__(
        self,
        tool_name: str,
        agent_id: str,
        *,
        where: str = "tools.setup.register",
    ) -> None:
        super().__init__(
            message=(f"Tool {tool_name!r} not found for agent {agent_id!r}"),
            where=where,
            detail={"tool_name": tool_name, "agent_id": agent_id},
        )


class ToolSetupFailed(PlatformError):
    """A tool failed to register (e.g., decorator parse, missing secret)."""

    code = "tool.setup_failed"
    level = Level.FATAL

    def __init__(
        self,
        tool_name: str,
        reason: str,
        *,
        where: str = "tools.setup.register",
    ) -> None:
        super().__init__(
            message=f"Tool {tool_name!r} setup failed: {reason}",
            where=where,
            detail={"tool_name": tool_name, "reason": reason},
        )


class ToolInvalidArguments(PlatformError):
    """Arguments failed the tool's Pydantic args_schema (TC-TLS-02)."""

    code = "tool.invalid_arguments"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        tool_name: str,
        validation_errors: list[dict[str, Any]],
        *,
        where: str = "tools.dispatch.invoke",
    ) -> None:
        super().__init__(
            message=(
                f"Invalid arguments for tool {tool_name!r} "
                f"({len(validation_errors)} error(s))"
            ),
            where=where,
            detail={
                "tool_name": tool_name,
                "validation_errors": list(validation_errors),
            },
        )


class ToolExecutionFailed(PlatformError):
    """The tool function raised during execution (TC-TLS-04)."""

    code = "tool.execution_failed"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        tool_name: str,
        error_class: str,
        *,
        where: str = "tools.dispatch.execute",
    ) -> None:
        super().__init__(
            message=(f"Tool {tool_name!r} raised {error_class}"),
            where=where,
            detail={"tool_name": tool_name, "error_class": error_class},
        )


class ToolTimeout(PlatformError):
    """The tool exceeded its execution budget."""

    code = "tool.timeout"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        tool_name: str,
        timeout_seconds: float,
        *,
        where: str = "tools.dispatch.execute",
    ) -> None:
        super().__init__(
            message=(f"Tool {tool_name!r} timed out after {timeout_seconds}s"),
            where=where,
            detail={
                "tool_name": tool_name,
                "timeout_seconds": timeout_seconds,
            },
        )


class McpServerUnreachable(PlatformError):
    """An MCP server could not be reached (spawn/connect failure)."""

    code = "mcp.server_unreachable"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        server_name: str,
        transport_error: str,
        *,
        where: str = "tools.mcp.spawn",
    ) -> None:
        super().__init__(
            message=(f"MCP server {server_name!r} unreachable: {transport_error}"),
            where=where,
            detail={
                "server_name": server_name,
                "transport_error": transport_error,
            },
        )


class McpUpstreamError(PlatformError):
    """An MCP server returned a tool-level failure."""

    code = "mcp.upstream_error"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        server_name: str,
        tool: str,
        response: str,
        *,
        where: str = "tools.mcp.invoke",
    ) -> None:
        super().__init__(
            message=(f"MCP server {server_name!r} tool {tool!r} returned error"),
            where=where,
            detail={
                "server_name": server_name,
                "tool": tool,
                "response": response,
            },
        )


class McpProtocolError(PlatformError):
    """An MCP server's response did not match the protocol."""

    code = "mcp.protocol_error"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        server_name: str,
        expected: str,
        actual: str,
        *,
        where: str = "tools.mcp.invoke",
    ) -> None:
        super().__init__(
            message=(
                f"MCP server {server_name!r} protocol error: "
                f"expected {expected!r}, got {actual!r}"
            ),
            where=where,
            detail={
                "server_name": server_name,
                "expected": expected,
                "actual": actual,
            },
        )

"""Tools subsystem errors, including MCP-side surfaces.

Tool execution and authorization failures live at Diagnostic so the
model adapts and the exchange continues; setup-time and dispatcher
binding failures are Fatal. See the
[error model](../explanation/error-model.md) for the
classification contract.
"""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class ToolPermissionsNoContext(PlatformError):
    """Raised when a tool calls `tools.permissions` outside the dispatcher binding.

    Indicates a developer mistake — typically a unit test that
    imported the tool function directly and exercised it without
    going through the dispatcher. Fatal so the test harness gets a
    clear pointer to set up an authz fixture instead of silently
    treating every check as deny.
    """

    code = "tool.permissions_no_context"
    level = Level.FATAL

    def __init__(
        self,
        tool: str,
        *,
        caller: str = "<unknown>",
        where: str = "tools.permissions",
    ) -> None:
        super().__init__(
            message=(
                f"tools.permissions called outside the engine's "
                f"dispatcher binding (tool={tool!r}); set up an "
                f"authz fixture or dispatch through the engine"
            ),
            where=where,
            detail={"tool": tool, "caller": caller},
        )


class ToolUnauthorized(PlatformError):
    """Raised when authorization denied a tool call.

    Diagnostic — the model sees the denial in its tool-result payload
    and the exchange continues.
    """

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
    """Raised when a declared or referenced tool isn't in the agent's catalog."""

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


class ConfigUnknownTool(PlatformError):
    """Raised when an agent's declared tools list a ref the asset catalog can't resolve.

    Surfaces at engine pre-flight when the asset-management lookup
    for a declared ref returns
    [`AssetsNotFound`][errors.assets.AssetsNotFound]; the engine
    catches it and re-raises as `ConfigUnknownTool` so the user
    sees a clear "agent config names a tool you don't have" rather
    than a generic "asset not found". Lives in this module rather
    than `errors.kernel` because the failure is the Tools
    subsystem's view of the catalog; the kernel itself doesn't
    enumerate tools.
    """

    code = "config.unknown_tool"
    level = Level.FATAL

    def __init__(
        self,
        ref: str,
        available_refs: list[str],
        *,
        agent_id: str | None = None,
        where: str = "engine.preflight.register_tools",
    ) -> None:
        super().__init__(
            message=(
                f"Agent config references tool {ref!r} which is not in "
                f"the catalog. Available tool refs: {sorted(available_refs)}"
            ),
            where=where,
            detail={
                "ref": ref,
                "available_refs": sorted(available_refs),
                "agent_id": agent_id,
            },
        )


class ToolSetupFailed(PlatformError):
    """Raised when a tool failed to register.

    Covers decorator parse failures, missing secrets the tool
    declares, and any other condition that prevents the Tools
    subsystem from registering the tool definition.
    """

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
    """Raised when arguments failed the tool's Pydantic `args_schema`."""

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
    """Raised when the tool function raised during execution."""

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
    """Raised when the tool exceeded its execution budget."""

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
    """Raised when an MCP server could not be reached.

    Covers both spawn failures (the server process didn't start) and
    connect failures (the transport couldn't attach to a running
    server).
    """

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
    """Raised when an MCP server returned a tool-level failure."""

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
    """Raised when an MCP server's response did not match the protocol."""

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

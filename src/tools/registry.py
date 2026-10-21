"""In-memory tool registry keyed by agent — Tools subsystem internal.

The Tools subsystem holds one of these per process. Per-agent
registration lives here; lookup at invoke time goes through
[`ToolRegistry.get_tool`][tools.registry.ToolRegistry.get_tool].

MCP server bindings are also tracked here so the dispatch flow can
forward `mcp.<server>.<tool>` invocations to the registered server
without an extra indirection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from providers.authz import Rule
    from schemas import AgentId
    from tools.decorator import ToolDefinition, UnavailableTool
    from tools.mcp import MCPServer


@dataclass
class _MCPRegistration:
    """One MCP server's per-agent registration record."""

    server: MCPServer
    lifecycle: Literal["lazy", "eager"]
    started: bool = False
    # The server's published tool list. Populated at registration
    # time for eager servers; for lazy servers populated either at
    # first-use start or from a config-supplied placeholder.
    tools: list[str] = field(default_factory=list)


@dataclass
class _AgentRegistration:
    """All tools and MCP servers registered for one agent.

    `agent_permissions` and `bundle_permissions` are the pre-resolved
    rule layers the dispatcher hands to `authz.allow` so the provider
    walks agent → bundle → platform → no-rule-callback → default-deny.
    Both stay empty for callers that don't supply them (tests and
    programmatic registration); the provider's standard two-layer
    behaviour is preserved in that case.
    """

    tools: dict[str, ToolDefinition] = field(default_factory=dict)
    # Keyed by MCP server name (e.g. "github"); the authz action
    # string is `mcp.<name>.<tool>`.
    mcp_servers: dict[str, _MCPRegistration] = field(default_factory=dict)
    agent_permissions: list[Rule] = field(default_factory=list)
    bundle_permissions: list[Rule] = field(default_factory=list)
    # feat-139: `hide_if_secrets_missing` tools left out by the most
    # recent `register_for_agent` call, keyed by tool name. Rebuilt on
    # every registration (in-memory; the durable record is the
    # `tool.unavailable` event the engine writes).
    unavailable: dict[str, UnavailableTool] = field(default_factory=dict)


class ToolRegistry:
    """Per-agent in-memory registry of registered tools and MCP servers."""

    def __init__(self) -> None:
        self._agents: dict[AgentId, _AgentRegistration] = {}

    def ensure_agent(self, agent_id: AgentId) -> _AgentRegistration:
        """Return the agent's registration record, creating it if absent."""
        if agent_id not in self._agents:
            self._agents[agent_id] = _AgentRegistration()
        return self._agents[agent_id]

    def get_tool(
        self,
        agent_id: AgentId,
        tool_name: str,
    ) -> ToolDefinition | None:
        """Return the tool registered for `agent_id` under `tool_name`, or `None`."""
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        return agent.tools.get(tool_name)

    def lookup_mcp(
        self,
        agent_id: AgentId,
        invocation_name: str,
    ) -> tuple[_MCPRegistration, str] | None:
        """Split `mcp.<server>.<tool>` into its registration and tool name.

        Returns `None` if `invocation_name` isn't an MCP call (no
        `mcp.` prefix) or if no MCP server is registered for the
        agent under the matched server name.
        """
        if not invocation_name.startswith("mcp."):
            return None
        # mcp.<server>.<tool> — server name is one segment; tool name
        # may contain dots (rare but allowed because it's the MCP
        # server's own naming).
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        for name, reg in agent.mcp_servers.items():
            prefix = f"mcp.{name}."
            if invocation_name.startswith(prefix):
                tool_name = invocation_name[len(prefix) :]
                return (reg, tool_name)
        return None

    def list_tools(self, agent_id: AgentId) -> list[ToolDefinition]:
        """Return every tool registered for `agent_id`."""
        agent = self._agents.get(agent_id)
        if agent is None:
            return []
        return list(agent.tools.values())

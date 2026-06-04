"""In-memory tool registry keyed by agent — Tools subsystem internal.

The Tools subsystem holds one of these per process. Per-agent
registration (spec §3) lives here; lookup at invoke time goes through
:meth:`get`.

MCP server bindings are also tracked here so the dispatch flow can
forward ``mcp.<server>.<tool>`` invocations to the registered server
without an extra indirection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from providers.authz import Rule
    from schemas import AgentId
    from tools.decorator import ToolDefinition
    from tools.mcp import MCPServer


@dataclass
class _MCPRegistration:
    """One MCP server's per-agent registration record."""

    server: MCPServer
    lifecycle: Literal["lazy", "eager"]
    started: bool = False
    # Per spec §5.2 — the server's published tool list. Populated at
    # registration time for eager servers; for lazy servers populated
    # either at first-use start OR from a config-supplied placeholder.
    tools: list[str] = field(default_factory=list)


@dataclass
class _AgentRegistration:
    """All tools + MCP servers registered for one agent.

    ``agent_permissions`` and ``bundle_permissions`` (qol-tool-
    permission-discovery-uniform-with-stacking.md Step 2) are the
    pre-resolved rule layers the dispatcher hands to
    ``authz.allow`` so the provider walks
    agent → bundle → platform → no-rule-callback → default-deny.
    Both stay empty for callers that don't supply them (tests +
    Phase 1 programmatic registration); the provider's existing
    Phase 2.0 behaviour is preserved.
    """

    tools: dict[str, ToolDefinition] = field(default_factory=dict)
    # Keyed by MCP server name (e.g. "github"); the action namespace
    # is `mcp.<name>.<tool>` per spec §5.4.
    mcp_servers: dict[str, _MCPRegistration] = field(default_factory=dict)
    agent_permissions: list[Rule] = field(default_factory=list)
    bundle_permissions: list[Rule] = field(default_factory=list)


class ToolRegistry:
    """Per-agent in-memory registry of registered tools + MCP servers."""

    def __init__(self) -> None:
        self._agents: dict[AgentId, _AgentRegistration] = {}

    def ensure_agent(self, agent_id: AgentId) -> _AgentRegistration:
        if agent_id not in self._agents:
            self._agents[agent_id] = _AgentRegistration()
        return self._agents[agent_id]

    def get_tool(
        self,
        agent_id: AgentId,
        tool_name: str,
    ) -> ToolDefinition | None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        return agent.tools.get(tool_name)

    def lookup_mcp(
        self,
        agent_id: AgentId,
        invocation_name: str,
    ) -> tuple[_MCPRegistration, str] | None:
        """Split ``mcp.<server>.<tool>`` and return ``(registration,
        tool_name)`` or ``None`` if the invocation isn't an MCP call.
        """
        if not invocation_name.startswith("mcp."):
            return None
        # mcp.<server>.<tool> — server name is one segment; tool name
        # may contain dots (rare but allowed by spec §5.4 since it's
        # the MCP server's own naming).
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
        agent = self._agents.get(agent_id)
        if agent is None:
            return []
        return list(agent.tools.values())

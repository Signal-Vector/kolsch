"""Tools — declared tools, MCP servers, and builtin tools.

Per Module API Reference §9 + docs/explanation/tools-subsystem.md.

Public surface:

- :class:`ToolsSubsystem` — the facade the engine consumes
  (register tools per agent, invoke a tool through the full
  validate→authz→secrets→sandbox→execute pipeline).
- ``@tool`` + :class:`ToolContext` / :class:`ToolDefinition` /
  :class:`ToolResult` / :class:`ToolInfo` — tool authoring + the
  shapes the dispatch returns.
- :class:`MCPServer` — the Protocol every MCP server backend honours.
- :func:`validate_standard_config` — the Layer 3 ConfigValidator
  the kernel dispatches for ``tools.variant=standard``.
- The four builtins via :func:`make_builtin` + the helpers
  :func:`builtin_names`.
"""

from tools.builtins import (
    builtin_names,
    make_bash,
    make_builtin,
    make_edit,
    make_read,
    make_write,
)
from tools.decorator import (
    ToolContext,
    ToolDefinition,
    ToolInfo,
    ToolResult,
    get_tool_definition,
    register_tool,
    tool,
)
from tools.discovery import discover_tools_in_file
from tools.mcp import MCPServer
from tools.subsystem import ToolsSubsystem
from tools.validator import validate_standard_config

__all__ = [
    "MCPServer",
    "ToolContext",
    "ToolDefinition",
    "ToolInfo",
    "ToolResult",
    "ToolsSubsystem",
    "builtin_names",
    "discover_tools_in_file",
    "get_tool_definition",
    "make_bash",
    "make_builtin",
    "make_edit",
    "make_read",
    "make_write",
    "register_tool",
    "tool",
    "validate_standard_config",
]

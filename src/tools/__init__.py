"""Tools — declared tools, MCP servers, and builtin tools.

Implements the Tools subsystem per the
[tools subsystem](../explanation/tools-subsystem.md) contract:
declared tools, MCP server lifecycle, and the four file-touching
builtins, exposed to the engine through a single facade.
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
    UnavailableTool,
    get_tool_definition,
    register_tool,
    tool,
)
from tools.discovery import discover_tools_in_file
from tools.mcp import MCPServer
from tools.observed import Denied, Failed, Ok, ToolOutcome, invoke_observed
from tools.subsystem import ToolsSubsystem
from tools.validator import validate_standard_config

__all__ = [
    "Denied",
    "Failed",
    "MCPServer",
    "Ok",
    "ToolContext",
    "ToolDefinition",
    "ToolInfo",
    "ToolOutcome",
    "ToolResult",
    "ToolsSubsystem",
    "UnavailableTool",
    "builtin_names",
    "discover_tools_in_file",
    "get_tool_definition",
    "invoke_observed",
    "make_bash",
    "make_builtin",
    "make_edit",
    "make_read",
    "make_write",
    "register_tool",
    "tool",
    "validate_standard_config",
]

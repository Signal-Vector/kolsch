"""Template-variable objects bound into the Jinja render context.

Each module ships one of the six standardised top-level variables
the engine binds at agent-load: `templating.vars.tools` (`ToolsVar`,
`ToolItem`), `templating.vars.docs` (`DocsVar`, `DocItem`),
`templating.vars.workspace` (`WorkspaceVar`), `templating.vars.agent`
(`AgentVar`), `templating.vars._platform` (`PlatformVar`), and
`templating.vars.skills` (`SkillsVar`, `SkillItem`).

Each variable object is a pure presentation projection — the engine
constructs them from the resolved agent config and active workspace
state at agent-load time, and the render harness
(`templating.render`) consumes them through ordinary Jinja attribute
and method access.
"""

from templating.vars._platform import (
    AliasesVar,
    AuthorizationVar,
    FileAccessVar,
    PlatformVar,
    ProvidersVar,
)
from templating.vars.agent import AgentVar, ModelView
from templating.vars.docs import DocItem, DocsVar
from templating.vars.skills import SkillItem, SkillsVar
from templating.vars.tools import ToolItem, ToolsVar
from templating.vars.workspace import WorkspaceVar

__all__ = [
    "AgentVar",
    "AliasesVar",
    "AuthorizationVar",
    "DocItem",
    "DocsVar",
    "FileAccessVar",
    "ModelView",
    "PlatformVar",
    "ProvidersVar",
    "SkillItem",
    "SkillsVar",
    "ToolItem",
    "ToolsVar",
    "WorkspaceVar",
]

"""Phase 2.2 template-variable objects.

Each module ships one of the five standardized top-level variables
the engine binds at pre-flight step 5.6.6 (per
``process/phase2.2-agent-envelope-specs.md`` §4):

- :mod:`templating.vars.tools` — :class:`ToolsVar` + :class:`ToolItem`.
- :mod:`templating.vars.docs` — :class:`DocsVar` + :class:`DocItem` (Step 4).
- :mod:`templating.vars.workspace` — :class:`WorkspaceVar` (Step 5).
- :mod:`templating.vars.agent` — :class:`AgentVar` (Step 6).
- :mod:`templating.vars._platform` — :class:`PlatformVar` (Step 7).

Each variable object is a pure presentation projection — the engine
constructs them from the resolved agent config + active workspace
state at pre-flight time, and the render harness
(:mod:`templating.render`) consumes them through ordinary Jinja
attribute / method access.
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
    "ToolItem",
    "ToolsVar",
    "WorkspaceVar",
]

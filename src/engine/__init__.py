"""The Agent Engine — long-lived orchestrator wrapping a framework.

Implements the engine subsystem per the
[agent engine contract](../explanation/agent-engine-contract.md).
Ships two frameworks:

- [`MinimalFramework`][engine.fake_framework.MinimalFramework] — the
  `bootstrap_runtime` default; a minimal message-and-tool-dispatch
  loop. Formerly named `FakeFramework`; the alias is preserved for
  backward compat.
- [`CrewAIFramework`][engine.crewai_framework.CrewAIFramework] — the
  production adapter that wraps CrewAI's Agent + Task machinery and
  adds cancellation, retry translation, and stdout capture.

See `process/feat-clarify-framework-selection.md` for the rename
rationale.
"""

from engine.assembly import ASSEMBLY_VERSION, AssembledContext, assemble_context
from engine.context import (
    AgentExecutionError,
    CancelToken,
    EngineCapabilities,
    ExecutionContext,
)
from engine.engine import ENGINE_VERSION, AgentEngine
from engine.fake_framework import FakeFramework, MinimalFramework

# CrewAIFramework imports CrewAI; only available when the test
# group is installed. Guard the import so engine consumers without
# crewai (runtime-only installs) can still use AgentEngine +
# MinimalFramework without ImportError.
try:
    from engine.crewai_framework import CrewAIFramework
except ImportError:  # pragma: no cover — runtime path without crewai
    CrewAIFramework = None  # type: ignore[assignment, misc]
from engine.framework import (
    Framework,
    LLMDriver,
    LLMHandle,
    LLMRequest,
    LLMResponse,
    Message,
    ToolCall,
    ToolHandle,
)

__all__ = [
    "ASSEMBLY_VERSION",
    "ENGINE_VERSION",
    "AgentEngine",
    "AgentExecutionError",
    "AssembledContext",
    "CancelToken",
    "CrewAIFramework",
    "EngineCapabilities",
    "ExecutionContext",
    "FakeFramework",
    "Framework",
    "LLMDriver",
    "LLMHandle",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "MinimalFramework",
    "ToolCall",
    "ToolHandle",
    "assemble_context",
]

"""The Agent Engine — long-lived orchestrator wrapping a framework.

Implements the engine subsystem per the
[agent engine contract](../explanation/agent-engine-contract.md).
Ships two frameworks:

- [`MinimalFramework`][engine.mock_framework.MinimalFramework] — the
  `bootstrap_runtime` default; a minimal message-and-tool-dispatch
  loop. Previously named `FakeFramework`.
- [`CrewAIFramework`][engine.crewai_framework.CrewAIFramework] — the
  production adapter that wraps CrewAI's Agent + Task machinery and
  adds cancellation, retry translation, and stdout capture.

See `process/feat-clarify-framework-selection.md` for the rename
rationale, and `process/qol-046-fake-to-mock-rename.md` for the
subsequent module-level `fake_*` → `mock_*` sweep.
"""

from engine.assembly import ASSEMBLY_VERSION, AssembledContext, assemble_context
from engine.context import (
    AgentExecutionError,
    CancelToken,
    EngineCapabilities,
    ExecutionContext,
)
from engine.engine import ENGINE_VERSION, AgentEngine
from engine.event_observer import EventObserver
from engine.mock_framework import MinimalFramework

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
    LLMCallable,
    LLMDriver,
    LLMFactory,
    LLMHandle,
    LLMRequest,
    LLMResponse,
    Message,
    ToolCall,
    ToolHandle,
)
from engine.subprocess_framework import ClaudeCodeHarness, SubprocessAgentFramework

__all__ = [
    "ASSEMBLY_VERSION",
    "ENGINE_VERSION",
    "AgentEngine",
    "AgentExecutionError",
    "AssembledContext",
    "CancelToken",
    "ClaudeCodeHarness",
    "CrewAIFramework",
    "EngineCapabilities",
    "EventObserver",
    "ExecutionContext",
    "Framework",
    "LLMCallable",
    "LLMDriver",
    "LLMFactory",
    "LLMHandle",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "MinimalFramework",
    "SubprocessAgentFramework",
    "ToolCall",
    "ToolHandle",
    "assemble_context",
]

"""Engine — Agent Engine wrapping the Framework Protocol.

Per Module API Reference §11 + agent-engine-contract +
docs/explanation/framework-protocol-design.md.

The engine is framework-agnostic. v1 ships:
- :class:`FakeFramework` — the default for tests and zero-LLM-cost
  development.
- (Step 12b: ``CrewAIFramework`` adapter — not in this commit.)

Public surface:

- :class:`AgentEngine` — the facade per §11.
- :class:`Framework` / :class:`LLMHandle` / :class:`ToolHandle` —
  the Protocols.
- :class:`LLMRequest` / :class:`LLMResponse` / :class:`ToolCall` /
  :class:`Message` — the value types crossing the seams.
- :class:`FakeFramework` — reference implementation.
- :class:`ExecutionContext` / :class:`CancelToken` /
  :class:`EngineCapabilities` / :class:`AgentExecutionError` — the
  caller-facing types per §11.2 + §13.5.
- :class:`AssembledContext` / :func:`assemble_context` /
  :data:`ASSEMBLY_VERSION` — Schemas §B.5 surface for engine internals
  and Materialization tests.
"""

from engine.assembly import ASSEMBLY_VERSION, AssembledContext, assemble_context
from engine.context import (
    AgentExecutionError,
    CancelToken,
    EngineCapabilities,
    ExecutionContext,
)
from engine.engine import ENGINE_VERSION, AgentEngine
from engine.fake_framework import FakeFramework, FakeFrameworkLoopExceededError

# CrewAIFramework imports CrewAI; only available when the test
# group is installed. Guard the import so engine consumers without
# crewai (runtime-only installs) can still use AgentEngine +
# FakeFramework without ImportError.
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
    "FakeFrameworkLoopExceededError",
    "Framework",
    "LLMDriver",
    "LLMHandle",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "ToolCall",
    "ToolHandle",
    "assemble_context",
]

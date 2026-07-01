"""The Agent Engine — long-lived orchestrator wrapping a framework.

Implements the engine subsystem per the
[agent engine contract](../explanation/agent-engine-contract.md).
v1 ships [`FakeFramework`][engine.fake_framework.FakeFramework] as the
default for tests and zero-LLM-cost development, plus
[`CrewAIFramework`][engine.crewai_framework.CrewAIFramework] as the
production adapter.
"""

from engine.assembly import ASSEMBLY_VERSION, AssembledContext, assemble_context
from engine.context import (
    AgentExecutionError,
    CancelToken,
    EngineCapabilities,
    ExecutionContext,
)
from engine.engine import ENGINE_VERSION, AgentEngine
from engine.fake_framework import FakeFramework

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

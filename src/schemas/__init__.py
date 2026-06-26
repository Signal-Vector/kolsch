"""Cross-module data models — Schemas §B (events), §C (entities),
plus Module API Reference §13 common types.

Re-exports every public model so callers can write::

    from schemas import Event, EventType, Session, Error, AgentId

Placement rationale: the Module API Reference §13 explicitly groups
these as "Common Types (Cross-Module)" and handbook §2.1 demands one
responsibility per module. A dedicated cross-cutting package
``schemas/`` carries the shared contract without coupling the store,
bus, engine, memory, and tools modules to each other through type
imports.
"""

from schemas.config import (
    ContextFileSpec,
    IOSpec,
    ModelSpec,
    PermissionRule,
    PromptSpec,
    ResolvedAgentConfig,
    RuntimeSpec,
    SubsystemConfig,
)
from schemas.entities import Actor, ContentRef, FromWorkflowRun, Session, Thread
from schemas.error import Error
from schemas.events import (
    AgentMessageData,
    AgentTemplateWarningData,
    ConfigSetData,
    ContextCompactedData,
    Event,
    EventType,
    MemoryDeletedData,
    MemoryRetrievedData,
    MemoryWrittenData,
    ModelFallbackData,
    ModelRequestData,
    ModelResponseData,
    SessionOpenedData,
    ThreadExchangeCompletedData,
    ThreadExchangeFailedData,
    ThreadExchangeStartedData,
    ToolCalledData,
    ToolFailedData,
    ToolResultData,
    UserMessageData,
)
from schemas.identifiers import (
    AgentCallId,
    AgentId,
    EventId,
    MemoryItemId,
    PrefixedId,
    SessionId,
    SubscriptionId,
    ThreadId,
    derive_agent_id,
)

__all__ = [
    "Actor",
    "AgentCallId",
    "AgentId",
    "AgentMessageData",
    "AgentTemplateWarningData",
    "ConfigSetData",
    "ContentRef",
    "ContextCompactedData",
    "ContextFileSpec",
    "Error",
    "Event",
    "EventId",
    "EventType",
    "FromWorkflowRun",
    "IOSpec",
    "MemoryDeletedData",
    "MemoryItemId",
    "MemoryRetrievedData",
    "MemoryWrittenData",
    "ModelFallbackData",
    "ModelRequestData",
    "ModelResponseData",
    "ModelSpec",
    "PermissionRule",
    "PrefixedId",
    "PromptSpec",
    "ResolvedAgentConfig",
    "RuntimeSpec",
    "Session",
    "SessionId",
    "SessionOpenedData",
    "SubscriptionId",
    "SubsystemConfig",
    "Thread",
    "ThreadExchangeCompletedData",
    "ThreadExchangeFailedData",
    "ThreadExchangeStartedData",
    "ThreadId",
    "ToolCalledData",
    "ToolFailedData",
    "ToolResultData",
    "UserMessageData",
    "derive_agent_id",
]

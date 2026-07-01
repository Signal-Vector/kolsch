"""The cross-module data models — shared types every subsystem uses.

Re-exports every public model so callers can write:

```python
from schemas import Event, EventType, Session, Error, AgentId
```

These types live in a dedicated `schemas/` package rather than inside
any one subsystem because they are shared contract: the store, bus,
engine, memory, and tools modules all consume them, and co-locating
them here avoids cross-subsystem type imports that would otherwise
create coupling.

For the complete cross-module type catalog and its layering rules,
see the
[module API reference](../reference/module-api-reference.md).
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
from schemas.entities import (
    Actor,
    ContentRef,
    FromWorkflowRun,
    ParentRunPointer,
    Session,
    Thread,
)
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
    "ParentRunPointer",
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

# Event log contract

The event log is Kolsch's ground truth for what happened during an
agent run. Everything else — the assembled model context, the entity
records the store maintains, the chat UI's transcript — is derived
from it. This page is the contract: the tree shape, the append
protocol, the assembly procedure, and the materialization rule that
makes replay honest.

The types on this page live in
[`src/schemas/events.py`](https://github.com/Signal-Vector/kolsch/blob/main/src/schemas/events.py).
For the mkdocstrings-rendered signatures, see
[Schemas reference](../reference/schemas.md).

## What the log is

An append-only, tree-structured record of everything the Agent
Engine did during one session. One JSONL file per session, opened on
`session.opened`, appended to for every subsequent event.

The log stores **inputs and outputs of execution, not assembled
artifacts**. The assembled model context — the thing that actually
gets sent to the LLM — is derived from the log by the Assembly
Procedure below. What lives on disk is what fed the derivation.

The log records **execution history only** — not entity state. The
store's `Session`, `Thread`, and `Actor` records track mutable
identity (name, starred, archived); the log tracks the immutable
history.

The Agent Engine is the sole emitter. No other subsystem writes
events directly.

## The event envelope

Every record on the JSONL is an `Event`:

```python
class Event(BaseModel):
    event_id: EventId          # ev_<uuid>
    parent_event_id: EventId | None
    thread_id: ThreadId        # th_<uuid>
    session_id: SessionId      # ses_<uuid>
    type: EventType
    data: dict[str, Any]       # type-specific payload
    actor: Actor               # who produced this event
    created_at: datetime
    content_ref: ContentRef | None
    trace_id: str | None       # chain-spanning trace, workflow-set
```

Notes:

- `session_id` is carried on every event even though the JSONL file
  identifies the session. In-memory consumers shouldn't need to know
  the storage layout to attribute events.
- No `seq` field. The tree via `parent_event_id` is the order.
- `content_ref` holds an optional pointer to a content-addressed
  large payload (image, big blob) when the inline `data` would blow
  past a reasonable line size.
- `trace_id` is set by the Agent Engine when a workflow-spawned
  invocation supplied one; `None` for chat/API surfaces that didn't
  opt in. Chain-spanning so multi-agent runs share it.

## The tree model

The events of a session form a tree. The file is a linearized view
of the tree in append order:

- Every event has an `event_id` and a `parent_event_id`.
- The first event of a thread has `parent_event_id` pointing at the
  thread's id (`th_...`).
- The **current leaf** of a thread is derived from the log, not
  stored. Today the log is single-path (retries are the only way a
  sibling appears); the leaf is the most recently appended event on
  the thread. When user-visible branching lands, the leaf becomes
  the most recently appended childless event.

## The append protocol

To append a new event to a thread the Agent Engine:

1. Finds the current leaf by reading the log.
2. Sets the new event's `parent_event_id` to the leaf's `event_id`.
3. Appends the event.

There is no stored leaf pointer to update — the new event's
existence makes it the new leaf for future appends. The runtime may
cache the tree and leaf in memory as an optimization; it discards
and rebuilds them on resume.

## The Assembly Procedure

The Assembly Procedure builds, on demand, the input to a model call.
It runs fresh every turn from the log; the output is not stored.

The procedure is versioned via `AgentConfig.assembly_version` and
recorded on `config.set` events. Replaying the log at a past point
uses the version in effect at that point.

1. **Walk the tree.** Starting at the thread's current leaf, walk
   `parent_event_id` pointers back to the thread root. Reverse the
   list to chronological order.
2. **Determine effective config.** The most recent `config.set` on
   the path (or, for the first call, the `config.set` at thread
   start) gives the resolved `AgentConfig` in effect — model spec,
   prompt layers, subsystem configurations, assembly version.
3. **Compose the system prompt** by rendering the configured prompt
   template with the standardized variable context.
4. **Project events to messages.** Walk the path, projecting each
   event to a model-facing message (or omitting it):

    | Event type | Projection |
    |---|---|
    | `user.message` | one user message, content verbatim |
    | `agent.message` | one assistant message, content verbatim |
    | `tool.called` | the tool-call block of an assistant message |
    | `tool.result` | one tool-result message, content verbatim |
    | `tool.failed` | one tool-result message (error variant) |
    | `memory.retrieved` | injected content per the variant's projection rule |
    | `context.compacted` | the summary replaces the events listed in `compacted_event_ids` |
    | all other types | not projected |

5. **Apply tool definitions.** From the effective config's tool
   subsystem, provide the tool list to the model in its expected
   format.
6. **Hand to the model.** The system prompt, message list, and tool
   list are the model's input.

The output of the procedure is **not stored**. What is stored: the
inputs (the events on the path), the call's metadata
(`model.request`), and the model's output (`model.response`).

**Determinism requirement:** for a given tree path and assembly
version, the procedure MUST produce a byte-identical output. No
clock reads, no random ordering, no implementation-specific drift.

### Wrapping frameworks — `model.request.data.messages`

`model.request.data` carries a `messages` field: the verbatim list
the underlying LLM received, captured post-framework-wrap and
pre-LLM-invoke.

Why: any framework that mutates messages between the engine's
hand-off and the LLM call (CrewAI's prompt-template injection,
multimodal reshaping, role normalization) makes a path-derived
context diverge from what the model actually saw. Replay reads
`messages` directly for "what did the model see at this call." The
Assembly Procedure still applies for projecting context for *future*
calls (calls that haven't happened yet and therefore haven't been
captured).

`model.request.data` also carries a `tools` field: the tool-schema
list the LLM saw for this call. Empty when the agent declares no
tools; one entry per registered tool otherwise. Capturing it keeps
the log faithful to every input that shaped the model's behaviour.

## The Materialization Rule

**Anything contributing to a model call's context must be captured
as an immutable event with the actual content at the time of use.**

The Assembly Procedure replays events; if an event references
something external whose value could change, fidelity is silently
broken on replay. So:

- **Configuration in effect** is captured on `config.set` fully
  dereferenced. No live references to mutable assets; actual model
  spec, actual prompt template body, actual tool definitions.
- **Memory retrievals** carry the actual content returned by the
  retrieval, verbatim. The Assembly Procedure uses the captured
  content; it does not re-run the retrieval at replay time.
- **Skill bodies** referenced by an agent's prompt are captured via
  the prompt template's content in `config.set`. Skills loaded
  mid-exchange are captured inline in the message that triggered the
  load.
- **File contents read into context** are captured in the
  `tool.result` of the reading tool. Files read via `@`-references
  in user messages are materialized into the `user.message` event
  itself.
- **User input and agent output** are captured verbatim on
  `user.message` and `agent.message`.

If anything contributes to context and is NOT captured as an
immutable event, that is a contract violation. The rule is the
bargain that makes on-the-fly context assembly honest: replay
reconstructs what the model saw because every input is in the log.

## The event types

The Agent Engine emits these event types today. The full enum is
`EventType` in `schemas/events.py`.

**Session identity** (the file's first event):

- `session.opened`

**Thread progress:**

- `thread.exchange_started`
- `thread.exchange_completed`
- `thread.exchange_failed`

**Conversation content** (projected to context):

- `user.message`
- `agent.message`
- `tool.called`
- `tool.result`
- `tool.failed`

**Model interaction** (record of the call, not derivable from the
tree — must be stored):

- `model.request`
- `model.response`
- `model.fallback` — one per fallback transition when the primary
  model errors recoverably and the engine walks the `fallback_models`
  chain

**Config:**

- `config.set` — the full resolved config in effect, including
  `assembly_version`

**Memory** (Materialization Rule):

- `memory.written`
- `memory.retrieved`
- `memory.deleted`

**Compaction:**

- `context.compacted`

**Agent pre-flight consistency:**

- `agent.template_warning` — the engine's pre-flight emits one when
  a prompt template references variable data the resolved config
  doesn't carry (e.g. `{{ tools }}` referenced but no tools
  declared).

## Per-type data payloads

The canonical shape of each event's `data` field. Each `*Data` model
enforces `extra="forbid"` — a typo fails validation rather than
passing through into the JSONL.

**`session.opened.data`:**

```python
session_id: SessionId
initial_agent: dict[str, str]   # {id, name}
forked_from: dict[str, Any] | None
```

**`thread.exchange_started.data`:**

```python
trigger_event_id: EventId
```

**`thread.exchange_completed.data`:**

```python
exchange_started_id: EventId
agent_message_id: EventId
```

**`thread.exchange_failed.data`:**

```python
exchange_started_id: EventId
error: Error
```

**`user.message.data`:**

```python
content: dict[str, Any]                  # typed per IOSpec.inputs
images: list[dict[str, Any]] | None
```

**`agent.message.data`:**

```python
content: dict[str, Any]                  # typed per IOSpec.outputs
exchange_id: EventId
```

**`model.request.data`:**

```python
call_id: AgentCallId
model: dict[str, Any]                    # actual {provider, model, parameters}
config_event_id: EventId
messages: list[dict[str, Any]]           # verbatim LLM-seam messages
tools: list[dict[str, Any]]              # tool schemas the LLM saw
```

**`model.response.data`:**

```python
call_id: AgentCallId
content: dict[str, Any]
reasoning_trace: dict[str, Any] | None
usage: dict[str, Any]
cost: float
latency_ms: int
stop_reason: str
error: Error | None                      # populated when stop_reason == "error"
```

**`model.fallback.data`:**

```python
from_: dict[str, str]                    # canonical {provider, model}
to: dict[str, str]
reason: str                              # failing attempt's error code
reason_detail: str
attempt: int                             # 1-indexed
```

**`tool.called.data`:**

```python
tool_name: str
tool_call_id: str
arguments: dict[str, Any]
```

**`tool.result.data`:**

```python
tool_call_id: str
tool_name: str
result: dict[str, Any]
is_error: bool
```

**`tool.failed.data`:**

```python
tool_call_id: str
tool_name: str
error: Error
recovery_hint: str | None                # e.g. an `/perm allow ...` string
```

**`config.set.data`:**

```python
resolved_config: dict[str, Any]          # full, fully-dereferenced
config_hash: str
```

**`memory.written.data`:**

```python
item_id: MemoryItemId
agent_id: AgentId
scope: str
content: dict[str, Any]
tags: list[str]
metadata: dict[str, Any]
variant: str
```

**`memory.retrieved.data`:**

```python
scope: str
query: dict[str, Any]
results: list[dict[str, Any]]            # actual items, verbatim
variant: str
```

**`memory.deleted.data`:**

```python
item_id: MemoryItemId
```

**`context.compacted.data`:**

```python
compacted_event_ids: list[EventId]
pre_compaction: dict[str, Any]           # FULL content that was summarized
summary: dict[str, Any]
tokens_before: int
tokens_after: int
```

**`agent.template_warning.data`:**

```python
reference: str
reason: str
hint: str
```

## Retries — natural branching

When the Agent Engine retries a failed call, the failure remains in
the tree (a `model.response` with an error, or a `tool.failed`) and
the retry attaches as a sibling — both children of the same parent.
The parent walk from the successful leaf naturally skips the failed
sibling; it's not on the path.

Fidelity is preserved. No special retry logic in the projection.
Tree machinery does the work.

## Reconstruction guarantee

Given a thread's events at any point and the assembly procedure
version recorded in the effective `config.set` at that point,
replaying through the Assembly Procedure reproduces
**byte-identically** what the model saw at that point. The guarantee
rests on:

- Events being immutable.
- The Materialization Rule ensuring every input is captured.
- The Assembly Procedure being deterministic at its recorded
  version.

A session is resumed by opening the JSONL file; no other lookup is
needed for session content.

## How it fits with the rest

- `AgentConfig` is resolved by the kernel and handed to the Agent
  Engine. See [Kernel](../architecture/kernel.md).
- The Agent Engine executes it, writing execution history into the
  session's JSONL log via the append protocol. See
  [Agent Engine](../architecture/engine.md).
- The model's context is built fresh every turn by the Assembly
  Procedure walking from the thread's leaf back to the root.
- Inputs to model calls are captured as events (Materialization
  Rule); outputs are captured on `model.response`. The assembled
  context itself is never stored — except at the LLM seam, on
  `model.request.messages`, when a wrapping framework mutated it.
- Entity records — `Session`, `Thread` — hold mutable UI state
  (name, starred, archived). Content history lives in the log. See
  [Store](../architecture/store.md).

The event log plus the Assembly Procedure version together are the
contract: same tree + same procedure version = same context,
byte-identically.

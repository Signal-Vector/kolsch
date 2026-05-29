# Phase 1 — The Agent Engine Contract

**Draft 0.1**

> The contract the Agent Engine must honor. v1 implementation wraps CrewAI;
> the contract is framework-agnostic and survives engine swaps.
>
> Builds on Phase 1 Schemas draft 0.4. References to §A, §B, §C are sections
> of that document.

---

## Part 0 — Decisions Locked

- **The engine is stateless across calls.** No "session is open" mode. Each
  `execute` finds the thread's leaf, runs one exchange, returns. The thread
  (its JSONL file and the derived leaf) carries continuity, not the engine.
- **Streaming is the primitive; sync is the convenience.** One execution
  path, two surface modes.
- **The engine is a recorder, not a driver.** The underlying framework
  (CrewAI in v1) does the agentic work — model calls, tool dispatch, retry
  loops. The engine prepares inputs, registers hooks, observes execution,
  emits events per the schema, returns the result.
- **Conformance is specified as observable outcomes** — events on the tree
  in the right shape — not as control flow. Any implementation that
  produces the conforming event sequence is a valid engine.
- **Tool dispatch is owned by the framework.** The engine's tool-related
  obligations are setup, observation (event emission), and sandboxing.
- **The Assembly Procedure (§B.5) is the main behavior the engine actually
  owns** — preparing the model's input from the tree. Normative, versioned,
  deterministic.

---

## Part 1 — Interface

### 1.1 The two methods

```
execute_stream(
    config:  ResolvedAgentConfig,
    inputs:  dict,                  — typed per config.io.inputs
    context: ExecutionContext,
) -> AsyncIterator[Event]


execute(
    config:  ResolvedAgentConfig,
    inputs:  dict,
    context: ExecutionContext,
) -> Event                          — the agent.message event, in full
```

`execute_stream` is the primitive. `execute` is implemented in terms of
`execute_stream`: collect all yielded events, return the final
`agent.message` event. There is exactly one execution path; sync is a
convenience layer.

### 1.2 `ResolvedAgentConfig`

The fully-resolved agent configuration per §A — system defaults, agent
definition, and any call-context layer already merged by the kernel. The
engine does NOT do config resolution; it receives a ready-to-execute config.

Includes `assembly_version` (§A.10), which selects the version of the
Assembly Procedure in force.

### 1.2.1 Engine dependencies (Phase 1.1 amendment, LLM-R4)

The engine depends on these collaborators at construction time:

- `SessionStore`, `EventBus`, `AssetManagement`, `ToolsSubsystem`,
  `SkillsSubsystem`, `MemoryProvider`, `SecretsProvider`,
  `SandboxProvider`, `AuthorizationProvider` — per the dependency
  matrix in Module API Reference §12.
- `Framework` — the chosen framework adapter (FakeFramework in tests
  and dev; CrewAIFramework in production).
- **`build_llm` helper** *(Phase 1.1, LLM-R4)* — the bridge between
  `ModelSpec` and a usable `crewai.LLM`. The engine calls this at
  pre-flight step 6 (§2.1) for each exchange. The helper itself
  consults the **connector registry** (`src/llm/registry.py`) to
  dispatch to the right `ProviderConnector` implementation. The
  registry is process-global; connectors register themselves on
  import (`src/llm/providers/__init__.py`). See Module API Reference
  Part 11.5 for the helper + Protocol surface.

### 1.3 `ExecutionContext`

```
ExecutionContext
├── thread_id    string        — "th_..." the thread this execute targets
├── trigger      object        — the cause of this call (see below)
└── cancellation CancelToken?  — optional; for cooperative cancellation
```

The `trigger` is one of several kinds:

Conversational mode (no force-invoke):
```
{ kind: "user_message", content: <typed> }
```

Conversational mode with force-invoke of one or more skills:
```
{
    kind: "user_message_with_force_skill",
    content: <typed>,
    force_skills: ["<bundle>/skills/<name>", ...]
}
```

Single-shot mode:
```
{ kind: "invocation", inputs: <typed> }
```

The engine processes `force_skills` per Skills subsystem §6.2 — emit
synthetic `tool.called`/`tool.result` for each skill's SKILL.md before
processing the user message.

The engine reads the thread's existing log to find the leaf and assemble
context; it appends new events under that leaf. `inputs` (parameter 2 of
`execute`) is redundant with `trigger.content` / `trigger.inputs` and is
the typed projection of it — both are passed for clarity and validation.

### 1.4 What `execute_stream` yields

Each yielded item is a full `Event` per §B.2, byte-identical to what is
written to the log. The stream yields events as they are emitted, in the
same order they appear in the log. Typical sequence for an exchange:

```
thread.exchange_started
config.set                         — if not already current
user.message  (or single-shot input materialized)
model.request
[ model.response ]
[ tool.called → tool.result/tool.failed ]   — possibly multiple cycles
agent.message
thread.exchange_completed
```

Some events (`memory.retrieved`, `context.compacted`) appear when their
conditions trigger. The exact ordering is constrained by §B.5 (Assembly
Procedure) and Part 3 (Conformance) below.

### 1.5 What `execute` returns

The final `agent.message` event in full, per §B.8. Equivalent to consuming
`execute_stream` and returning the last `agent.message` yielded.

### 1.6 Failure surface

**Whole-exchange failure** — all retries exhausted, an unrecoverable error,
or cancellation. The engine emits `thread.exchange_failed` and:

- `execute` raises `AgentExecutionError(failed_event=<the event>)`.
- `execute_stream` yields the `thread.exchange_failed` event and then ends
  (does not raise).

**In-exchange failures** — a tool errors, a model call times out and is
retried. These are recorded as events on the tree (`tool.failed` or
`model.response` with error) and do not raise. The exchange continues
toward eventual success or whole-exchange failure.

**Cancellation** — caller cancels via `CancelToken` (or closes the stream
iterator early). The engine completes any in-flight tool/model call, emits
`thread.exchange_failed` with a `cancelled` cause, and ends. `execute` does
NOT raise on cancellation (per Python idiom for cooperative cancellation);
the failed event's cause is what the caller reads.

### 1.7 Capability declaration

```
capabilities() -> CapabilitySet

CapabilitySet
├── assembly_versions   [string]   — versions of §B.5 this engine implements
├── supported_modes     [enum]     — { single_shot, conversational }
├── supported_features  [string]   — e.g. "tool_use", "reasoning"
└── supported_providers [string]   — model providers; or ["*"] if unlimited
```

Phase 1: lean content; one engine; the shape matters more than the values.
The loader uses `capabilities()` to refuse agent configs requiring features
this engine cannot honor, before any execute.

### 1.8 Principal Derivation

The Agent Engine threads a `principal` through authorization checks per
the Authorization Provider's namespace conventions (§3.2.4 of the
Authorization spec). Rules:

- **The top-level `execute` call's principal is `user.local`** in v1
  (single-user local-first). When real multi-user support arrives in a
  later phase, this becomes `user.<id>` of the invoking user.
- **Tool invocations within an agent's execution carry the agent's
  principal: `agent.<agent_id>`.** When the model decides to call a
  tool, the platform's tool adapter checks authorization with this
  principal, not the user's. This is correct: the agent is the
  immediate actor.
- **Synthetic platform actions carry `system`.** This includes
  force-invoke `read` calls for SKILL.md files (per §2.1 step 7),
  reconstructive operations, and any platform-initiated maintenance.

The principal is threaded explicitly through every layer that calls into
Authorization. It is not stored on the ExecutionContext; it is computed
at each check site based on what's calling into auth.

(A future variant could make principal a first-class field on
ExecutionContext if call-site computation proves error-prone. For v1,
explicit derivation at each site is simpler.)

---

## Part 2 — Behavior

The engine is a thin recorder around a framework that does the agentic
work. Its responsibilities, in order of an `execute` call:

### 2.1 Pre-flight (before the framework runs)

1. **Validate the resolved config** against §A's schema. The kernel
   normally does this; the engine double-checks (defense in depth).
2. **Validate** that `config.assembly_version` is in
   `capabilities().assembly_versions`. If not, refuse with a clear error.
3. **Authorization check.** Call
   `authz.allow(principal, f"agent.execute.{agent_id}")`. The
   principal is `user.local` for top-level invocations,
   `agent.<calling_agent_id>` if a workflow node or another agent is
   triggering this execution. If denied, raise `agent.unauthorized`
   (Fatal); emit `thread.exchange_failed`; no further work.
4. **Open the session's JSONL file** for append. Compute the thread's
   current leaf by reading the log (§B.4).
5. **Bind memory context.** Call
   `memory.bind_session(session_id, agent_id, resolved_memory_config)`.
   Hold the returned `MemoryContext` for the session's duration. The
   Memory provider replays `memory.written` / `memory.deleted` events
   from the log to rebuild its cache (per Memory contract §3.3).
6. **Acquire LLM client for the primary model.** *(Phase 1.1 — LLM-R3;
   retitled by Phase 1.3 — FB-R3.)* Call
   `build_llm(config.model, provider_config, secrets)` where
   `provider_config` is the entry from `platform.yaml`'s
   `llm_providers[config.model.provider]`. Hold the returned
   `crewai.LLM` for the exchange's lifetime; pass it to the Framework
   via the Framework Protocol. On any `llm.*` Fatal
   (`llm.provider_not_configured`, `llm.secret_missing`,
   `llm.client_init_failed`, `llm.connector_unknown`), emit
   `thread.exchange_failed` and raise per the failure surface (§1.6).
7. **Build the fallback chain.** *(Phase 1.3 — FB-R3.)* Resolve each
   entry in `config.fallback_models` (or
   `defaults.fallback_models` if the agent omitted the field) to a
   canonical `ModelSpec` via the kernel's normalisation. Store as
   an ordered list of resolved `ModelSpec`s. Do NOT pre-build their
   `crewai.LLM` clients — fallbacks may never be needed, and
   per-provider credential failures should surface at use time, not
   pre-flight time. Validate that each entry's `provider` is in
   `llm_providers` (a fallback against an unconfigured provider is
   a Fatal at this step — fail fast with a clear message naming the
   offending entry). When neither `config.fallback_models` nor
   `defaults.fallback_models` is set, the chain is empty and a
   Recoverable `model.*` error ends the exchange directly.
8. **Resolve a `config.set` event if needed.** If the most recent
   `config.set` on the thread differs from the incoming `config` (or there
   is none), emit a new `config.set` carrying the full resolved config
   (per Materialization Rule, §B.6). The new event becomes part of the
   tree path before the exchange begins.
9. **Process force-invoke skills, if any.** If
   `trigger.kind == "user_message_with_force_skill"`, for each skill ref
   in `trigger.force_skills`:
   - Verify the skill is in the agent's enabled list (per Skills §4).
     If not, raise `skill.forced_not_enabled` (Fatal).
   - Emit a synthetic `tool.called` for `read` on the SKILL.md path
     (principal `system` for these synthetic calls).
   - Read the file (subject to path policy and authorization on
     `tool.read`).
   - Emit `tool.result` carrying the SKILL.md content.
10. **Emit `thread.exchange_started`** with the trigger event id (which is
    the `user.message` for conversational mode, or a synthetic input event
    for single_shot mode — §B.7).
11. **Materialize the trigger** as an event on the tree:
    - Conversational: emit `user.message` with the trigger content verbatim.
    - Single_shot: emit `user.message` (still the term used) with the typed
      inputs verbatim — the conversational/single_shot distinction is an
      IOSpec property and does not change the event vocabulary.

### 2.2 Set up the framework

10. **Build the model's input** using the Assembly Procedure (§B.5) on the
    current tree path. This is the engine's main owned behavior. After
    running the procedure (which projects the tree path into model-facing
    messages), **append the available-skills listing** (per Skills
    subsystem §5.1) to the assembled system prompt. The skills listing is
    engine-appended, not declared in the PromptSpec.
11. **Register tools with the framework.** From `config.subsystems.tools`:
    - Builtin tools (the platform's standard library) — register with the
      framework's tool mechanism.
    - Declared tools (user/bundle-supplied) — register similarly.
    - MCP server tools — eager servers started now and their tools
      registered; lazy servers' tools registered as placeholders that
      start the server on first invocation.
    In all cases, **tool execution is routed through the Sandbox provider**
    per `config.subsystems.sandbox` (Phase 1: native).
12. **Register lifecycle hooks** with the framework so the engine can
    observe (a) when the model is called, (b) when tools are dispatched,
    (c) when results come back. The specific hooks depend on the framework;
    the obligation is that the engine sees enough to emit the events
    required by §3.
13. **Resolve secrets** the tools need via the Secrets provider. Resolved
    values are passed to the tools at invocation time, never logged.

### 2.3 Hand to the framework

14. **Invoke the framework** with: the assembled context, the registered
    tools, the model spec, and the lifecycle hooks. The framework runs the
    agentic loop — calls the model, dispatches tool calls, retries failed
    calls per its and the config's retry policy, until it reaches a final
    answer or fails terminally.

The engine is now passive: it receives callbacks from the framework's
lifecycle hooks. Each callback triggers an event emission per §3.

**Note on concurrent callbacks.** The framework (CrewAI) may invoke
lifecycle callbacks from different async contexts concurrently. The
engine's adapter MUST serialize event construction per thread to avoid
race conditions in `parent_event_id` computation.

Specifically: the Session/Run Store's `append_event` is lock-protected
(Store I2), but the engine's *callback handler* code that constructs
events from framework data is not automatically serialized. The adapter
must use an asyncio Lock (or equivalent) around event-construction-and-
append for each active thread.

This is engine-internal correctness work; it's not visible at the
contract surface, but it's a real implementation obligation.

### 2.4 Observe and record

For each lifecycle event the framework reports, the engine:

- **Model called:** emit `model.request` per §B.8 (records the call's
  metadata — model, parameters, config_event_id — but NOT the assembled
  context, which is derivable per §B.5). When the call returns a
  Recoverable `model.*` error and a fallback chain is available
  (pre-flight step 7), the engine consults the next entry — see
  the fallback loop below.
- **Model responded:** emit `model.response` with content and reasoning
  trace verbatim, usage, cost, latency.
- **Model fallback triggered:** *(Phase 1.3 — FB-R4.)* when a
  Recoverable `model.*` error from the previous attempt led to
  selecting the next chain entry, emit `model.fallback` per §B.8 with
  `{from, to, reason, reason_detail, attempt}`. The event sits
  between the failing attempt's `model.request` (or its
  `model.response` if one was produced before failure) and the next
  attempt's `model.request`. Exactly one `model.fallback` per
  transition.
- **Tool requested:** emit `tool.called` with arguments verbatim.
- **Tool succeeded:** emit `tool.result` with the result verbatim.
- **Tool failed:** emit `tool.failed` with error detail.
- **Memory retrieved:** if a memory retrieval contributes to context, emit
  `memory.retrieved` with the actual returned content verbatim (per
  Materialization Rule).
- **Compaction triggered:** if the engine determines the projected context
  exceeds policy (see §2.6), trigger compaction via the Memory provider,
  emit `context.compacted` with the full pre-compaction content and the
  summary.

**Fallback loop (Phase 1.3 — FB-R3).** When the primary model call
returns a Recoverable `model.*` error (rate limit, transient 5xx,
timeout, context overflow — see Error Model §4.15 for the trigger
table), the engine:

1. Constructs the next attempt's `crewai.LLM` from the chain
   resolved at pre-flight step 7 (lazy build via `build_llm`).
2. Emits a `model.fallback` event for the transition.
3. Re-issues the request against the new model. Same lifecycle
   handlers fire; recursion-by-iteration walks the chain.
4. On chain exhaustion, raises `llm.all_models_failed` (Fatal) with
   the full ordered list of attempts in `detail.attempts`. The
   engine then proceeds to the standard `thread.exchange_failed`
   emission path (§2.5 terminal failure branch).

Fatal `model.*` errors (auth failure, bad request, etc.) abort the
exchange without consulting the chain — the same key would fail at
the next provider too. Streaming exchanges fall back ONLY when the
error fires before the first content event; after partial output
has been committed to the log, the engine ends the exchange with
the truncation visible. See Phase 1.3 patch §2.2 + §2.3 for the
canonical trigger table and the streaming boundary.

All emissions are appended to the JSONL file in order via
`store.append_event(...)`. They are *also* yielded from `execute_stream`.

After each successful `append_event` returns, the engine calls
`bus.publish(event)` to publish the same event to the Event Bus's
ephemeral tier. If `bus.publish` raises (Redis unavailable, etc.), the
engine logs a warning and continues — the bus is best-effort, the log
is the source of truth, and a `bus.publish_failed` Diagnostic error
doesn't affect agent execution.

The two operations (append, publish) are intentionally NOT
transactionally atomic. The log is the ground-truth record; bus
subscribers that need guaranteed delivery use the strict-subscriber
pattern (per Event Bus contract §5.1) against the log, not against
the bus.

### 2.5 Finalize

When the framework reports completion:
- **Successful completion:** emit `agent.message` with the final output
  typed per `config.io.outputs`, then `thread.exchange_completed`. Return
  the `agent.message` event from `execute`, or end the stream from
  `execute_stream`.
- **Terminal failure:** emit `thread.exchange_failed` with cause. Raise
  `AgentExecutionError` from `execute`, or end the stream cleanly from
  `execute_stream`.

### 2.6 Compaction triggering

The engine MUST monitor the projected context size (computed by the
Assembly Procedure §B.5 step 4) against the effective model's context
window. When projected size exceeds a configured threshold (Phase 1
default: 80% of the model's window), the engine triggers compaction:

1. Selects the events to compact (per the configured policy — Phase 1
   default: oldest events excluding the most recent N turns).
2. Asks the Memory provider to produce a summary of the selected events'
   content.
3. Emits `context.compacted` carrying the selected event ids, the full
   pre-compaction content, and the summary.
4. Re-runs the Assembly Procedure; the new projection uses the summary in
   place of the compacted events (per §B.5's projection table).
5. Continues the exchange.

The compaction *algorithm* is the Memory provider's concern; the
*triggering* and *event emission* are the engine's. The log loses
nothing — the full pre-compaction content is in the event.

### 2.7 Retries

Retries of model calls and tool calls are the framework's responsibility,
within bounds set by `config.runtime`. Each retry attempt produces its
own observable events: a failed `model.response` (or `tool.failed`),
followed by a retry attempt that the engine observes as a sibling in the
tree (per §B.9, retries-as-natural-branching).

The engine does NOT swallow failed attempts. They remain in the log
permanently, with the eventually-successful attempt as a sibling. The
parent walk from the successful leaf skips them because they are not on
the path.

---

## Part 3 — Conformance Invariants

The contract any engine implementation must honor. Framework-agnostic.
These are the *load-bearing* invariants that make engine swap meaningful;
deviation in any of them is a contract violation, regardless of
implementation language or framework.

### I1 — Sole emission

The engine is the sole writer of the session's event log. Subsystems
return values; they do not write to the log. (Subsystems MAY keep private
diagnostic logs entirely separate from the session log.)

### I2 — Tree append

Every event MUST be appended such that its `parent_event_id` points at the
current leaf (or, for the first event of a thread, at the thread's id).
The engine MUST NOT mutate previously written events. The engine MUST NOT
reorder appended events.

### I3 — Materialization

Anything contributing to the model's context MUST be captured as an
immutable event with actual content at the time of use (§B.6 Materialization
Rule). This includes:

- The effective configuration via `config.set` (fully dereferenced).
- Memory retrievals via `memory.retrieved` with the actual returned content
  verbatim.
- File contents read into context via `tool.result` of the reading tool.
- User input and agent output via `user.message` and `agent.message`.

An implementation that lets context contribute from an external source
without an event capturing it is non-conforming. Replay must be honest.

### I4 — Assembly Procedure conformance

The engine MUST implement the Assembly Procedure (§B.5) deterministically
at its declared `assembly_version`. Given the same tree path and the same
declared version, the procedure MUST produce a byte-identical model input.
No clock reads, no random ordering, no implementation variance.

### I5 — Event schema conformance

Every emitted event MUST conform to §B.2 (envelope) and §B.7/B.8 (per-type
specifications). `data` content MUST be verbatim where the spec demands
verbatim (model context, tool arguments, tool results, memory retrievals,
agent output, user input). No summarization, truncation, or normalization
in event content unless an explicit event type (`context.compacted`) is
emitted to record the lossy transformation.

### I6 — Event order

Within an exchange, events MUST be emitted in the order observable to a
consumer of `execute_stream` and consistent with their tree positions.
Specifically:
- `thread.exchange_started` precedes all other events in the exchange.
- `agent.message` immediately precedes `thread.exchange_completed`.
- `model.request` precedes its matching `model.response`.
- `tool.called` precedes its matching `tool.result` or `tool.failed`.

### I7 — Failure surface

Whole-exchange failure MUST emit `thread.exchange_failed` and raise
`AgentExecutionError` (sync) / end the stream (streaming). In-exchange
failures (single retry attempts) MUST NOT raise; they appear as events
only. Cancellation MUST emit `thread.exchange_failed` with cancellation
cause; sync MUST NOT raise on cancellation.

### I8 — Statelessness across calls

The engine MUST NOT carry exchange state in process memory across separate
`execute` calls on the same thread. Each call MUST reconstruct from the
log. A live runtime cache during a single exchange is permitted as an
optimization; it MUST NOT survive the call.

### I9 — Sandboxing

Tool execution MUST be routed through the configured Sandbox provider.
The engine MAY NOT bypass the Sandbox provider for any tool, builtin or
otherwise. (Phase 1's "native" sandbox provider is itself a provider
choice, not a bypass.)

### I10 — Secrets

Secrets resolved for tool invocation MUST NOT appear in any emitted event.
Tools that legitimately produce secret-derived output (e.g., an
authentication tool returning a token) MUST emit that output in
`tool.result` per the Materialization Rule — but values resolved from
the Secrets provider for tool *configuration* (API keys, credentials)
MUST NOT appear in `tool.called.arguments` or any other event.

### I11 — Capability honesty

`capabilities()` MUST honestly reflect what the engine supports. If the
engine cannot honor a feature the resolved config requires, the engine
MUST refuse to execute (raise) rather than execute partially or silently
substitute behavior.

---

## Part 4 — Open Items

1. **Lifecycle hook surface within the engine.** Pi-style extensions hook
   into the engine's loop (`before_model_request`, `after_tool_result`,
   etc.). The engine contract is silent on whether/how to expose hooks for
   *external* extension. Phase 1 may not need this; flagging for Phase 2
   when the IDE and extensions arrive.

2. **The threshold and policy for compaction triggering.** §2.6 sketches
   "80% of model window, oldest N turns excluded." Whether these are
   configurable, where they live in the config schema, and what the
   Memory provider's compaction algorithm interface looks like — deferred
   to per-Provider-Module specs.

3. **CancelToken shape.** §1.3 references a cancel token; its exact API
   (cooperative-poll vs. signal-on-event) is implementation-touched and
   should be settled with the FastAPI/Arq integration.

4. **MCP server lifecycle details.** §2.2 mentions lazy/eager; the
   transition (when does a lazy server actually start, how is its readiness
   surfaced) is a real implementation question and depends on which MCP
   library the engine wraps.

5. **Per-call vs. per-config tool registration.** If a thread runs many
   exchanges in sequence with the same config, must tools be re-registered
   each time? A live runtime cache (per I8) could keep them registered;
   but the contract is silent. Probably a non-issue but worth confirming
   when implementing.

6. **Streaming event ordering vs. log ordering.** §1.4 says yielded events
   match log order. The implementation must ensure the JSONL append and
   the iterator yield are consistent (no yielded-but-not-yet-flushed
   events visible). Mechanism is implementation; the invariant is contract.

7. **Multi-thread engines in Phase 2.** This contract is for a single
   thread per `execute` call. Phase 2's Workflow Engine calls `execute`
   once per node; concurrent calls on different threads are allowed.
   Concurrency on the *same* thread is not — the leaf is a single point
   of append. This should be a stated invariant when Phase 2 lands.

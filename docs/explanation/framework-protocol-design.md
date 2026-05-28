# Framework Protocol — Design Spike

**Draft 0.1 · 2026-05-28 · Status: pre-Step-12 research**

> Purpose: define the minimal Python Protocol that the Agent Engine depends
> on, informed by what CrewAI actually exposes today, so the FakeFramework
> used by Step 12a's integration tests only assumes guarantees the eventual
> CrewAI adapter can also provide. Read-only spike: no installs, no engine
> code, no dependency edits.

---

## Part 1 — What the engine needs from the framework

Walking the engine contract's observable-event obligations (Agent Engine
spec §2.4, §3 I1/I3/I5/I6) and translating each into a hook or
interception point:

| Engine obligation | Source | Hook required |
|---|---|---|
| `model.request` event fires **before** the model is invoked | spec §2.4 + I6 | A BEFORE-LLM-call interception point that exposes the assembled messages, tools, model name |
| `model.response` event fires **after** the model returns | spec §2.4 + I6 | The matching AFTER point with content, usage, latency |
| `tool.called` event fires **before** tool dispatch | spec §2.4 + I6 | A BEFORE-tool-call point with name + args verbatim |
| `tool.result` or `tool.failed` fires **after** dispatch | spec §2.4 + I6 | The matching AFTER point with output or error, distinguished |
| Retry attempts each produce events | spec §2.7 | Per-attempt observability — neither LLM nor tool retries may be collapsed by the framework |
| Cancellation completes any in-flight call, then ends | spec §1.6 | Cancellation cooperation at the call boundary |
| **Materialization (I3)** — the messages **as sent to the LLM** are captured verbatim in the `model.request` event | spec §3 I3, §B.6 | The engine must see the messages **after any framework wrapping** (system-prompt assembly, role normalization, multimodal formatting) but **before** they leave the framework toward the network |

The Materialization Rule is the load-bearing constraint. Listeners that
fire BEFORE the framework's final message-wrap are insufficient — replay
must reconstruct the same input the model actually saw, byte-for-byte.

Tool dispatch carries the same risk: if the framework wraps a tool
function in its own retry/parsing layer, the engine must see the *actual*
args (the parsed ones it would have executed against), not the model's
raw text.

---

## Part 2 — What CrewAI actually exposes (May 2026, v1.14.6)

Verified facts from public sources only; citations at the bottom.

### 2.1 Event taxonomy

CrewAI ships a paired `*StartedEvent` + `*Completed/Finished` + `*Failed`
shape for **LLM calls, tool calls, agent executions, and crew kickoffs**
under `lib/crewai/src/crewai/events/types/` (the events package moved
from `crewai.utilities.events` to `crewai.events`):

- `LLMCallStartedEvent` (BEFORE) — `messages`, `tools`, `callbacks`,
  `available_functions`, `model`, `call_id`
- `LLMCallCompletedEvent` (AFTER success) — adds `response`, `usage`
- `LLMCallFailedEvent` (AFTER failure) — `error: str`
- `ToolUsageStartedEvent` (BEFORE) — `tool_name`, `tool_args`,
  `run_attempts`, `agent_id`, `task_id`
- `ToolUsageFinishedEvent` (AFTER success) — adds `output`, `from_cache`,
  timing
- `ToolUsageErrorEvent` / `ToolValidateInputErrorEvent` /
  `ToolSelectionErrorEvent` / `ToolExecutionErrorEvent` (AFTER failure)

This is a one-to-one match with the engine's `model.*` and `tool.*`
event family. The BEFORE half satisfies I6's pre-event ordering for
each pair.

### 2.2 The Materialization gap

`LLMCallStartedEvent` is emitted inside `LLM.call()` with the messages
**as received** — i.e., after CrewAI's prompt assembly
(`Prompts.task_execution` in `lib/crewai/src/crewai/utilities/prompts.py`
concatenates role-playing + tools + task + skills blocks) but **before**
provider-specific formatting.

After the event fires, the same method runs
`_prepare_completion_params` → `_process_message_files` →
`_format_messages_for_provider` (multimodal reshaping, role flips for
o1 models, message-file attachment). The `params["messages"]` that
reaches `litellm.completion()` is therefore **not** what listeners
captured. The Completed/Failed events do see the post-format messages.

**Consequence:** the event-bus path cannot satisfy I3 verbatim. The
listener's pre-format view and the LLM's post-format input diverge —
replay against the listener-captured messages would fail to reproduce
what the model saw.

### 2.3 The LLM seam (the way out)

`crewai.BaseLLM` is a **public**, stable abstract class
(`lib/crewai/src/crewai/llms/base_llm.py`). The only `@abstractmethod`
is `call(messages, tools, callbacks, available_functions, from_task,
from_agent, response_model) -> str | Any`. Subclasses go through
`Agent(llm=MyLLM(...))`. A `BaseLLM` instance **bypasses
`crewai.llm.LLM` and litellm entirely** — no provider reformatting
ever runs.

This is the seam that closes the Materialization gap. By subclassing
`BaseLLM`, the engine controls the messages at the *exact* moment they
would have gone to the LLM. The engine captures verbatim, then routes
to the actual LLM (real or fake) through its own controlled path.

Introduced in PR #2277 (2025-03), stable since the 0.105.x line.

### 2.4 Tools — proxy dispatch

`Agent(tools=[BaseTool, ...])` is the contract. Dispatch in the ReAct
path goes `execute_tool_and_check_finality` →
`ToolUsage.parse_tool_calling` → `tool_usage.use(...)`; native function
calling routes through `available_functions[name](**args)` inside
`BaseLLM._handle_tool_execution`. Either path, the tool is invoked
through a CrewAI-controlled wrapper. The engine wraps each
`BaseTool` in a closure that captures `tool.called` BEFORE invocation
and `tool.result` / `tool.failed` AFTER — completely independent of
the event bus.

### 2.5 Async + concurrency

`crewai.events.event_bus` is a singleton bus with mixed dispatch:
sync handlers go through a `ThreadPoolExecutor(max_workers=10)`, async
handlers via `asyncio.run_coroutine_threadsafe()` onto a daemon-thread
event loop. **Handlers run off-thread.** `emit()` does not wait. Handler
exceptions are caught and silently logged.

This rules out the event bus as the engine's primary observation path.
The engine needs serial, in-band capture for `parent_event_id`
correctness (spec §2.3 "Note on concurrent callbacks"). The BaseLLM
subclass + tool wrappers give us that — they execute synchronously
inside the model loop, in the agent's own async context.

### 2.6 Retry observability

Mixed. CrewAI's own recursive retry on an "unsupported stop param" error
emits a fresh `LLMCallStartedEvent`, so it's visible. But **litellm's
internal retries** (`num_retries`, network backoff inside
`litellm.completion`) are invisible — one outer call, one Started/
Completed pair. Tool retries are surfaced via a `run_attempts` counter
on `ToolUsage*` events rather than one event per attempt.

By subclassing `BaseLLM` we make litellm-internal retry moot (we don't
use litellm). By wrapping tools we capture every invocation directly.
The engine owns retry policy per `RuntimeSpec.max_retries`; CrewAI's
internal retry stays at 0 / disabled where configurable.

---

## Part 3 — The proposed Framework Protocol

A minimal Protocol the engine depends on. Three types: the framework
itself, the LLM seam, the tool seam. Everything CrewAI-specific lives
in a separate adapter module the engine imports through the Protocol.

```python
# src/engine/framework.py (proposed)

from typing import Any, Awaitable, Callable, Protocol


# ---- LLM seam ---------------------------------------------------------

class LLMRequest(BaseModel):
    """The messages + tool schemas the framework would send to the LLM,
    captured at the moment of the call — already post-wrap, ready to be
    materialized into `model.request.data` verbatim (I3)."""
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    model: str
    parameters: dict[str, Any]


class LLMResponse(BaseModel):
    """What the engine returns to the framework. Content or tool calls."""
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    usage: dict[str, int] | None
    raw: dict[str, Any]


class LLMHandle(Protocol):
    """Engine-provided LLM. The framework calls this; the engine controls
    the body, which means the engine sees `LLMRequest` BEFORE the model
    is invoked and `LLMResponse` AFTER. Materialization happens inside
    `__call__`."""

    async def __call__(self, request: LLMRequest) -> LLMResponse: ...


# ---- Tool seam --------------------------------------------------------

class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any]


class ToolHandle(Protocol):
    """Engine-provided tool wrapper. The framework looks up by name +
    calls this; the engine controls the body, which means the engine
    sees `tool.called` BEFORE and `tool.result`/`tool.failed` AFTER.
    The framework MUST invoke through this handle, not the raw tool."""

    name: str
    schema: dict[str, Any]

    async def __call__(self, call: ToolCall) -> Any: ...


# ---- Framework seam ---------------------------------------------------

class Framework(Protocol):
    """Drive one exchange to completion."""

    async def run_exchange(
        self,
        *,
        system_prompt: str,
        user_message: str,
        llm: LLMHandle,
        tools: list[ToolHandle],
        max_iterations: int,
        cancellation: Callable[[], bool] | None = None,
    ) -> str:
        """Run the agent loop. Each model turn calls `llm`; each tool
        invocation calls the matching `ToolHandle`. Returns the final
        agent text. Raises framework-specific exceptions on terminal
        failure; the engine maps these to ExchangeFailed events."""
```

### What each method needs vs what CrewAI delivers

| Protocol method | Engine needs | CrewAI delivers | Gap |
|---|---|---|---|
| `LLMHandle.__call__` | Sees messages + tools verbatim at call moment; returns content or tool calls | `BaseLLM.call` / `acall` — sole abstractmethod, exact match | None — subclass `BaseLLM`, delegate to `LLMHandle` |
| `ToolHandle.__call__` | Invoked by framework with parsed args; before/after capture is the engine's choice | `BaseTool` subclasses, invoked by `tool_usage.use()` or `_handle_tool_execution` | None — wrap each `ToolHandle` in a `BaseTool` subclass that delegates |
| `Framework.run_exchange` | One exchange = build an agent + task + crew, kickoff_async, return result | `Crew.kickoff_async()` exists; sync `kickoff` too | Setup overhead per call (build Agent/Task/Crew each time) but matches engine I8 statelessness |
| `cancellation` poll | Cooperative cancel between iterations | CrewAI has no first-class cancel; we poll inside `LLMHandle.__call__` and raise to abort the loop | Acceptable — the LLM seam is the natural cancellation point |

The FakeFramework used in Step 12a integration tests implements
`run_exchange` directly: loop calling `llm` and dispatching tool calls
the LLM emits. No CrewAI dependency; same Protocol; same engine code.

---

## Part 4 — Open questions / verdict

### Gaps that the contract still satisfies

1. **Provider message reformatting** — closed by subclassing `BaseLLM`
   instead of using `crewai.llm.LLM`. Litellm's reformatting never runs.

2. **Concurrent listeners** — closed by NOT using the event bus as the
   primary observation path. The `LLMHandle` and `ToolHandle` seams run
   in-band with the agent loop, in the same async context, serial by
   construction.

3. **Tool retry granularity** — closed by capturing inside `ToolHandle`,
   not via `ToolUsageStartedEvent`. Every invocation passes through the
   engine.

4. **Litellm-internal retry invisibility** — closed by the engine
   owning retry per `RuntimeSpec.max_retries`. CrewAI's per-call retry
   stays at 0; the engine wraps `llm()` with its own retry policy and
   emits one `model.request`/`model.response` pair per attempt.

### Gaps the contract does **not** yet handle

1. **CrewAI prompt-template injection.** Even with a `BaseLLM` subclass,
   the messages reaching `__call__` are CrewAI's assembled list (role-
   playing + tools + task + skills blocks), not the engine's
   Assembly-Procedure output verbatim. Two responses:

   - **(a) Accept** — record the post-CrewAI messages as the
     materialized `model.request.data`. The engine's Assembly Procedure
     is its own derivation; CrewAI's wrap is also deterministic. Replay
     reconstructs the engine's projection; the divergence is documented.
   - **(b) Bypass** — use CrewAI in a "minimal driver" mode where we
     supply a single message and short-circuit the prompt builder. May
     require monkey-patching or a different entry point than
     `Crew.kickoff_async`. Higher risk; defer.
   - **(c) Contract amendment** — say the captured `model.request.data`
     is "messages as observed at the LLM seam", not "Assembly Procedure
     output verbatim." This is the most honest framing and only requires
     a clarifying note in spec §B.6.

   **Proposed:** (c). The Materialization Rule's purpose is replay
   honesty; capturing what actually went to the model satisfies that.
   The Assembly Procedure is the engine's input to CrewAI; CrewAI's
   wrap is part of the framework's contribution and is itself
   replayable given the same inputs + same framework version. The
   spec amendment is one sentence.

2. **`force_skills` synthetic read positioning.** Engine spec §2.1
   step 7 says the synthetic `tool.called` for `read` on SKILL.md
   happens BEFORE `thread.exchange_started`. CrewAI knows nothing
   about this — it's pre-framework work. **No gap**: the engine emits
   these events itself before calling `Framework.run_exchange`. Force-
   invoke is engine-only; CrewAI sees only the final user message.

3. **Multi-message context (history).** The engine's Assembly Procedure
   walks the thread tree to build the message list. CrewAI by default
   builds its own. To get the engine's history into CrewAI:
   - Pass via `Agent.execute_task`'s `context` parameter (string of
     prior turns) — works but lossy.
   - Or supply the assembled messages directly inside `LLMHandle` by
     prepending them to what CrewAI passes — works but couples engine
     to CrewAI's wrap behavior.

   **Proposed:** the `LLMHandle.__call__` receives the framework's
   messages (CrewAI's wrap). The engine's history goes in via the
   framework's input (task description + context); CrewAI threads it
   into the wrap. Capture the post-wrap list as the materialized
   request. Document in spec §B.6 that "messages" means "as observed
   at the LLM seam, post-framework-wrap."

### Verdict

**CrewAI can satisfy the Framework Protocol** if and only if the engine:

1. Subclasses `crewai.BaseLLM` and routes through `LLMHandle` (not
   `crewai.llm.LLM`).
2. Wraps tools as `BaseTool` subclasses delegating to `ToolHandle`.
3. Treats CrewAI's event bus as **secondary telemetry**, not the
   primary observation path. Primary observation is in-band via the
   two seams.
4. Owns retry policy at the engine layer (`RuntimeSpec.max_retries`),
   not delegated to CrewAI/litellm.
5. Spec §B.6 amended (one sentence) to clarify that the materialized
   `model.request.data.messages` is what the LLM seam sees, which may
   include framework-applied wrapping.

With those five conditions, every engine-contract invariant — I1, I3,
I5, I6, I7, I8 — can be honored. The Step 12a engine code targets the
Framework Protocol; Step 12b ships the CrewAI adapter; the FakeFramework
in tests/fixtures/ provides the same Protocol shape so TC-ENG-01..11
become true Framework-Protocol integration tests that the real adapter
will inherit.

**No escalation needed.** The amendment in §B.6 is the only contract
change; everything else is implementation.

---

## Citations

CrewAI source (all under
`https://github.com/crewAIInc/crewAI/blob/main/`):

- LLM events:
  `lib/crewai/src/crewai/events/types/llm_events.py`
- Tool events:
  `lib/crewai/src/crewai/events/types/tool_usage_events.py`
- Agent events:
  `lib/crewai/src/crewai/events/types/agent_events.py`
- Crew events:
  `lib/crewai/src/crewai/events/types/crew_events.py`
- Event bus:
  `lib/crewai/src/crewai/events/event_bus.py`
- Base listener:
  `lib/crewai/src/crewai/events/base_event_listener.py`
- BaseLLM:
  `lib/crewai/src/crewai/llms/base_llm.py`
- LLM (litellm path):
  `lib/crewai/src/crewai/llm.py`
- Prompt builder:
  `lib/crewai/src/crewai/utilities/prompts.py`
- LLM call sites:
  `lib/crewai/src/crewai/utilities/agent_utils.py`,
  `lib/crewai/src/crewai/agents/crew_agent_executor.py`
- Agent core:
  `lib/crewai/src/crewai/agent/core.py`
- BaseLLM introduction:
  https://github.com/crewAIInc/crewAI/pull/2277

CrewAI docs:

- Custom LLM:
  https://docs.crewai.com/en/learn/custom-llm
- LLM call hooks:
  https://docs.crewai.com/en/learn/llm-hooks
- Without LiteLLM:
  https://docs.crewai.com/en/learn/litellm-removal-guide
- Event listener:
  https://docs.crewai.com/concepts/event-listener
- Changelog:
  https://docs.crewai.com/en/changelog

Phase 1 contracts (internal):

- Agent Engine: `docs/explanation/agent-engine-contract.md`
- Schemas §B.5 (Assembly Procedure), §B.6 (Materialization Rule):
  `docs/explanation/specification.md`
- Module API Reference §11: `docs/reference/module-api-reference.md`

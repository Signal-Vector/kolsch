"""The Framework Protocol — the seam the engine depends on.

Implements the framework boundary per the
[framework protocol design](../explanation/framework-protocol-design.md).
The engine depends on this Protocol, never on a concrete framework.
Reference implementations:
[`MinimalFramework`][engine.mock_framework.MinimalFramework] (the v1 default
for engine tests and zero-LLM-cost dev workflows) and
[`CrewAIFramework`][engine.crewai_framework.CrewAIFramework] (the real
CrewAI adapter).

The Protocol is deliberately minimal — over-broad guarantees that
CrewAI cannot deliver are the failure mode to avoid.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from llm.protocol import LLMCallContext
    from schemas import ModelSpec

# ---------------------------------------------------------------------------
# Value types — what flows across the seams
# ---------------------------------------------------------------------------


class Message(BaseModel):
    """One chat-style message crossing the LLM seam.

    Fields match the OpenAI chat completions shape because every real
    provider (OpenAI / Anthropic / Google / litellm) speaks this idiom
    and CrewAI's `BaseLLM.call` accepts `list[LLMMessage]` of this
    shape. The engine emits no events with this type directly;
    messages are projected from the Assembly Procedure output and
    recorded on `model.request` only by reference (the events on the
    path are the verbatim record).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    name: str | None = None


class LLMRequest(BaseModel):
    """The exact input the framework would send to the LLM.

    Captured at the LLM seam (inside the
    [`LLMHandle`][engine.framework.LLMHandle] body) — after any
    framework wrapping (CrewAI's prompt-template injection, multimodal
    reshaping) and immediately before the LLM is invoked. This is the
    materialized view; replay reconstructs the same value from the
    engine's events.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[Message]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    model: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """What the engine returns to the framework.

    `content` is plain text; `tool_calls` is the structured
    tool-invocation list when the model wants to call one or more
    tools (mutually exclusive in v1 — a model either answers in text
    OR requests tools, not both, per the
    [`MinimalFramework`][engine.mock_framework.MinimalFramework] loop's
    invariant). `raw` carries the provider-shaped response dict for
    downstream materialization on `model.response.data` — the parts
    of the LLM's reply the engine cannot derive from `content` +
    `tool_calls` alone (provider message-id, finish_reason,
    system_fingerprint, cached-prompt markers, reasoning trace
    blocks). `MinimalFramework` satisfies it with `raw={}`; the CrewAI
    adapter populates it from whatever the configured provider
    returned. The asymmetry is deliberate — `raw` is a passthrough
    channel, not a Mock-contract gap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None
    tool_calls: list[dict[str, Any]] | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """The arguments the model emitted, validated by the engine.

    The engine validates against the tool's `args_schema` before this
    Protocol type is constructed. `arguments` MUST NOT contain
    resolved secret values; secrets flow only via
    [`ToolContext.secrets`][tools.decorator.ToolContext] on the inner
    side of the seam — never through the framework, never through
    this type, never through `tool.called.arguments` in events.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    arguments: dict[str, Any]
    tool_call_id: str | None = None


# ---------------------------------------------------------------------------
# The three seams
# ---------------------------------------------------------------------------


class LLMHandle(Protocol):
    """Engine-provided LLM the framework calls.

    The engine owns the body: it captures `model.request` BEFORE the
    call, drives the configured LLM driver, captures `model.response`
    AFTER, and translates the driver's reply into
    [`LLMResponse`][engine.framework.LLMResponse]. Materialization
    holds at this point because the
    [`LLMRequest`][engine.framework.LLMRequest] is what the LLM
    actually saw.
    """

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        """Drive the configured LLM driver with `request` and return the reply."""


class ToolHandle(Protocol):
    """Engine-provided tool wrapper the framework invokes by name.

    The engine owns the body: capture `tool.called` BEFORE, route
    through the [Tools subsystem](../explanation/tools-subsystem.md)
    (which routes through the sandbox), then capture
    `tool.result` / `tool.failed` AFTER. On a Diagnostic failure the
    wrapper MUST return a value the framework can include as a
    tool-result message instead of raising — the engine contract
    forbids in-exchange failures from raising into the framework
    loop.

    `description` was added by bug-024. The framework needs the tool's
    prose description (from `@tool(description=...)`) to build the
    outgoing OpenAI-spec `tools=` payload — without it every advertised
    tool ships as `"Tool: <name>"` and models (Kimi K2.6 especially)
    fall back to `bash` for anything they can spawn as a subprocess.
    """

    name: str
    description: str
    schema: dict[str, Any]

    async def __call__(self, call: ToolCall) -> Any:
        """Route `call` through the Tools subsystem and return the tool's result."""


class Framework(Protocol):
    """Drive one exchange to completion.

    The framework receives the engine's seam objects (`llm`, `tools`)
    and the engine's pre-assembled inputs (`system_prompt`,
    `user_message`). It returns the agent's final text; the engine
    materializes that into an `agent.message` event. Errors raised
    from `run_exchange` are Fatal — Diagnostic failures must be
    swallowed inside the seam wrappers and routed back as tool
    results.

    `cancellation` is a poll callback (returns True when the engine
    wants to abort). The canonical
    [`CancelToken`][engine.context.CancelToken] is constructed by the
    engine and bound to this callback by pre-flight before being
    handed to the framework.

    `cancel_token` is the same
    [`CancelToken`][engine.context.CancelToken] exposed directly so
    the framework can race `await llm(...)` / `await handle(...)`
    against `cancel_token.wait_cancelled()` instead of only polling
    at iteration boundaries. `cancellation` (the callable) stays for
    backwards compatibility with frameworks that only need a poll
    check; new code prefers `cancel_token`. Both can be passed;
    framework adapters use whichever they need.

    `max_iterations` is an optional per-call cap on the framework's
    internal loop. `None` (the default) means no cap — the framework
    loops until the LLM stops emitting tool calls or `cancellation`
    fires. The engine does not impose a cap in v1; the kwarg remains
    on the Protocol so individual frameworks can offer a per-call
    override if they need one.

    `tool_bridge` (spec-121) is an optional, engine-owned per-exchange
    tool bridge (`tools.bridge.ToolBridgeServer` — typed `Any` here so
    this module needs no `tools.bridge` import; the concrete framework
    narrows it). The engine opens/closes it and passes it only when it
    AUTO-selects the subprocess framework path (a `claude_code` agent
    with ≥1 registered tool); every other framework ignores the kwarg,
    exactly like `max_iterations`.
    """

    async def run_exchange(
        self,
        *,
        system_prompt: str,
        user_message: str,
        history: Sequence[Message] = (),
        llm: LLMHandle,
        tools: Sequence[ToolHandle],
        max_iterations: int | None = None,
        cancellation: Callable[[], bool] | None = None,
        cancel_token: Any = None,
        tool_bridge: Any = None,
    ) -> str:
        """Drive one exchange to completion and return the agent's final text."""


# ---------------------------------------------------------------------------
# LLM driver — the "what" behind the LLMHandle
# ---------------------------------------------------------------------------


#: LLM driver signature: takes the captured context dict and returns
#: whatever the underlying model produced. The engine translates the
#: driver's reply into [`LLMResponse`][engine.framework.LLMResponse]
#: inside the [`LLMHandle`][engine.framework.LLMHandle] body. In tests
#: this is bound to `MockLLM.agenerate`; the CrewAI adapter binds it
#: to a litellm-backed function.
LLMDriver = Callable[[dict[str, Any]], Awaitable[Any]]


#: A per-attempt LLM callable — what an
#: [`LLMFactory`][engine.framework.LLMFactory] produces for one spec.
LLMCallable = Callable[[LLMRequest], Awaitable[LLMResponse]]


class LLMFactory(Protocol):
    """Builds the per-attempt LLM callable for a `ModelSpec`.

    The engine's fallback orchestrator calls this lazily once per
    chain entry; compaction calls it for its summariser model.
    Production wires `engine._crewai_adapter.make_build_llm_factory`.

    `context` is keyword-only and has no default so every caller states
    it explicitly: production callers pass the owning session's
    [`LLMCallContext`][llm.protocol.LLMCallContext]; `None` comes only
    from test doubles. Implementations forward it to `llm.build_llm`
    (bug-144).
    """

    def __call__(
        self,
        spec: ModelSpec,
        /,
        *,
        context: LLMCallContext | None,
    ) -> Awaitable[LLMCallable]:
        """Return (asynchronously) the LLM callable for `spec`."""
        ...

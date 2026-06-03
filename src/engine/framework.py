"""Framework Protocol — docs/explanation/framework-protocol-design.md.

The Agent Engine depends on this Protocol, never on a concrete framework
implementation. Two reference implementations exist:

- :class:`engine.fake_framework.FakeFramework` — the v1 default for
  engine tests and zero-LLM-cost dev workflows (this Step 12a).
- ``engine.crewai_framework.CrewAIFramework`` — the real CrewAI adapter
  (Step 12b; not in this commit, do NOT install CrewAI here).

Three Protocols:

- :class:`LLMHandle` — engine-provided LLM. The framework calls it; the
  engine controls the body and captures ``model.request`` BEFORE +
  ``model.response`` AFTER. Materialization (I3) happens inside the
  engine's implementation.
- :class:`ToolHandle` — engine-provided tool wrapper. The framework
  invokes by name; the engine captures ``tool.called`` BEFORE +
  ``tool.result``/``tool.failed`` AFTER. Sandboxing (I9) and secrets
  (I10) live inside the wrapper, never on the framework side.
- :class:`Framework` — drives one exchange to completion. The single
  method ``run_exchange`` returns the agent's final text; per-step
  observation is via the two seams above, not via framework callbacks.

The Protocol is deliberately minimal — every surface justified in the
prereq design note's §3. Over-broad guarantees that CrewAI cannot
deliver are the failure mode to avoid.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Value types — what flows across the seams
# ---------------------------------------------------------------------------


class Message(BaseModel):
    """One chat-style message — what crosses the LLM seam.

    Fields match the OpenAI chat completions shape because every real
    provider (OpenAI / Anthropic / Google / litellm) speaks this idiom
    and CrewAI's `BaseLLM.call` accepts `list[LLMMessage]` of this
    shape. The Phase 1 engine emits no events with this type directly;
    Messages are projected from Assembly-Procedure output and recorded
    on `model.request` only by reference (the events on the path are
    the verbatim record per §B.6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    name: str | None = None


class LLMRequest(BaseModel):
    """The exact input the framework would send to the LLM.

    Captured at the LLM seam (inside :class:`LLMHandle`'s body) — i.e.
    after any framework wrapping (CrewAI's prompt-template injection,
    multimodal reshaping) and immediately before the LLM is invoked.
    §B.6 (draft-0.5 clarification, 2026-05-28) says this is the
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

    ``content`` is plain text; ``tool_calls`` is the structured tool-
    invocation list when the model wants to call one or more tools
    (mutually exclusive in v1 — a model either answers in text OR
    requests tools, not both, per the FakeFramework loop's
    invariant). ``raw`` carries the provider-shaped response dict for
    downstream materialization on ``model.response.data`` — i.e., the
    part of the LLM's reply the engine cannot derive from
    ``content`` + ``tool_calls`` alone (provider message-id,
    finish_reason, system_fingerprint, cached-prompt markers,
    reasoning trace blocks, etc.). FakeFramework satisfies it with
    ``raw={}``; the CrewAI adapter populates it from whatever the
    configured provider returned. This asymmetry is deliberate —
    ``raw`` is a passthrough channel, not a Fake-contract gap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None
    tool_calls: list[dict[str, Any]] | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """The arguments the model emitted, validated against the tool's
    args_schema by the engine before this Protocol type is constructed.

    ``arguments`` MUST NOT contain resolved secret values (I10). Secrets
    flow only via the engine's :class:`tools.ToolContext.secrets` on
    the inner side of the seam — never through the framework, never
    through this Protocol type, never through ``tool.called.arguments``
    in events.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    arguments: dict[str, Any]
    tool_call_id: str | None = None


# ---------------------------------------------------------------------------
# The three seams
# ---------------------------------------------------------------------------


class LLMHandle(Protocol):
    """Engine-provided LLM. The framework calls this.

    The engine owns the body: it captures ``model.request`` BEFORE the
    call, drives the configured LLM driver, captures ``model.response``
    AFTER, and translates the driver's reply into :class:`LLMResponse`.
    Materialization (I3) is honored at this point because the
    ``LLMRequest`` is what the LLM actually saw.
    """

    async def __call__(self, request: LLMRequest) -> LLMResponse: ...


class ToolHandle(Protocol):
    """Engine-provided tool wrapper. The framework invokes by name.

    The engine owns the body: capture ``tool.called`` BEFORE, route
    through the Tools subsystem (which routes through Sandbox, per
    I9), then capture ``tool.result``/``tool.failed`` AFTER. On a
    Diagnostic failure (T-I-style), the wrapper MUST return a value
    the framework can include as a tool-result message instead of
    raising — the engine spec §1.6 forbids in-exchange failures from
    raising into the framework loop.
    """

    name: str
    schema: dict[str, Any]

    async def __call__(self, call: ToolCall) -> Any: ...


class Framework(Protocol):
    """Drive one exchange to completion.

    Implementations:

    - :class:`engine.fake_framework.FakeFramework` (v1 default for
      tests; uses a plain ``[system, user]`` message list and loops
      tool calls until a text response).
    - CrewAI adapter (Step 12b — wraps `Crew.kickoff_async()`).

    The framework receives the engine's seam objects (``llm``,
    ``tools``) and the engine's pre-assembled inputs (``system_prompt``,
    ``user_message``). It returns the agent's final text; the engine
    materializes that into an ``agent.message`` event. Errors raised
    from ``run_exchange`` are Fatal — Diagnostic failures must be
    swallowed inside the seam wrappers and routed back as tool
    results (engine spec §1.6).

    ``cancellation`` is a poll callback (returns True when the engine
    wants to abort). Phase 1 uses the simplest possible shape; the
    canonical :class:`engine.context.CancelToken` (per Module API §13.5)
    is constructed by the engine and bound to this callback by the
    engine's preflight before being handed to the framework.

    ``max_iterations`` is an optional per-call cap on the framework's
    internal loop. ``None`` (the default) means no cap — the framework
    loops until the LLM stops emitting tool calls or ``cancellation``
    fires. The engine does not impose a cap in v1; the kwarg remains
    on the Protocol so individual frameworks can offer a per-call
    override if they need one.
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
    ) -> str: ...


# ---------------------------------------------------------------------------
# LLM driver — the "what" behind the LLMHandle
# ---------------------------------------------------------------------------


#: Phase 1 LLM driver signature: takes the captured context dict and
#: returns whatever the underlying model produced. The engine
#: translates the driver's reply into :class:`LLMResponse` inside the
#: :class:`LLMHandle` body. In v1 tests this is bound to
#: ``FakeLLM.agenerate``; the Step 12b CrewAI adapter binds it to a
#: real litellm-backed function.
LLMDriver = Callable[[dict[str, Any]], Awaitable[Any]]

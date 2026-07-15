"""ExecutionContext, CancelToken, EngineCapabilities, and AgentExecutionError.

The runtime data shapes the engine accepts and emits at its public
boundary. `ExecutionContext` is what callers pass *in*;
`AgentExecutionError` is what they get *out* on failure; `CancelToken`
is the cooperative cancellation handle they may attach;
`EngineCapabilities` is what the engine *declares* about itself.

For the full engine contract — `execute` semantics, ordering rules,
cancellation guarantees — see the
[architecture pages](../architecture/index.md).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from schemas import ThreadId
from workflows.ids import TraceId, WorkflowRunId

if TYPE_CHECKING:
    from schemas import Event


# ---------------------------------------------------------------------------
# CancelToken — cooperative cancellation handle
# ---------------------------------------------------------------------------


class CancelToken:
    """Cooperative cancellation handle.

    Three methods: `cancel`, `is_cancelled`, `wait_cancelled`. The
    engine polls `is_cancelled` at safe points (between framework
    iterations, inside the LLM seam); the framework receives the
    polling callback so it can abort mid-loop too.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        """Signal cancellation. Idempotent; safe to call from any task."""
        self._event.set()

    def is_cancelled(self) -> bool:
        """Return `True` if `cancel` has been called."""
        return self._event.is_set()

    async def wait_cancelled(self) -> None:
        """Suspend until `cancel` is called."""
        await self._event.wait()


# ---------------------------------------------------------------------------
# CombinedCancelToken — cancel-on-any composition
# ---------------------------------------------------------------------------


class CombinedCancelToken(CancelToken):
    """Cancel-on-any composition of two or more CancelTokens.

    Used by the workflow service (spec-012) to thread a workflow's
    cancel token into sub-agent `ExecutionContext.cancellation`
    alongside the exchange's existing token. Neither wins; whichever
    fires first triggers cancellation.

    `.is_cancelled()` returns True if either the combined token's OWN
    local flag was fired via `.cancel()` OR any wrapped inner token
    reports `is_cancelled() == True`.

    `.cancel()` sets the LOCAL flag only. It does NOT propagate to
    inner tokens. This is deliberate: a sub-agent's local abort
    shouldn't accidentally cancel the parent workflow or exchange.
    Callers that want to cascade must call `.cancel()` on each inner
    token explicitly.

    `.wait_cancelled()` awaits ANY of {local flag, inner_1, inner_2,
    ...}. Uses `asyncio.wait(..., FIRST_COMPLETED)` over the inner
    tokens' `wait_cancelled()` coroutines plus the local event's
    `.wait()`. The ephemeral inner-wait tasks are cancelled in the
    `finally` clause so no tasks leak past resolution.
    """

    def __init__(self, *inner: CancelToken) -> None:
        super().__init__()
        if len(inner) < 1:
            raise ValueError(
                "CombinedCancelToken requires at least one inner token",
            )
        self._inner: tuple[CancelToken, ...] = inner

    def is_cancelled(self) -> bool:
        """Return `True` if this token's local flag OR any inner is cancelled."""
        return self._event.is_set() or any(t.is_cancelled() for t in self._inner)

    async def wait_cancelled(self) -> None:
        """Suspend until the first of {local flag, any inner token} fires."""
        # If already cancelled, short-circuit without spawning tasks.
        if self.is_cancelled():
            return

        waits: list[asyncio.Task[Any]] = [
            asyncio.create_task(t.wait_cancelled()) for t in self._inner
        ]
        waits.append(asyncio.create_task(self._event.wait()))
        try:
            await asyncio.wait(
                waits,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for w in waits:
                if not w.done():
                    w.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await w


# ---------------------------------------------------------------------------
# ExecutionContext — the per-execute payload
# ---------------------------------------------------------------------------


class ExecutionContext(BaseModel):
    """The per-execute payload carrying the thread and trigger.

    Every call to [`AgentEngine.execute`][engine.engine.AgentEngine.execute]
    takes one of these. The payload identifies which thread the
    exchange runs against and what kicked it off — a user message, a
    force-invoke message that pins a skill, or single-shot inputs.

    Three trigger shapes are valid; the engine dispatches on
    `trigger["kind"]`:

    ```python
    # Conversational
    {"kind": "user_message", "content": <typed>}

    # Conversational with force-invoke
    {"kind": "user_message_with_force_skill",
     "content": <typed>,
     "force_skills": ["<bundle>/skills/<name>", ...]}

    # Single-shot
    {"kind": "invocation", "inputs": <typed>}
    ```

    The remaining fields are optional cross-call threading:
    `cancellation` for cooperative cancellation (the engine constructs
    an internal no-op token when omitted); `trace_id` for
    workflow→agent trace propagation (the workflow agent runner sets
    it so the spawned session's JSONL and the originating workflow's
    JSONL share a single trace id); and `caller_chain` for authz
    audit framing (the workflow agent runner prepends the agent's ref
    so audit events record the workflow→agent frame). All three
    defaults preserve backward-compatibility with callers that
    haven't opted in.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    thread_id: ThreadId
    trigger: dict[str, Any]
    cancellation: CancelToken | None = None
    trace_id: TraceId | None = None
    caller_chain: tuple[str, ...] = Field(default_factory=tuple)

    # spec-004 r-review r1 — populated by the workflow executor
    # (spec-011) when it invokes a sub-agent inside a workflow node.
    # `None` for every other caller (chat, `kolsch run`, tests). The
    # `InlineApprovalCallback._derive_source` helper reads these to
    # tag emitted `kolsch.prompt.requested` events as workflow-scoped
    # so a downstream renderer can group approvals by run.
    workflow_run_id: WorkflowRunId | None = None
    workflow_node_name: str | None = None


# ---------------------------------------------------------------------------
# EngineCapabilities — what this engine declares
# ---------------------------------------------------------------------------


class EngineCapabilities(BaseModel):
    """What this engine declares it can do.

    Surfaced via
    [`AgentEngine.capabilities`][engine.engine.AgentEngine.capabilities].
    The shape is minimal today; values get richer as features arrive.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine_version: str
    assembly_versions: list[str] = Field(default_factory=list)
    supported_modes: list[str] = Field(default_factory=list)
    supported_features: list[str] = Field(default_factory=list)
    supported_providers: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AgentExecutionError — fatal-failure transport for sync callers
# ---------------------------------------------------------------------------


class AgentExecutionError(Exception):
    """Raised by `AgentEngine.execute` on Fatal exchange failure.

    Carries the `thread.exchange_failed` event the engine emitted
    just before raising. See
    [`AgentEngine.execute`][engine.engine.AgentEngine.execute] for the call
    contract.

    Two surfaces, two behaviours:

    - `execute` raises this so the caller can `try/except`.
    - `execute_stream` does NOT raise; it yields the failed event as
      the last item and ends. The caller reads the failure from the
      stream.

    NOT a `PlatformError`. It's a transport mechanism for the
    synchronous caller; the actual platform error lives on
    `failed_event.data["error"]`.
    """

    def __init__(self, failed_event: Event) -> None:
        super().__init__(
            f"Agent execution failed at event {failed_event.event_id}",
        )
        self.failed_event = failed_event

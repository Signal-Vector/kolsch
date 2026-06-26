"""ExecutionContext + CancelToken + EngineCapabilities + AgentExecutionError.

Per Module API Reference §11.2 + §13.5 + agent-engine-contract §1.3.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from schemas import ThreadId
from workflows.ids import TraceId

if TYPE_CHECKING:
    from schemas import Event


# ---------------------------------------------------------------------------
# CancelToken — Module API Reference §13.5
# ---------------------------------------------------------------------------


class CancelToken:
    """Cooperative cancellation handle.

    Three methods per §13.5: :meth:`cancel`, :meth:`is_cancelled`,
    :meth:`wait_cancelled`. The engine polls
    :meth:`is_cancelled` at safe points (between framework iterations,
    inside the LLM seam); the framework receives the polling callback
    so it can abort mid-loop too.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait_cancelled(self) -> None:
        await self._event.wait()


# ---------------------------------------------------------------------------
# ExecutionContext — agent-engine-contract §1.3 + Module API Reference §11.2
# ---------------------------------------------------------------------------


class ExecutionContext(BaseModel):
    """The per-execute payload carrying the thread + trigger.

    Trigger shapes (spec §1.3):

    - Conversational: ``{kind: "user_message", content: <typed>}``.
    - Conversational with force-invoke:
      ``{kind: "user_message_with_force_skill", content: <typed>,
         force_skills: ["<bundle>/skills/<name>", ...]}``.
    - Single-shot: ``{kind: "invocation", inputs: <typed>}``.

    ``cancellation`` is optional — the engine constructs an internal
    no-op token when omitted.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    thread_id: ThreadId
    trigger: dict[str, Any]
    cancellation: CancelToken | None = None
    # Phase 3.2 — workflow→agent trace propagation (master spec §11.6.4).
    # When set by a caller (the workflow agent runner), the engine
    # threads this id into every event it emits so the spawned session's
    # JSONL and the originating workflow's JSONL share a single
    # ``trace_id`` across all events. ``None`` defaults preserve
    # backward-compat with chat / API / sub-agent callers; those paths
    # are unaffected until they opt in.
    trace_id: TraceId | None = None
    # Phase 3.2 — proximate-first caller chain (master spec §13.x).
    # The workflow agent runner prepends the agent's ref before passing
    # it in so authz audit events record the workflow→agent frame.
    # ``()`` default preserves backward-compat with existing callers.
    caller_chain: tuple[str, ...] = Field(default_factory=tuple)


# ---------------------------------------------------------------------------
# EngineCapabilities — agent-engine-contract §1.7
# ---------------------------------------------------------------------------


class EngineCapabilities(BaseModel):
    """What this engine can do — surfaced via :meth:`AgentEngine.capabilities`.

    Phase 1 v1 keeps the shape minimal; values get richer as features
    arrive.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine_version: str
    assembly_versions: list[str] = Field(default_factory=list)
    supported_modes: list[str] = Field(default_factory=list)
    supported_features: list[str] = Field(default_factory=list)
    supported_providers: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AgentExecutionError — agent-engine-contract §1.6
# ---------------------------------------------------------------------------


class AgentExecutionError(Exception):
    """Raised by sync :meth:`AgentEngine.execute` on Fatal exchange failure.

    Carries the ``thread.exchange_failed`` event the engine emitted
    just before raising. Per spec §1.6:

    - ``execute`` raises this.
    - ``execute_stream`` yields ``failed_event`` and ends; it does NOT
      raise this (the caller reads the failed event from the stream).

    NOT a :class:`PlatformError`. It is a transport mechanism for the
    caller of the sync API; the actual platform error is on
    ``failed_event.data["error"]`` (per Error Model §5.2).
    """

    def __init__(self, failed_event: Event) -> None:
        super().__init__(
            f"Agent execution failed at event {failed_event.event_id}",
        )
        self.failed_event = failed_event

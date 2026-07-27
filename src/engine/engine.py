"""The Agent Engine — recorder around the framework.

Implements the engine per the
[agent engine contract](../explanation/agent-engine-contract.md):
the wrapped framework (CrewAI in v1) does the agentic work — model
calls, tool dispatch, retry loops; the engine prepares inputs,
registers hooks, observes execution, emits events per the schema,
returns the result.

The execute path:

1. [`execute_stream`][engine.engine.AgentEngine.execute_stream] — the
   streaming primitive. Spawns an internal runner task that walks
   pre-flight, framework setup, the framework driver loop, and
   finalize, pushing events into an `asyncio.Queue`. The generator
   yields events from the queue as they land.
2. [`execute`][engine.engine.AgentEngine.execute] — collects the stream;
   returns the final `agent.message` event; raises
   [`AgentExecutionError`][engine.AgentExecutionError] on Fatal
   failure.

Per-thread serialization: a lock per active thread ensures the
`parent_event_id` chain is well-formed even when the framework
invokes the LLM/tool seams from concurrent callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from engine.assembly import ASSEMBLY_VERSION, assemble_context
from engine.assembly_cache import AssemblyCache, _Key, hash_tool_schemas
from engine.context import (
    AgentExecutionError,
    CancelToken,
    EngineCapabilities,
)
from engine.observers import _ObservedToolHandle
from errors import (
    AgentUnauthorized,
    CapabilityMismatch,
    ConfigExtendsTemplateWithoutInheritance,
    ConfigFallbackDuplicate,
    ModelRetryExhausted,
    PlatformError,
    PlatformInternalError,
)
from schemas import (
    Actor,
    ConfigSetData,
    Error,
    Event,
    EventId,
    EventType,
    ResolvedAgentConfig,
    ThreadExchangeCompletedData,
    ThreadExchangeFailedData,
    ThreadExchangeStartedData,
    ToolCalledData,
    ToolResultData,
    UserMessageData,
)
from schemas.events import AgentMessageData
from tools import ToolContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import crewai

    from assets import AssetManagement
    from bus import EventBus
    from engine.assembly import AssembledContext
    from engine.context import ExecutionContext
    from engine.framework import Framework, LLMRequest, LLMResponse
    from llm import ProviderConfig
    from providers import (
        AuthorizationProvider,
        ContextCompactionProvider,
        RecentBufferMemoryProvider,
        SandboxProvider,
        SecretsProvider,
    )
    from schemas import ModelSpec, ThreadId
    from skills import SkillsSubsystem
    from store import SessionStore
    from tools import ToolsSubsystem


_log = logging.getLogger("engine")

# Sentinel pushed to the stream queue when the runner finishes.
_QUEUE_END: Any = object()


# spec-068 Part A — span registry type. A "span" is any begin/end
# pair the engine writes to the log (`tool.called` → `tool.result` /
# `tool.failed`; `node.started` → `node.completed` / `node.failed`;
# `prompt.requested` → `prompt.answered` / `prompt.cancelled` in a
# later part). Each begin registers a terminal factory keyed by the
# span id; the exchange's `try/finally` invokes any still-open
# factory and routes the terminal through `_emit`, preserving I1.
# Aliased via typing.TypeAlias-shape at TYPE_CHECKING to avoid a
# runtime import of `Callable` here; the alias is only consulted by
# type-checkers, not by the interpreter.
if TYPE_CHECKING:
    SpanTerminalFactory = Callable[[], tuple[EventType, dict[str, Any], Actor]]


# spec-068 Part B — cross-subsystem span-registry seam. `PromptsImpl`
# lives in the renderer package and never touches engine internals
# directly, but its `WIRE_PROMPT_REQUESTED` write is a span begin that
# must close via the engine's `_emit` on cancel/quit. The exchange
# publishes bound `(register_span, close_span)` callables into this
# ContextVar on `run()` entry; downstream subsystems invoked inside
# the exchange's task (tools → `Prompts.choice`/`text` → PromptsImpl)
# read them from the same contextvars-propagated task context. The
# renderer imports the tiny reader helper below rather than
# `AgentEngine` internals — no import cycle, no coupling to `_Exchange`
# beyond the two callable shapes. `None` when no engine exchange is
# on the stack (headless tests, force-invoke synthetic reads on the
# preflight boundary), and the reader is a no-op.
_ExchangeSpanRegistry = tuple[
    "Callable[[str, SpanTerminalFactory], None]",
    "Callable[[str], None]",
]

_current_exchange_span_registry: contextvars.ContextVar[
    _ExchangeSpanRegistry | None
] = contextvars.ContextVar(
    "_current_exchange_span_registry",
    default=None,
)


def current_span_registry() -> _ExchangeSpanRegistry | None:
    """Return the active exchange's `(register_span, close_span)` pair.

    Subsystems whose begin-event site fires inside an engine exchange
    (today: `PromptsImpl`) read this to plug their span into the
    exchange's registry so the finally-safety-net + resume
    reconciliation cover them symmetrically with observer-driven tool
    spans. Returns `None` outside of an engine exchange (headless
    unit tests of the subsystem in isolation), in which case the
    caller is responsible for its own closure (or the resume path
    picks it up next boot — the whole point of the reconciliation
    invariant).
    """
    return _current_exchange_span_registry.get()


#: Engine version — bump when the public contract changes.
ENGINE_VERSION = "0.1"


class AgentEngine:
    """The platform's Agent Engine — long-lived, framework-agnostic.

    Constructor takes every subsystem the engine collaborates with
    plus the chosen
    [`Framework`][engine.framework.Framework] and an LLM callable.
    The LLM callable is what the engine wraps inside the observed
    LLM handle and hands to the framework — for tests, a Mock-LLM
    adapter; for production, a litellm-backed callable.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        bus: EventBus,
        asset_management: AssetManagement,
        tools_subsystem: ToolsSubsystem,
        skills_subsystem: SkillsSubsystem,
        memory_provider: RecentBufferMemoryProvider,
        secrets: SecretsProvider,
        sandbox: SandboxProvider,
        authz: AuthorizationProvider,
        framework: Framework,
        llm: Callable[[LLMRequest], Awaitable[LLMResponse]] | None = None,
        llm_providers_config: dict[str, ProviderConfig] | None = None,
        llm_for_model: (
            Callable[
                [ModelSpec],
                Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]],
            ]
            | None
        ) = None,
        compaction_provider: ContextCompactionProvider | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._assets = asset_management
        self._tools = tools_subsystem
        self._skills = skills_subsystem
        self._memory = memory_provider
        self._secrets = secrets
        self._sandbox = sandbox
        self._authz = authz
        self._framework = framework
        self._llm = llm
        # Async factory `llm_for_model` lets the fallback orchestrator
        # lazily build per-attempt LLM clients. Tests can wire a
        # `spec → MockLLM` map; production wires `build_llm` via the
        # bootstrap. When the factory is None, fall back to wrapping
        # `self._llm` so every spec returns the same callable —
        # backward-compatible with tests that pass `llm=` directly.
        self._llm_for_model = llm_for_model
        # Pre-flight acquires a `crewai.LLM` via `build_llm` when
        # `self._llm is None`. Tests still pass `llm=` directly to
        # bypass this path; production wires `llm_providers_config`
        # so build_llm has something to look up.
        self._llm_providers_config: dict[str, ProviderConfig] = (
            llm_providers_config or {}
        )
        # feat-054 Phase 1: constructor-only wiring. The trigger check
        # at pre-flight lands in Phase 2. `None` = compaction disabled
        # (platform.yaml has no ``compaction:`` section or the provider
        # factory declined to build one) and the engine will skip the
        # threshold check entirely.
        self._compaction_provider: ContextCompactionProvider | None = (
            compaction_provider
        )
        # The acquired crewai.LLM lives on the exchange instance, NOT
        # the engine — each exchange may target a different model.
        # Per-thread lock map. Each execute call shares the lock so
        # concurrent framework callbacks serialize their event-construct
        # paths. The map persists across calls because engine instances
        # are long-lived (statelessness is about exchange state, not
        # in-process structural state like locks).
        self._thread_locks: dict[str, asyncio.Lock] = {}
        # feat-059: per-engine assembly-projection cache. Keyed by
        # ``(thread_id, leaf_event_id, tool_schemas_hash, respect_compaction,
        # assembly_version)`` so a new event appended to the log naturally
        # invalidates the entry via the ``leaf_event_id`` change. Reads and
        # writes for a given thread are already serialised by
        # ``_lock_for_thread(str(thread_id))``, which every
        # ``assemble_context`` call site holds.
        self._assembly_cache: AssemblyCache = AssemblyCache()
        # bug-097: per-thread idempotency guard for the
        # ``no_context_length`` compaction-skip emit (qol-092). The emit
        # sits inside ``_maybe_compact_events``, which fires per user
        # turn — without a guard, a wedged session (opencode + no
        # declared ``context_length`` + no cross-provider fallback per
        # qol-091) stacks a fresh ``CompactionBlock`` widget per turn
        # of the transcript.
        # Thread-scoped so distinct threads each get their first-turn
        # notice; process-scoped would incorrectly silence resumed
        # sessions. Only the ``no_context_length`` reason is gated —
        # ``no_net_savings`` intentionally repeats per attempt.
        self._no_context_length_skip_emitted: set[str] = set()

    # ---- Public API ----------------------------------------------------

    def capabilities(self) -> EngineCapabilities:
        """Declare what this engine can do.

        feat-054 Phase 2 bumped ``ASSEMBLY_VERSION`` from ``"1"`` to
        ``"2"``. Both are advertised: agent YAMLs shipped in bundles
        still declare ``assembly_version: "1"`` (they don't rely on
        compaction) and the projection is byte-identical when no
        ``CONTEXT_COMPACTED`` events are on the path. Agents that
        explicitly bump to ``"2"`` opt into the compaction projection
        rule.
        """
        return EngineCapabilities(
            engine_version=ENGINE_VERSION,
            assembly_versions=["1", ASSEMBLY_VERSION],
            supported_modes=["single_shot", "conversational"],
            supported_features=["tool_use", "force_invoke"],
            supported_providers=["*"],
        )

    async def execute(
        self,
        config: ResolvedAgentConfig,
        context: ExecutionContext,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> Event:
        """Run one exchange and return the final agent.message event.

        Collects the stream from
        [`execute_stream`][engine.engine.AgentEngine.execute_stream] and
        returns the closing `agent.message`. For callers that need to
        react event-by-event, use `execute_stream` instead. Unlike
        `execute_stream`, this method **raises** on Fatal failure
        rather than yielding a failed event as the last item.

        Args:
            config: The resolved agent configuration for this exchange.
            context: The runtime payload — thread id, trigger,
                optional cancellation token. See
                [`ExecutionContext`][engine.context.ExecutionContext].
            inputs: Single-shot mode inputs, forwarded to the
                framework unchanged. `None` for conversational
                triggers.

        Returns:
            The `agent.message` event closing the exchange.

        Raises:
            AgentExecutionError: Fatal exchange failure. Carries the
                `thread.exchange_failed` event.
            PlatformInternalError: Engine invariant violation —
                should be unreachable.
        """
        agent_message: Event | None = None
        failed_event: Event | None = None
        async for event in self.execute_stream(config, context, inputs=inputs):
            if event.type == EventType.AGENT_MESSAGE:
                agent_message = event
            elif event.type == EventType.THREAD_EXCHANGE_FAILED:
                failed_event = event
        if failed_event is not None:
            raise AgentExecutionError(failed_event)
        if agent_message is None:
            # Should be unreachable: success path always emits both.
            msg = "execute completed without an agent.message or failed event"
            raise PlatformInternalError(
                original_type="engine.invariant_violation",
                args=[msg],
            )
        return agent_message

    async def execute_stream(
        self,
        config: ResolvedAgentConfig,
        context: ExecutionContext,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> AsyncIterator[Event]:
        """Yield each event as it is emitted, including failure events.

        Unlike [`execute`][engine.engine.AgentEngine.execute], this method
        does NOT raise on Fatal failure: the failed event is the
        final yielded item, and the iterator ends after it. Callers
        that prefer a `try/except` shape should use `execute`
        instead.

        Args:
            config: The resolved agent configuration for this
                exchange.
            context: The runtime payload — thread id, trigger,
                optional cancellation token.
            inputs: Single-shot mode inputs, forwarded to the
                framework unchanged. `None` for conversational
                triggers.

        Yields:
            Each [`Event`][schemas.Event] the engine emits during the
            exchange, in append order.
        """
        queue: asyncio.Queue[Event | object] = asyncio.Queue()
        runner = asyncio.create_task(
            self._run_exchange(config, context, inputs or {}, queue),
        )
        try:
            while True:
                item = await queue.get()
                if item is _QUEUE_END:
                    break
                # Items in the queue are always Events when not the
                # sentinel; the runner is the sole producer and only
                # puts Events.
                if isinstance(item, Event):
                    yield item
        finally:
            if not runner.done():
                runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner

    # ---- Driver -------------------------------------------------------

    async def _run_exchange(
        self,
        config: ResolvedAgentConfig,
        context: ExecutionContext,
        inputs: dict[str, Any],
        queue: asyncio.Queue[Event | object],
    ) -> None:
        ex = _Exchange(
            engine=self,
            config=config,
            context=context,
            inputs=inputs,
            queue=queue,
        )
        try:
            await ex.run()
        except Exception as exc:  # noqa: BLE001 — engine boundary safety net
            _log.exception("engine internal error in _run_exchange: %s", exc)
            # Try to emit a thread.exchange_failed with the safety-net
            # error if we can; otherwise just close the queue.
            try:
                err_obj = (
                    exc
                    if isinstance(exc, PlatformError)
                    else PlatformInternalError.from_exception(exc)
                )
                await ex.emit_exchange_failed(err_obj)
            except Exception:  # noqa: BLE001 — secondary failure
                _log.exception("failed to emit exchange_failed safety event")
        finally:
            # spec-068 Part A — close any still-open spans BEFORE
            # `_QUEUE_END` so their terminals stream to consumers as
            # part of this exchange, and go through `_emit` (I1 sole
            # emitter). Best-effort: `close_all_open_spans` catches
            # its own emit failures. The primary owner of each span
            # is its emit site (e.g. `_ObservedToolHandle`'s
            # `try/finally`); this is the belt to those suspenders.
            try:
                await ex.close_all_open_spans()
            except Exception:  # noqa: BLE001 — finally safety net
                _log.exception("close_all_open_spans safety net raised")
            queue.put_nowait(_QUEUE_END)

    # ---- Internal helpers used by _Exchange ---------------------------

    def _lock_for_thread(self, thread_id: str) -> asyncio.Lock:
        lock = self._thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[thread_id] = lock
        return lock

    def _assemble_context_cached(
        self,
        events: list[Event],
        *,
        tool_schemas: list[dict[str, Any]] | None = None,
        respect_compaction: bool = True,
    ) -> AssembledContext:
        """Cached wrapper over :func:`engine.assembly.assemble_context`.

        feat-059: memoises the pure projection on
        ``(thread_id, leaf_event_id, tool_schemas_hash, respect_compaction,
        assembly_version)``. A new event appended to the thread changes the
        leaf id, so the next call misses and re-projects with the fresh
        path — invalidation is automatic. Empty ``events`` bypasses the
        cache: no leaf means no stable key, and this is the trivial
        no-history path a fresh session takes exactly once.

        Callers hold ``_lock_for_thread(str(thread_id))``, so cache
        reads/writes for a given thread are already serialised. The
        cache's own outer lock only guards the first insert for a
        previously-unseen thread.
        """
        key: _Key | None
        if events:
            thread_id = events[-1].thread_id
            leaf_event_id = events[-1].event_id
            key = _Key(
                thread_id=thread_id,
                leaf_event_id=leaf_event_id,
                tool_schemas_hash=hash_tool_schemas(tool_schemas),
                respect_compaction=respect_compaction,
                assembly_version=ASSEMBLY_VERSION,
            )
            cached = self._assembly_cache.get(key)
            if cached is not None:
                _log.debug(
                    "assembly_cache: %s thread=%s leaf=%s",
                    "hit",
                    thread_id,
                    leaf_event_id,
                )
                return cached
            _log.debug(
                "assembly_cache: %s thread=%s leaf=%s",
                "miss",
                thread_id,
                leaf_event_id,
            )
        else:
            # Trivial no-history case: no leaf, no stable cache key.
            # Delegate to the pure function below and skip caching.
            key = None

        value = assemble_context(
            events,
            tool_schemas=tool_schemas,
            respect_compaction=respect_compaction,
        )
        if key is not None:
            self._assembly_cache.put(key, value)
        return value

    async def manual_compact(
        self,
        *,
        config: ResolvedAgentConfig,
        thread_id: ThreadId,
        preview: bool = False,
        extra_instructions: str | None = None,
    ) -> Event | None:
        """Force a compaction on ``thread_id`` regardless of the trigger threshold.

        feat-054 Phase 3 — powers the ``/compact`` slash command. Runs
        the same summariser path as
        :meth:`_Exchange._maybe_compact_events` but bypasses the
        pre-flight token estimate check so the user can compact NOW.
        Emits a
        `CONTEXT_COMPACTED` event
        with ``trigger="manual"`` (or ``"manual_preview"`` when
        ``preview=True``) so downstream telemetry can tell a user-
        initiated compaction apart from an auto-fired one.

        Returns the emitted event on the happy path (``CONTEXT_COMPACTED``)
        OR — post-bug-098 — a ``CONTEXT_COMPACTION_SKIPPED`` event with
        ``reason="no_context_length"`` when the resolved model has no
        declared ``context_length`` (deliberately unguarded: each
        user-initiated ``/compact`` deserves a fresh notice). Returns
        ``None`` only when the middle span was empty (short session) or
        the provider is not wired.

        Raises the summariser exception on failure. The caller
        (``ChatApp._handle_compact``) mounts a notice with the error;
        the failure ladder in Phase 4 will retry.
        """
        # feat-054 Phase 4 — thread-safety: serialise the read-then-emit
        # of a manual compaction against any concurrent in-flight
        # exchange on the same thread. Without the lock, an exchange's
        # emit slotting between our `read_path_to_leaf` and
        # `append_event` would produce a compaction whose
        # `compacted_event_ids` reference a stale leaf — Assembly would
        # still project them (they're valid ids on the path) but the
        # user-visible span would exclude events emitted during the
        # window, leaving a jagged "compaction covers past me, not
        # around me" state. Same lock the exchange uses.
        async with self._lock_for_thread(str(thread_id)):
            return await self._manual_compact_locked(
                config=config,
                thread_id=thread_id,
                preview=preview,
                extra_instructions=extra_instructions,
            )

    async def _manual_compact_locked(
        self,
        *,
        config: ResolvedAgentConfig,
        thread_id: ThreadId,
        preview: bool,
        extra_instructions: str | None,
    ) -> Event | None:
        """Body of :meth:`manual_compact`, invoked inside the thread lock."""
        provider = self._compaction_provider
        if provider is None:
            return None
        context_length = _context_length_for_spec(
            self._llm_providers_config,
            config.model,
        )
        if context_length is None:
            # bug-098 — emit the distinct ``no_context_length`` skip
            # event so the ``/compact`` slash handler's opaque
            # three-way notice is superseded by the event adapter's
            # remediation-hint-bearing ``CompactionBlock``. Unguarded
            # (user-initiated action deserves a fresh response each
            # time; the pending ``CompactionBlock`` mounted by the
            # slash handler is consumed via
            # ``_acquire_compaction_block``).
            return await self._emit_no_context_length_skip_event(
                thread_id=thread_id,
                spec=config.model,
            )
        events = [e async for e in self._store.read_path_to_leaf(thread_id)]
        compaction_cfg = (self._assets._platform_config or {}).get(
            "compaction",
            {},
        ) or {}
        max_output = int(
            compaction_cfg.get("config", {}).get("max_output_tokens", 4096)
        )
        budget = max(context_length - max_output, 1)
        keep_recent_fraction = float(
            compaction_cfg.get("config", {}).get("keep_recent_fraction", 0.20)
        )
        keep_tail_tokens = max(int(budget * keep_recent_fraction), 1)
        inner_config: dict[str, Any] = dict(compaction_cfg.get("config", {}) or {})
        # Route the manual-invocation markers through the provider's
        # config dict so the summariser prompt + emitted event both
        # reflect user intent.
        inner_config["trigger"] = "manual_preview" if preview else "manual"
        if preview:
            inner_config["preview"] = True
        if extra_instructions:
            inner_config["extra_instructions"] = extra_instructions

        outcome = await provider.compact(
            events=events,
            keep_head=0,
            keep_tail=keep_tail_tokens,
            model_context_length=context_length,
            thread_id=thread_id,
            config=inner_config,
        )
        if not outcome.compacted_event_ids:
            return None

        # Manual compaction is not exchange-scoped. Emit through the
        # store directly with a system Actor and a per-thread event.
        thread = await self._store.get_thread(thread_id)
        emitted = Event(
            event_id=EventId(f"ev_{uuid4().hex}"),
            session_id=thread.session_id,
            thread_id=thread_id,
            parent_event_id=None,
            created_at=datetime.now(UTC),
            type=EventType.CONTEXT_COMPACTED,
            data=outcome.model_dump(mode="json"),
            actor=Actor(kind="system"),
        )
        stored = await self._store.append_event(thread_id, emitted)
        # Publish so live subscribers (chat event adapter) see it.
        try:
            await self._bus.publish(stored)
        except Exception:  # noqa: BLE001
            _log.exception("manual_compact: bus publish failed")
        return stored

    async def maybe_compact_thread_eagerly(
        self,
        *,
        thread_id: ThreadId,
        spec: ModelSpec,
        tool_schemas: list[dict[str, Any]] | None = None,
        trigger: Literal["auto_resume"] = "auto_resume",
    ) -> Event | None:
        """Check ``thread_id`` for over-threshold context and compact if so.

        feat-054 Phase 5 — session-open eager compaction. Safe to call
        BEFORE any exchange begins (e.g. from ``ChatApp.on_mount``).
        Shifts the summariser delay out of the user's first post-resume
        message and into the resume UX itself.

        Runs the same 3-tier failure ladder as
        :meth:`_Exchange._maybe_compact_events`:

        1. Tier 1 — retry summariser once.
        2. Tier 2 — mechanical truncation fallback (emits
           ``CONTEXT_COMPACTION_FAILED`` + ``CONTEXT_COMPACTED`` with
           ``summary.trigger="auto_truncation"``).
        3. Tier 3 — raise ``ContextOverflowUncompactable`` when even
           mechanical truncation cannot bring the projection below
           the model's budget.

        ``trigger`` marks the emitted event's ``trigger`` field so
        downstream telemetry can distinguish resume-time compaction
        from per-exchange compaction.

        Returns the emitted event on success (``CONTEXT_COMPACTED``) OR
        — post-bug-098 — a ``CONTEXT_COMPACTION_SKIPPED`` with
        ``reason="no_context_length"`` when the resolved model has no
        declared ``context_length`` AND the thread has history at risk
        (guarded by bug-097's per-thread set to prevent double-render on
        session-open). Returns ``None`` when compaction was NOT needed
        (thread under threshold, provider not wired, or fresh session
        with no events).
        """
        # Serialise against a concurrent in-flight exchange OR a
        # concurrent /compact call on the same thread — same lock
        # both other paths acquire.
        async with self._lock_for_thread(str(thread_id)):
            return await self._eager_compact_locked(
                thread_id=thread_id,
                spec=spec,
                tool_schemas=tool_schemas or [],
                trigger=trigger,
            )

    async def _eager_compact_locked(
        self,
        *,
        thread_id: ThreadId,
        spec: ModelSpec,
        tool_schemas: list[dict[str, Any]],
        trigger: str,
    ) -> Event | None:
        """Body of :meth:`maybe_compact_thread_eagerly`, run under the thread lock."""
        provider = self._compaction_provider
        if provider is None:
            return None
        # bug-098 review F2 + qol-100: gate the wedged-provider notice
        # on real conversation history (``USER_MESSAGE`` — bootstrap
        # events like ``SESSION_OPENED`` / ``CONFIG_SET`` are always
        # present and don't count as "history the user has invested
        # in preserving"). qol-100 replaces the full ``read_path_to_leaf``
        # + ``any(...)`` with the cheap ``has_any_event_of_type`` probe
        # so healthy providers don't pay a full-log read at chat-open
        # just to answer a boolean.
        has_user_activity = await self._store.has_any_event_of_type(
            thread_id, EventType.USER_MESSAGE
        )
        if not has_user_activity:
            # Fresh session — nothing to compact and nothing to warn
            # about. Fast no-op path so a brand-new chat doesn't
            # spuriously call the summariser OR mount the wedged notice.
            return None
        context_length = _context_length_for_spec(
            self._llm_providers_config,
            spec,
        )
        if context_length is None:
            # bug-098 — emit the distinct ``no_context_length`` skip
            # event (engine-initiated eager check, shares bug-097's
            # per-thread guard so a session that already got the
            # notice from a per-turn or ``/compact`` emit doesn't
            # double-render on session-open).
            if str(thread_id) in self._no_context_length_skip_emitted:
                return None
            emitted = await self._emit_no_context_length_skip_event(
                thread_id=thread_id,
                spec=spec,
            )
            self._no_context_length_skip_emitted.add(str(thread_id))
            return emitted

        # Healthy provider with user history — the summariser + estimate
        # paths need the full events list. Deferred to after the cheap
        # presence + wedged checks so wedged sessions bail without paying
        # the full-log read (qol-100).
        events = [e async for e in self._store.read_path_to_leaf(thread_id)]
        compaction_cfg = (self._assets._platform_config or {}).get(
            "compaction",
            {},
        ) or {}
        # Estimate current projection tokens using the same tiktoken
        # helper the per-exchange trigger uses.
        preview = self._assemble_context_cached(
            events,
            tool_schemas=tool_schemas,
            respect_compaction=True,
        )
        from providers.compaction import (  # noqa: PLC0415
            build_mechanical_truncation_payload,
            estimate_tokens_for_messages,
        )

        preview_msgs = [m.model_dump() for m in preview.messages]
        estimate = estimate_tokens_for_messages(preview_msgs, preview.tools)

        max_output = int(
            compaction_cfg.get("config", {}).get("max_output_tokens", 4096)
        )
        budget = max(context_length - max_output, 1)
        trigger_at = float(compaction_cfg.get("trigger_at", 0.80))
        if estimate < budget * trigger_at:
            # Under threshold — no compaction needed.
            return None

        base_keep_recent_fraction = float(
            compaction_cfg.get("config", {}).get("keep_recent_fraction", 0.20)
        )
        keep_fractions: list[float] = [
            base_keep_recent_fraction,
            base_keep_recent_fraction * 0.5,
            base_keep_recent_fraction * 0.25,
        ]
        last_error: Exception | None = None
        for attempt_index, kf in enumerate(keep_fractions):
            keep_tail_tokens = max(int(budget * kf), 1)
            inner_config: dict[str, Any] = dict(compaction_cfg.get("config", {}) or {})
            inner_config["keep_recent_fraction"] = kf
            # Propagate the requested trigger through the provider so
            # the emitted ContextCompactedData.trigger reflects reality.
            inner_config["trigger"] = trigger
            outcome: Any = None
            attempt_error: Exception | None = None
            for retry in range(2):
                try:
                    outcome = await provider.compact(
                        events=events,
                        keep_head=0,
                        keep_tail=keep_tail_tokens,
                        model_context_length=context_length,
                        thread_id=thread_id,
                        config=inner_config,
                    )
                except Exception as exc:  # noqa: BLE001 — ladder catches any raise
                    attempt_error = exc
                    _log.warning(
                        "compaction (eager): summariser attempt %d/2 failed (%s)",
                        retry + 1,
                        exc,
                    )
                    outcome = None
                    continue
                break
            if outcome is None:
                last_error = attempt_error
                break
            if not outcome.compacted_event_ids:
                # No middle to summarise (short session) — nothing to do.
                return None
            if outcome.tokens_before > 0 and (
                outcome.tokens_after > outcome.tokens_before * 0.90
            ):
                await self._emit_thread_event(
                    thread_id=thread_id,
                    event_type=EventType.CONTEXT_COMPACTION_SKIPPED,
                    data={
                        "reason": "no_net_savings",
                        "tokens_before": outcome.tokens_before,
                        "tokens_after": outcome.tokens_after,
                        "keep_recent_fraction": kf,
                        "attempt": attempt_index + 1,
                    },
                )
                if attempt_index + 1 < len(keep_fractions):
                    continue
                last_error = RuntimeError(
                    "compaction produced no net token savings on any retry"
                )
                break
            # Success — emit the compacted event.
            return await self._emit_thread_event(
                thread_id=thread_id,
                event_type=EventType.CONTEXT_COMPACTED,
                data=outcome.model_dump(mode="json"),
            )

        # Tier 1 exhausted — emit CONTEXT_COMPACTION_FAILED then attempt
        # tier 2 mechanical truncation.
        await self._emit_thread_event(
            thread_id=thread_id,
            event_type=EventType.CONTEXT_COMPACTION_FAILED,
            data={
                "error": {
                    "type": (
                        type(last_error).__name__
                        if last_error is not None
                        else "unknown"
                    ),
                    "message": (
                        str(last_error)
                        if last_error is not None
                        else "summariser exhausted retries"
                    ),
                    "where": "engine.compaction.maybe_compact_thread_eagerly",
                    "detail": {},
                },
                "span_first_event_id": None,
                "span_last_event_id": None,
            },
        )

        keep_tail_tokens = max(int(budget * base_keep_recent_fraction), 1)
        mechanical = build_mechanical_truncation_payload(
            events=events,
            keep_tail_tokens=keep_tail_tokens,
        )
        if mechanical is None:
            from errors import ContextOverflowUncompactable  # noqa: PLC0415

            raise ContextOverflowUncompactable(
                thread_id=str(thread_id),
                keep_tail_tokens=keep_tail_tokens,
                budget_tokens=budget,
            )

        return await self._emit_thread_event(
            thread_id=thread_id,
            event_type=EventType.CONTEXT_COMPACTED,
            data=mechanical.model_dump(mode="json"),
        )

    async def _emit_no_context_length_skip_event(
        self,
        *,
        thread_id: ThreadId,
        spec: ModelSpec,
    ) -> Event:
        """Engine-side emit of the ``no_context_length`` compaction-skip event.

        bug-098 — sibling to the exchange-side emit at
        ``_maybe_compact_events`` (:~2470). Extracted so both
        ``_manual_compact_locked`` (``/compact`` slash command) and
        ``_eager_compact_locked`` (session-open eager path) emit the
        same shape as the per-turn site — model spec + operator-facing
        remediation hint in ``detail``, ``system`` actor.

        Unguarded — callers decide whether to consult the bug-097
        per-thread guard (``_no_context_length_skip_emitted``). The
        ``/compact`` path deliberately does NOT gate: each user
        invocation of ``/compact`` deserves a fresh distinct notice
        (with a new pending ``CompactionBlock`` consumed via
        ``_acquire_compaction_block``). The eager path does gate for
        symmetry with the per-turn cadence.
        """
        return await self._emit_thread_event(
            thread_id=thread_id,
            event_type=EventType.CONTEXT_COMPACTION_SKIPPED,
            data={
                "reason": "no_context_length",
                "tokens_before": 0,
                "tokens_after": 0,
                "detail": {
                    "provider": spec.provider,
                    "model": spec.model,
                    "remediation_hint": _no_context_length_remediation(
                        self._llm_providers_config, spec
                    ),
                },
            },
        )

    async def _emit_thread_event(
        self,
        *,
        thread_id: ThreadId,
        event_type: EventType,
        data: dict[str, Any],
    ) -> Event:
        """Append + publish a system-actor event on ``thread_id``.

        Shared by :meth:`maybe_compact_thread_eagerly` — mirrors the
        emit shape used by :meth:`_manual_compact_locked` (system actor,
        no ``parent_event_id`` override; the store computes the parent
        from the current leaf).
        """
        thread = await self._store.get_thread(thread_id)
        emitted = Event(
            event_id=EventId(f"ev_{uuid4().hex}"),
            session_id=thread.session_id,
            thread_id=thread_id,
            parent_event_id=None,
            created_at=datetime.now(UTC),
            type=event_type,
            data=data,
            actor=Actor(kind="system"),
        )
        stored = await self._store.append_event(thread_id, emitted)
        try:
            await self._bus.publish(stored)
        except Exception:  # noqa: BLE001 — bus publish best-effort
            _log.exception("eager compaction: bus publish failed for %s", event_type)
        return stored

    async def manual_uncompact(
        self,
        *,
        thread_id: ThreadId,
    ) -> Event | None:
        """Append a ``CONTEXT_COMPACTION_REVOKED`` for the newest active compaction.

        feat-054 Phase 3 — powers the ``/uncompact`` slash command.
        Walks the thread from newest to oldest, finds the first
        ``CONTEXT_COMPACTED`` event that has no ``revoked_by`` field
        set (i.e. is still active), and emits a revocation pointing at
        it. Coordinator decision #2: LIFO first-only. Returns the
        emitted revocation event, or ``None`` when no active
        compaction exists on the thread.

        Assembly (Phase 2) already skips revoked compactions when
        projecting, so the next exchange sees the restored context.
        Does NOT restart the current exchange.
        """
        # feat-054 Phase 4 — thread-safety, mirrors :meth:`manual_compact`.
        async with self._lock_for_thread(str(thread_id)):
            return await self._manual_uncompact_locked(thread_id=thread_id)

    async def _manual_uncompact_locked(
        self,
        *,
        thread_id: ThreadId,
    ) -> Event | None:
        """Body of :meth:`manual_uncompact`, invoked inside the thread lock."""
        events = [e async for e in self._store.read_path_to_leaf(thread_id)]
        # Index every CONTEXT_COMPACTION_REVOKED so a prior /uncompact
        # is honoured — the revocation is emitted as a separate event
        # (immutable log), NOT by mutating the compaction's payload,
        # so an in-payload `revoked_by` check alone would miss it.
        revoked_ids: set[str] = set()
        for e in events:
            if e.type != EventType.CONTEXT_COMPACTION_REVOKED:
                continue
            revoked_target = e.data.get("compaction_event_id")
            if isinstance(revoked_target, str) and revoked_target:
                revoked_ids.add(revoked_target)
        target: Event | None = None
        for e in reversed(events):
            if e.type != EventType.CONTEXT_COMPACTED:
                continue
            if str(e.event_id) in revoked_ids:
                continue
            # `revoked_by` on the payload (belt + braces for future
            # variants that DO stamp the pointer inline).
            revoked_by = e.data.get("revoked_by")
            if revoked_by:
                continue
            target = e
            break
        if target is None:
            return None
        thread = await self._store.get_thread(thread_id)
        emitted = Event(
            event_id=EventId(f"ev_{uuid4().hex}"),
            session_id=thread.session_id,
            thread_id=thread_id,
            parent_event_id=None,
            created_at=datetime.now(UTC),
            type=EventType.CONTEXT_COMPACTION_REVOKED,
            data={
                "compaction_event_id": str(target.event_id),
                "reason": "user_requested",
            },
            actor=Actor(kind="system"),
        )
        stored = await self._store.append_event(thread_id, emitted)
        try:
            await self._bus.publish(stored)
        except Exception:  # noqa: BLE001
            _log.exception("manual_uncompact: bus publish failed")
        return stored


# ---------------------------------------------------------------------------
# _Exchange — per-execute state holder
# ---------------------------------------------------------------------------


class _Exchange:
    """Owns the state of one execute call.

    Not reusable; a fresh instance per execute. The engine instance
    itself is durable across calls — statelessness is about
    *exchange* state, not the engine object.
    """

    def __init__(
        self,
        *,
        engine: AgentEngine,
        config: ResolvedAgentConfig,
        context: ExecutionContext,
        inputs: dict[str, Any],
        queue: asyncio.Queue[Event | object],
    ) -> None:
        self.engine = engine
        self.config = config
        self.context = context
        self.inputs = inputs
        self.queue = queue
        # Identifiers filled in pre-flight step 4.
        self.thread_id = str(context.thread_id)
        self.session_id: str = ""
        self.agent_id: str = str(config.agent_id)
        # Wired in pre-flight step 6 — see _acquire_llm_if_needed.
        self.acquired_llm: crewai.LLM | None = None
        # Wired in pre-flight step 7 — Phase 1.3 fallback chain.
        self.fallback_chain: list[ModelSpec] = []
        # Wired in pre-flight step 8.
        self.config_event_id: str | None = None
        # Wired in pre-flight step 10.
        self.exchange_started_id: str | None = None
        # Wired in pre-flight step 11.
        self.user_message_event_id: str | None = None
        # Cancellation token — engine-internal default if caller omitted.
        self.cancel = context.cancellation or CancelToken()
        # Per-thread lock — shared across emits in this exchange.
        self._lock = engine._lock_for_thread(self.thread_id)
        # spec-068 Part A — span registry. Ordered by insertion so the
        # exchange finally closes newest-first (mirroring stack-unwind
        # order). Values are zero-arg factories that produce the
        # terminal event's `(EventType, data, actor)` tuple; the
        # exchange finally invokes them and routes each through
        # `_emit`, preserving I1 sole-emitter. Primary owner of the
        # closure is the emit site itself (observer's try/finally);
        # this registry is the belt to that suspenders — if any span
        # begin ever slips past its own finally, the exchange finally
        # still guarantees a terminal.
        self._open_spans: OrderedDict[str, SpanTerminalFactory] = OrderedDict()

    # ---- Span registry (spec-068 Part A) ------------------------------

    def register_span(
        self,
        span_id: str,
        terminal_factory: SpanTerminalFactory,
    ) -> None:
        """Record an open span that must be closed by a paired terminal.

        Called by the emit site immediately AFTER its begin event
        (`tool.called`, `node.started`, etc.) has been appended to the
        log. The factory produces the terminal event's
        ``(EventType, data, actor)`` tuple; the exchange finally
        invokes any still-registered factory and routes each terminal
        through ``_emit`` so I1 sole-emitter holds even on the
        cancellation / crash path. Re-registering an id overwrites —
        the last registration wins (used by pathological retries; the
        common case is one register + one close per id).
        """
        self._open_spans[span_id] = terminal_factory

    def close_span(self, span_id: str) -> None:
        """Mark a span closed. No-op when the id was never registered.

        Called by the emit site immediately AFTER its terminal event
        has been appended (successfully or on any error arm). Idempotent
        — the exchange finally's ``close_all_open_spans`` skips ids
        that are no longer in the registry.
        """
        self._open_spans.pop(span_id, None)

    async def close_all_open_spans(self) -> None:
        """Close every still-open span by emitting its terminal.

        Runs from the exchange's outer ``try/finally`` on normal
        completion, error, or cancel-bubble. Each terminal is routed
        through ``_emit`` so the sole-emitter invariant (I1) holds and
        the event is appended, published on the bus, and pushed to the
        stream queue in the same shape as any other engine emit.

        Emission is best-effort per span: an individual emit failure
        is logged but does not prevent subsequent spans from closing.
        The registry is drained regardless — a second call to this
        method is a no-op.
        """
        # Snapshot + clear so a concurrent close_span (e.g. from a
        # racing observer arm still in flight) can't double-close.
        factories = list(self._open_spans.items())
        self._open_spans.clear()
        for span_id, factory in factories:
            try:
                type_, data, actor = factory()
            except Exception:  # noqa: BLE001 — factory is untrusted
                _log.exception(
                    "close_all_open_spans: terminal factory for span %s raised",
                    span_id,
                )
                continue
            try:
                await self._emit(type_=type_, data=data, actor=actor)
            except Exception:  # noqa: BLE001 — emit is best-effort in finally
                _log.exception(
                    "close_all_open_spans: emit terminal for span %s failed",
                    span_id,
                )

    # ---- Resume reconciliation (spec-068 Part B) ----------------------

    async def _reconcile_open_spans_on_resume(self) -> None:
        """Close any span begin on the leaf path whose terminal never landed.

        The append-only log is authoritative; the projector does not
        repair it at read time (Store §5.4). If a prior process wrote
        `tool.called` or `prompt.requested` and died / was cancelled
        before appending the paired terminal, every subsequent
        assembly re-projects the orphan and the provider 400s the
        session ("tool_call_ids did not have response messages").

        This method walks the current leaf path once, indexes span
        begins → terminals by span id (`tool_call_id`, `prompt_id`),
        and appends a synthetic terminal via `_emit` for each
        unpaired begin — the SAME sole-emitter seam every engine-
        authored event goes through, preserving I1. Span-keyed (not
        tail-windowed): a begin 5000 events deep is still closed.
        Idempotent: after this method appends the terminal, the next
        boot's scan sees the pair and does nothing.

        Terminal shapes are byte-identical to what the observer's
        cancel arm emits for tools (`_build_session_interrupted_tool_
        failed` in engine.observers) and what the (now-deleted)
        renderer synth wrote for prompts (`reason="session_
        interrupted"`). Actor is `system` — engine-authored on behalf
        of the dead exchange, not model-attributable.

        Runs from `_run_framework` BEFORE the first assembly read and
        BEFORE the live-bus subscription in `ChatApp` has replayed
        anything through widgets. A prompt injected on the live bus
        AFTER replay lands as a fresh begin on future paths and is
        picked up by the observer/PromptsImpl span registration
        during its own exchange — reconciliation never touches it.
        """
        from schemas.wire_kinds import (  # noqa: PLC0415
            WIRE_PROMPT_ANSWERED,
            WIRE_PROMPT_CANCELLED,
            WIRE_PROMPT_REQUESTED,
        )

        events = [
            e
            async for e in self.engine._store.read_path_to_leaf(
                self.context.thread_id,
            )
        ]

        # Index begins and terminals in one pass. `tool_call_id` and
        # `prompt_id` are disjoint namespaces (different generators,
        # different prefixes), so a single dict per class is safe.
        tool_calls: OrderedDict[str, dict[str, Any]] = OrderedDict()
        tool_terminals: set[str] = set()
        prompt_requests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        prompt_terminals: set[str] = set()
        for evt in events:
            data = evt.data if isinstance(evt.data, dict) else {}
            if evt.type == EventType.TOOL_CALLED:
                call_id = data.get("tool_call_id")
                if isinstance(call_id, str):
                    tool_calls[call_id] = data
            elif evt.type in (EventType.TOOL_RESULT, EventType.TOOL_FAILED):
                call_id = data.get("tool_call_id")
                if isinstance(call_id, str):
                    tool_terminals.add(call_id)
            elif evt.type == EventType.UI_EVENT:
                kind = data.get("kind")
                payload = data.get("payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                prompt_id = payload.get("prompt_id")
                if not isinstance(prompt_id, str) or not prompt_id:
                    continue
                if kind == WIRE_PROMPT_REQUESTED:
                    prompt_requests[prompt_id] = payload
                elif kind in (WIRE_PROMPT_ANSWERED, WIRE_PROMPT_CANCELLED):
                    prompt_terminals.add(prompt_id)

        # Emit synthetic terminals for every unpaired begin. Order
        # begins-of-appearance so downstream consumers see a stable
        # sequence across boots. Tool terminals use the observer's
        # central factory — one source of truth for the payload
        # shape. Prompt terminals mirror the exact `WIRE_PROMPT_
        # CANCELLED` payload the (now-deleted) renderer synth wrote.
        from engine.observers import (  # noqa: PLC0415
            _build_session_interrupted_tool_failed,
        )

        for call_id, called_data in tool_calls.items():
            if call_id in tool_terminals:
                continue
            tool_name = called_data.get("tool_name")
            if not isinstance(tool_name, str):
                tool_name = "<unknown>"
            type_, terminal_data, actor = _build_session_interrupted_tool_failed(
                tool_call_id=call_id,
                tool_name=tool_name,
            )
            try:
                await self._emit(type_=type_, data=terminal_data, actor=actor)
            except Exception:  # noqa: BLE001 — best-effort per span
                _log.exception(
                    "resume reconciliation: emit tool.failed for %s failed",
                    call_id,
                )

        for prompt_id in prompt_requests:
            if prompt_id in prompt_terminals:
                continue
            terminal_data = {
                "schema_version": 1,
                "kind": WIRE_PROMPT_CANCELLED,
                "payload": {
                    "prompt_id": prompt_id,
                    "reason": "session_interrupted",
                },
            }
            try:
                await self._emit(
                    type_=EventType.UI_EVENT,
                    data=terminal_data,
                    actor=Actor(kind="system"),
                )
            except Exception:  # noqa: BLE001 — best-effort per span
                _log.exception(
                    "resume reconciliation: emit prompt.cancelled for %s failed",
                    prompt_id,
                )

    # ---- The orchestrator ---------------------------------------------

    async def run(self) -> None:
        # spec-068 Part B — publish this exchange's span registry into
        # the task-local ContextVar so subsystems whose begin-event
        # site fires deep inside the tool call chain (today:
        # PromptsImpl, which writes WIRE_PROMPT_REQUESTED durably from
        # the renderer package with no direct engine handle) can
        # register their span with our finally-safety-net + resume
        # reconciliation. The token is reset in the outer finally so
        # a follow-on exchange on the same task never sees stale
        # callables. contextvars.Token semantics guarantee reset
        # happens even on cancellation.
        span_registry_token = _current_exchange_span_registry.set(
            (self.register_span, self.close_span),
        )
        try:
            await self._run_body()
        finally:
            _current_exchange_span_registry.reset(span_registry_token)

    async def _run_body(self) -> None:
        try:
            await self._preflight()
        except PlatformError as exc:
            # Pre-flight Fatal — emit thread.exchange_failed iff we got
            # far enough to know the session_id. If not, the caller
            # gets an empty stream and the wrapper at the boundary
            # raises.
            if self.session_id and self.exchange_started_id is None:
                # Authz / capability denial — the engine contract
                # requires emitting the failed event even without an
                # exchange_started.
                await self.emit_exchange_failed(exc, allow_no_started=True)
            elif self.exchange_started_id is not None:
                await self.emit_exchange_failed(exc)
            return

        # Pre-flight succeeded — set up the framework + drive the loop.
        try:
            agent_text = await self._run_framework()
        except PlatformError as exc:
            await self.emit_exchange_failed(exc)
            return
        except Exception as exc:  # noqa: BLE001 — engine boundary
            # Safety-net cause-chain inspection before falling back to
            # PlatformInternalError. The primary translation lives in
            # the framework adapter; this catches anything that
            # slipped past (future framework releases, paths the
            # adapter doesn't yet cover). When a known typed
            # exception appears in `__cause__`, re-classify so the
            # user sees the right surface (e.g. model.upstream_error)
            # instead of an opaque "internal platform error".
            from engine._classify import classify_from_cause_chain  # noqa: PLC0415

            classified = classify_from_cause_chain(exc, agent_id=self.agent_id)
            if classified is not None:
                await self.emit_exchange_failed(classified)
                return
            wrapped = PlatformInternalError.from_exception(exc)
            await self.emit_exchange_failed(wrapped)
            return

        await self._finalize(agent_text)

    # ---- Pre-flight ---------------------------------------------------

    async def _preflight(self) -> None:
        # Step 1 — validate the resolved config. The kernel did this;
        # defense in depth: a Pydantic re-validate. If config is not a
        # ResolvedAgentConfig (Pydantic instance), construct one to
        # raise ValidationError.
        ResolvedAgentConfig.model_validate(self.config.model_dump())

        # Step 2 — capability check.
        caps = self.engine.capabilities()
        required = [self.config.assembly_version]
        missing = sorted(set(required) - set(caps.assembly_versions))
        if missing:
            raise CapabilityMismatch(
                required=required,
                available=caps.assembly_versions,
            )

        # Step 3 — authorization. principal = user.local in v1.
        principal = "user.local"
        action = f"agent.execute.{self.agent_id}"
        # Look up thread/session early so we have session_id when raising.
        thread = await self.engine._store.get_thread(self.context.thread_id)
        self.session_id = str(thread.session_id)

        # Pass the proximate-first caller_chain so workflow-spawned
        # invocations surface the workflow→agent frame in the authz
        # audit decision. Chat / API surfaces leave the chain empty
        # and existing behaviour is unchanged.
        auth = await self.engine._authz.allow(
            principal,
            action,
            caller_chain=self.context.caller_chain,
        )
        if not auth.allowed:
            raise AgentUnauthorized(
                agent_id=self.agent_id,
                action=action,
                principal=principal,
                matched_rule=(
                    auth.matched_rule.model_dump() if auth.matched_rule else None
                ),
            )

        # Step 4 — open session (already done at create_session time)
        # + compute the thread's current leaf for parent_event_id
        # wiring. `SessionStore.get_leaf` is called per-emit; nothing
        # to do here beyond storing the session_id (already done).

        # Step 5 — bind memory context, IF the agent has a memory
        # subsystem configured. The wired result lives on the
        # exchange for tool-handler / future use.
        memory_sub = self.config.subsystems.get("memory")
        self.memory_ctx = None
        if memory_sub is not None:
            # PlatformError from bind_session bubbles as Fatal — the
            # engine cannot run an exchange whose config promises
            # memory access without that access.
            self.memory_ctx = await self.engine._memory.bind_session(
                session_id=self.session_id,  # type: ignore[arg-type]
                agent_id=self.agent_id,  # type: ignore[arg-type]
                config=memory_sub.config,
            )

        # Step 6 — acquire LLM client for the primary model. When the
        # engine was constructed without an `llm` callable, build one
        # from this exchange's ModelSpec via `build_llm`. Tests inject
        # `llm=` directly and skip this seam; production wires
        # `llm_providers_config` so the lookup has something to
        # resolve. The crewai.LLM lives on the exchange because each
        # exchange may target a different model.
        self.acquired_llm = await self._acquire_llm_if_needed()

        # Step 7 — resolve fallback chain. Each entry's provider must
        # be in `llm_providers`; clients are NOT built here (lazy
        # construction is the engine loop's job).
        self.fallback_chain = await self._resolve_fallback_chain()

        # Register tools with the Tools subsystem now (NOT with the
        # framework — that happens in setup). The synthetic
        # force-invoke read in step 9 routes through
        # `tools_subsystem.invoke_tool`, which requires registration
        # to have happened. Registration runs BEFORE the prompt-render
        # step so the render has access to the registered tool
        # metadata when building the `tools` variable.
        await self._register_tools_with_subsystem()

        # Render the prompt template with the standardized variable
        # context. Replaces `self.config.prompt` with the rendered
        # string so `config.set` persists the prompt the model
        # actually saw. Plain-text prompts (no Jinja markers) survive
        # the round-trip byte-identical via the StrictUndefined
        # render harness.
        await self._render_prompt_if_needed()

        # Step 8 — emit `config.set` if the current effective config
        # differs from the most recent `config.set` on the thread.
        await self._emit_config_set_if_changed()

        # Step 9 — process force-invoke skills, if any.
        await self._process_force_invoke()

        # Step 10 — emit `thread.exchange_started`. The trigger_event_id
        # is the trigger event id; pre-allocate the user.message id,
        # emit exchange_started carrying that id, then emit user.message
        # under that id.
        self.user_message_event_id = f"ev_{uuid4()}"
        started = await self._emit(
            type_=EventType.THREAD_EXCHANGE_STARTED,
            data=ThreadExchangeStartedData(
                trigger_event_id=EventId(self.user_message_event_id),
            ).model_dump(mode="json"),
            actor=Actor(kind="system"),
        )
        self.exchange_started_id = str(started.event_id)

        # Step 10.5 — spec-068 Part B resume reconciliation. Any begin
        # event (`tool.called`, `prompt.requested`) on the leaf path
        # whose paired terminal never landed (prior process died
        # between emits) permanently 400s every future exchange because
        # the provider rejects a `tool_calls` message with no matching
        # `tool` reply. Close every such span with an engine-authored
        # terminal HERE — BEFORE the trigger `user.message` in Step 11
        # — so the terminal precedes the trigger in file order. That
        # ordering is load-bearing: the projector produces
        # `[..., assistant(tool_calls=[t1]), tool(t1), user]`, and
        # `_history_before_last_user` (which splits on the last user)
        # correctly hands the `tool(t1)` message to the framework as
        # history. If reconciliation ran AFTER Step 11 (as it did in
        # the original Part B landing), file order became
        # `..., tool.called(t1), user.message, tool.failed(t1)`,
        # projecting to `[assistant(tool_calls=[t1]), user, tool(t1)]`
        # — and the tool message got sliced off, wedging the exchange
        # on any real OpenAI-compatible provider. Span-keyed (not
        # tail-windowed), idempotent, and routes through `_emit` so I1
        # sole-emitter holds. Runs BEFORE the live-bus subscription in
        # `ChatApp` starts replaying / dispatching to widgets — a live
        # prompt injected after replay has no orphan begin on the
        # persisted path so reconciliation ignores it.
        await self._reconcile_open_spans_on_resume()

        # Step 11 — materialize the trigger as user.message.
        await self._emit(
            type_=EventType.USER_MESSAGE,
            data=UserMessageData(
                content=_extract_trigger_content(self.context.trigger),
            ).model_dump(mode="json"),
            actor=Actor(kind="user", id="user.local"),
            event_id_override=EventId(self.user_message_event_id),
        )

    async def _acquire_llm_if_needed(self) -> crewai.LLM | None:
        """Acquire a `crewai.LLM` via `build_llm` for the primary spec.

        Skipped (returns `None`) when the engine was constructed
        with a callable `llm` — the test path keeps working
        unchanged. Also skipped when `llm_for_model` is wired: the
        per-spec factory handles construction lazily at call time
        (including `provider: "mock"` → EchoLLMDriver). Otherwise
        the configured provider is looked up in
        `AgentEngine._llm_providers_config` (read from
        `platform.yaml` at runtime bootstrap), `build_llm` is called
        with the agent's `ModelSpec`, the provider config, and
        secrets, and the constructed `crewai.LLM` is returned for
        use by the framework adapter.
        """
        if self.engine._llm is not None:
            return None
        if self.engine._llm_for_model is not None:
            # Factory handles construction (including provider: mock)
            # at call time; pre-flight acquire would only duplicate
            # work AND fail for `provider: mock` because no `mock`
            # connector is registered.
            return None
        from llm import build_llm  # noqa: PLC0415 — defer crewai import to runtime

        provider_name = self.config.model.provider
        provider_config = self.engine._llm_providers_config.get(provider_name)
        return await build_llm(
            self.config.model,
            provider_config,
            self.engine._secrets,
            configured_providers=sorted(self.engine._llm_providers_config),
        )

    def _build_llm_factory(
        self,
    ) -> Callable[
        [ModelSpec],
        Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]],
    ]:
        """Return the per-spec LLM factory the orchestrator uses.

        Production callers wire `llm_for_model=` at engine
        construction (typically a closure around `llm.build_llm` and
        the runtime's `llm_providers_config`). Callers passing only
        `llm=` get a degenerate factory that returns the same single
        callable for every spec — fallback still works in tests,
        but every chain entry calls the same underlying LLM.
        """
        per_spec = self.engine._llm_for_model
        if per_spec is not None:
            return per_spec
        single = self.engine._llm
        if single is None:
            msg = (
                "engine was constructed without either `llm=` or "
                "`llm_for_model=` — no way to make an LLM call. Pass "
                "one of them at engine construction, or wire build_llm "
                "via platform_runtime (Phase 1.1+)."
            )
            raise PlatformInternalError(
                original_type="engine.llm_adapter_missing",
                args=[msg],
            )

        async def _single_factory(
            spec: ModelSpec,
        ) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
            _ = spec
            return single

        return _single_factory

    async def _resolve_fallback_chain(self) -> list[ModelSpec]:
        """Resolve and validate the agent's fallback chain.

        Reads `config.fallback_models` — the platform default
        (`defaults.fallback_models`) is already merged in by
        bootstrap, so the engine only reads the resolved field. If
        the agent specifies `fallback_models` (even `[]`), that
        wins. Clients are NOT built here.

        Validates each entry's provider is in `llm_providers`. Emits
        a `config.fallback_duplicate` warning per duplicate
        (Diagnostic).
        """
        from engine._fallback import build_fallback_chain  # noqa: PLC0415

        configured = sorted(self.engine._llm_providers_config)
        warnings: list[ConfigFallbackDuplicate] = []
        chain = build_fallback_chain(
            agent_id=self.agent_id,
            fallback_specs=list(self.config.fallback_models),
            primary=self.config.model,
            configured_providers=configured,
            emit_warning=warnings.append,
        )
        # Surface duplicates visibly. For now the engine forwards
        # them as Diagnostic-level platform-log warnings; a future
        # patch may add a real CLI banner. Tests observe them via
        # `_Exchange.fallback_warnings` (kept for introspection).
        self.fallback_warnings: list[ConfigFallbackDuplicate] = warnings
        for w in warnings:
            _log.warning("%s", str(w))
        return chain

    async def _emit_config_set_if_changed(self) -> None:
        """Emit `config.set` when the effective config has changed.

        Walks the path for the most recent `config.set` event and
        compares its `resolved_config` to the current one; emits a
        new event only when they differ (or when no prior event
        exists).
        """
        cur_resolved = self.config.model_dump(mode="json")
        # Walk path, find latest config.set.
        latest_set: Event | None = None
        async for event in self.engine._store.read_path_to_leaf(
            self.context.thread_id,
        ):
            if event.type == EventType.CONFIG_SET:
                latest_set = event
        latest_matches = (
            latest_set is not None
            and latest_set.data.get("resolved_config") == cur_resolved
        )
        if latest_set is not None and latest_matches:
            self.config_event_id = str(latest_set.event_id)
            return
        # Emit a new one.
        config_hash = _stable_hash(cur_resolved)
        evt = await self._emit(
            type_=EventType.CONFIG_SET,
            data=ConfigSetData(
                resolved_config=cur_resolved,
                config_hash=config_hash,
            ).model_dump(mode="json"),
            actor=Actor(kind="system"),
        )
        self.config_event_id = str(evt.event_id)

    async def _render_prompt_if_needed(self) -> None:
        """Render the agent's Jinja2 prompt template with the standard vars.

        Builds the top-level variables (`tools`, `docs`, `workspace`,
        `agent`, `platform`, `skills`) from the resolved config and
        runtime state, calls `templating.render.render_prompt`, and
        replaces `self.config` with a `model_copy` carrying the
        rendered string. A prompt with no Jinja markers renders
        byte-identical to itself, so plain-text agents survive the
        round-trip untouched.

        Walks the prompt's Jinja AST after render and emits
        `agent.template_warning` (Recoverable) for variable accesses
        whose data is empty (e.g. `{{ tools }}` referenced but the
        agent declares no tools).
        """
        prompt = self.config.prompt
        if not isinstance(prompt, str):
            # Layered list-form prompts are treated as fully
            # pre-resolved by the asset manager — skip the render.
            return

        from templating import render_prompt  # noqa: PLC0415
        from templating.inheritance import (  # noqa: PLC0415
            prompt_uses_extends_directive,
            render_with_parents,
            resolve_parent_chain_from_extends,
        )
        from templating.vars import (  # noqa: PLC0415
            AgentVar,
            DocsVar,
            ModelView,
            SkillItem,
            SkillsVar,
            ToolsVar,
            WorkspaceVar,
        )
        from templating.vars._platform import (  # noqa: PLC0415
            AliasesVar,
            AuthorizationVar,
            FileAccessVar,
            PlatformVar,
            ProvidersVar,
        )
        from templating.vars.docs import DocItem  # noqa: PLC0415
        from templating.vars.tools import ToolItem  # noqa: PLC0415

        # ToolsVar — from the just-registered tool defns.
        registered = self.engine._tools.list_registered_tools(
            self.agent_id,  # type: ignore[arg-type]
        )
        tool_items: list[ToolItem] = []
        for info in registered:
            defn = self.engine._tools._registry.get_tool(
                self.agent_id,  # type: ignore[arg-type]
                info.name,
            )
            summary = getattr(defn, "summary", "") if defn is not None else ""
            tool_items.append(
                ToolItem(
                    name=info.name,
                    description=info.description,
                    summary=summary or info.description.split(".")[0].strip(),
                    signature="",
                    schema=info.args_schema,
                )
            )
        tools_var = ToolsVar(tool_items)

        # SkillsVar — the Jinja path renders the skills block from a
        # template variable instead of a post-append at assembly time.
        # `get_enabled_skills` returns disabled skills too (so
        # force-invoke can route to them); we filter them out here so
        # the model-facing prompt matches the historical
        # `format_skills_listing` behaviour.
        enabled_skills = await self.engine._skills.get_enabled_skills(self.config)
        skill_items: list[SkillItem] = [
            SkillItem(name=s.name, description=s.description, ref=s.ref)
            for s in enabled_skills
            if not s.disable_model_invocation
        ]
        skills_var = SkillsVar(skill_items)

        # DocsVar — load each ContextFileSpec.
        from templating.context_files import (  # noqa: PLC0415
            load_context_file,
        )

        workspace_root = Path(self.engine._assets._root)
        doc_items: list[DocItem] = []
        for spec in self.config.context_files:
            doc_items.append(
                await asyncio.to_thread(load_context_file, spec, workspace_root)
            )
        docs_var = DocsVar(doc_items)

        # WorkspaceVar from platform.yaml.
        platform_cfg = self.engine._assets._platform_config or {}
        file_access = platform_cfg.get("file_access") or {}
        workspace_summary = platform_cfg.get("workspace_summary")
        workspace_var = WorkspaceVar(
            path=str(workspace_root),
            summary=workspace_summary,
            allowed_paths=list(file_access.get("allowed_paths", [])),
            denied_paths=list(file_access.get("denied_paths", [])),
        )

        # AgentVar — self-introspection over the resolved config.
        agent_var = AgentVar(
            name=self.config.name,
            id=str(self.config.agent_id),
            model=ModelView.from_model_spec(self.config.model),
            tools=tools_var,
            permissions=list(self.config.permissions),
            context_files=list(doc_items),
        )

        # PlatformVar — projection over platform.yaml.
        providers = list(self.engine._llm_providers_config.values())
        authz_cfg = platform_cfg.get("authorization", {}) or {}
        platform_var = PlatformVar(
            providers=ProvidersVar(providers),
            authorization=AuthorizationVar(
                variant=authz_cfg.get("variant", ""),
                rules=[],
            ),
            workspace_summary=workspace_summary,
            file_access=FileAccessVar(
                allowed_paths=list(file_access.get("allowed_paths", [])),
                denied_paths=list(file_access.get("denied_paths", [])),
            ),
            aliases=AliasesVar(platform_cfg.get("model_aliases", {}) or {}),
            _workspace_path=str(workspace_root),
        )

        context = {
            "tools": tools_var,
            "docs": docs_var,
            "skills": skills_var,
            "workspace": workspace_var,
            "agent": agent_var,
            "platform": platform_var,
        }

        # Three paths for prompt render:
        #
        #   1. orphan: prompt has `{% extends parent %}` but the
        #      YAML has no `extends:` field → typed Fatal so the
        #      author gets a useful error instead of the
        #      loader-less Environment's `TypeError: no loader …`.
        #   2. inheritance: prompt has an `extends:` field → walk
        #      the parent chain via the catalog and render via
        #      `render_with_parents` (DictLoader-backed env).
        #   3. plain: `render_prompt` with the no-loader
        #      StrictUndefined env.
        #
        # Order is load-bearing: the orphan check must fire BEFORE
        # `render_prompt` because that path's env has no loader and
        # would crash on the directive.
        template_name = f"agent:{self.config.agent_id}"
        if self.config.extends is None and prompt_uses_extends_directive(prompt):
            raise ConfigExtendsTemplateWithoutInheritance(
                agent_path=template_name,
            )
        if self.config.extends is not None:
            parent_chain_yaml = await asyncio.to_thread(
                resolve_parent_chain_from_extends,
                self.config.extends,
                workspace_root,
                self.engine._assets.catalog(),
            )
            parent_chain_prompts = [
                str(layer.get("prompt", "")) for layer in parent_chain_yaml
            ]
            rendered = render_with_parents(
                prompt,
                parent_chain_prompts,
                context,
                template_name=template_name,
            )
        else:
            rendered = render_prompt(
                prompt,
                context,
                template_name=template_name,
            )

        # Replace config with a copy carrying the rendered prompt.
        # ResolvedAgentConfig is frozen so we go through model_copy.
        self.config = self.config.model_copy(update={"prompt": rendered})

        # Emit `agent.template_warning` for empty-data references.
        # Minimal first cut: warn when the prompt references the
        # variable name but the corresponding data is empty. A future
        # patch can broaden this to a full AST walk.
        from schemas import AgentTemplateWarningData  # noqa: PLC0415

        warnings: list[tuple[str, str, str]] = []
        if "tools" in prompt and tools_var.count == 0:
            warnings.append(
                (
                    "tools",
                    "prompt references {{ tools }} but agent declares no tools",
                    "add tools to subsystems.tools.config.declared or "
                    "remove the reference",
                )
            )
        if "docs" in prompt and docs_var.count == 0:
            warnings.append(
                (
                    "docs",
                    "prompt references {{ docs }} but agent declares no context_files",
                    "add a context_files: entry or remove the reference",
                )
            )
        # Symmetric warning for the `{{ skills }}` variable.
        if "skills" in prompt and skills_var.count == 0:
            warnings.append(
                (
                    "skills",
                    "prompt references {{ skills }} but agent declares no skills",
                    "add a skills entry under subsystems.skills.config.enabled "
                    "or remove the reference",
                )
            )
        # Migration warning. Historically, every rendered prompt got
        # an auto-appended `## Available Skills` block whether the
        # template asked for it or not. Now the prompt owns rendering;
        # agents that declare skills but whose template doesn't
        # reference them would silently lose the listing. Fire a
        # Recoverable so the author sees the gap on first agent-load.
        if skills_var.count > 0 and "skills" not in prompt:
            warnings.append(
                (
                    "skills",
                    "agent declares skills but prompt does not reference "
                    "{{ skills }} — the auto-appended listing was removed "
                    "in qol-skills-jinja-variable-symmetric-with-tools.md",
                    "add a `{{ skills }}` block to the prompt template "
                    "(see docs/explanation/tools-subsystem.md)",
                )
            )
        for reference, reason, hint in warnings:
            await self._emit(
                type_=EventType.AGENT_TEMPLATE_WARNING,
                data=AgentTemplateWarningData(
                    reference=reference,
                    reason=reason,
                    hint=hint,
                ).model_dump(mode="json"),
                actor=Actor(kind="system"),
            )

    async def _register_tools_with_subsystem(self) -> None:
        """Register builtins / declared tools with the Tools subsystem.

        Resolves every `subsystems.tools.config.declared[*].ref` via
        `AssetManagement.get_asset` to its on-disk Path, then passes
        the collected paths to `register_for_agent` alongside any
        builtins. Without this resolution, declared tools never
        reach the framework — the model gets zero tools and
        hallucinates output.

        Re-registration is idempotent — `register_for_agent`
        overwrites in-place. The engine re-registers on every
        execute so a config change (different tools enabled) takes
        effect. The asset-management catalog is built once at
        bootstrap so the `get_asset` lookups are cheap; the actual
        Python imports happen inside `discover_tools_in_file` and
        hit Python's module cache on subsequent invocations.

        Missing refs raise
        [`ConfigUnknownTool`][errors.ConfigUnknownTool] (Fatal). MCP
        servers are still deferred — the shape is documented in the
        tools subsystem contract; engine wiring lands in a later
        phase.
        """
        tools_sub = self.config.subsystems.get("tools")
        if tools_sub is None:
            return
        body = tools_sub.config

        (
            declared_paths,
            declared_owner_bundles,
        ) = await self._resolve_declared_tool_paths(
            list(body.get("declared", []) or []),
        )

        # Thread the agent's permissions + its home bundle's
        # permissions to the tools subsystem so the dispatcher hands
        # them to `authz.allow`. The provider walks
        # agent → bundle → platform → no-rule callback → default-deny.
        # PermissionRule is structurally identical to Rule; copy every
        # field the provider matches on — dropping `pattern` or
        # `from_` here would silently degrade the caller-chain scope
        # or target glob of any layer-supplied rule.
        from pathlib import Path as _Path  # noqa: PLC0415

        from assets.manifest import permissions_source_map  # noqa: PLC0415
        from providers.authz import Rule  # noqa: PLC0415

        # feat-041 — source-line metadata is threaded via a sidecar
        # because `PermissionRule` (schemas.config) is `extra="forbid"`
        # and can't carry the fields directly. The map keys are the
        # PermissionRule's list index in its source YAML; the compiled
        # `Rule` on the provider side stamps them onto `source_file` +
        # `source_line`. Absent YAML → empty map → `None` fallback.
        agent_source_map = self._agent_permissions_source_map()
        agent_rules: list[Rule] = [
            Rule(
                id=r.id,
                who=r.who,
                what=r.what,
                pattern=r.pattern,
                from_=r.from_,
                effect=r.effect,
                source_file=agent_source_map.get(i, (None, None))[0],
                source_line=agent_source_map.get(i, (None, None))[1],
            )
            for i, r in enumerate(self.config.permissions)
        ]
        bundle_rules: list[Rule] = []
        home_bundle = self.engine._assets.get_bundle_for_agent_id(self.agent_id)
        if home_bundle is not None:
            manifest = self.engine._assets.get_bundle(home_bundle)
            if manifest is not None:
                bundle_source_map: dict[int, tuple[str, int]] = {}
                try:
                    bundle_yaml = _Path(manifest.path) / "bundle.yaml"
                    bundle_source_map = permissions_source_map(bundle_yaml)
                except (OSError, ValueError):
                    bundle_source_map = {}
                bundle_rules = [
                    Rule(
                        id=r.id,
                        who=r.who,
                        what=r.what,
                        pattern=r.pattern,
                        from_=r.from_,
                        effect=r.effect,
                        source_file=bundle_source_map.get(i, (None, None))[0],
                        source_line=bundle_source_map.get(i, (None, None))[1],
                    )
                    for i, r in enumerate(manifest.permissions)
                ]

        # feat-054 Phase 2: auto-register `reference_lookup` when
        # compaction is active for this exchange. The engine's
        # provider being non-None is the enable flag — an agent whose
        # exchanges get compacted needs to be able to fetch cited
        # events. Skipped when compaction is disabled (no engine
        # provider) so agents without compaction see no extra tool.
        programmatic_tools: list[Callable[..., Awaitable[Any]]] = []
        if self.engine._compaction_provider is not None:
            from tools.context_reference import (  # noqa: PLC0415
                make_reference_lookup,
            )

            programmatic_tools.append(
                make_reference_lookup(session_store=self.engine._store),
            )

        await self.engine._tools.register_for_agent(
            agent_id=self.agent_id,  # type: ignore[arg-type]
            tools=programmatic_tools or None,
            builtin_names=list(body.get("builtin", []) or []),
            declared_paths=declared_paths,
            # Per-declared-path owner bundle derived from the ref's
            # leading segment (bug-authz-rule-caller-bundle-mismatch).
            # A butler-in-`andre` extending `kolsch/agents/helper`
            # inherits declared refs like `kolsch/tools/read_file` —
            # the tool's authoritative home is `kolsch`, NOT the
            # caller agent's bundle. Threading the map through
            # register_for_agent stamps each defn with its ref's
            # bundle instead of a single caller-derived default.
            declared_owner_bundles=declared_owner_bundles,
            agent_permissions=agent_rules,
            bundle_permissions=bundle_rules,
            # The home_bundle resolved above (from the caller agent's
            # own bundle) sources bundle_permissions and stamps
            # PROGRAMMATIC / TEST tools that don't come through the
            # declared-refs path. For declared tools the per-path
            # owner map takes precedence. Falls through as None for
            # synthetic test configs; the subsystem stamps "kolsch"
            # for builtins and legacy 1-seg action form for
            # unstamped declared tools in that case.
            home_bundle=home_bundle,
        )
        # MCP server registration lands when the MCP wiring patch ships.

    def _agent_permissions_source_map(self) -> dict[int, tuple[str, int]]:
        """Return `{permission_index: (source_file, source_line)}` for this agent.

        feat-041 — resolves the agent's on-disk YAML path via the
        asset catalog (`<bundle>/agents/<name>` lookup) and calls
        `permissions_source_map`. Empty when the catalog doesn't know
        this agent (synthetic test configs) or the YAML lacks position
        info (custom `!tag` type — see spec §Design refinements).
        """
        from pathlib import Path as _Path  # noqa: PLC0415

        from assets.manifest import permissions_source_map  # noqa: PLC0415

        try:
            catalog = self.engine._assets.catalog()
        except Exception:  # noqa: BLE001 — never raise out of source-tag path
            return {}
        for entry in catalog.assets.values():
            if entry.asset_type != "agent":
                continue
            # Match by resolved agent_id via the same derivation the
            # catalog scanner uses. Explicit `agent_id:` YAMLs won't
            # match this derivation — the cache in AssetManagement
            # handles those but doesn't expose the source path here.
            # Falling through to the empty map is acceptable: the
            # `/why` card degrades to `(unknown source)` for agents
            # that carry an explicit id.
            agent_name = entry.ref.rsplit("/", 1)[-1]
            if entry.bundle == self.engine._assets.get_bundle_for_agent_id(
                self.agent_id,
            ) and agent_name in str(self.agent_id):
                try:
                    return permissions_source_map(_Path(entry.path))
                except (OSError, ValueError):
                    return {}
        return {}

    async def _resolve_declared_tool_paths(
        self,
        declared_entries: list[dict[str, Any]],
    ) -> tuple[list[Path], dict[Path, str]]:
        """Resolve each declared tool's `ref` to a Path + owner bundle.

        Returns `(paths, owner_bundles)`:

        - `paths` — resolved on-disk paths in declaration order.
        - `owner_bundles` — path → owner-bundle string, derived from
          the ref's leading segment (e.g. `kolsch/tools/read_file`
          → `"kolsch"`). This is the tool's AUTHORITATIVE home bundle
          for stamping `defn.home_bundle` at registration time. The
          convention matches
          [`_tools_for_agent`][cli._slash._tools_for_agent] at
          `src/cli/_slash.py:1755-1756`.

        Threading the owner bundle through fixes
        bug-authz-rule-caller-bundle-mismatch: a butler agent in the
        `andre` bundle extending `kolsch/agents/helper` inherits
        declared refs like `kolsch/tools/read_file`; before this
        change, `register_for_agent` stamped each tool with the
        caller agent's bundle (`andre`) so `defn.action_name`
        returned `andre/tools/read_file` and modal-granted rules
        landed under the wrong bundle.

        The asset catalog (built at runtime bootstrap via
        `AssetManagement.initialize`) already scanned every bundle's
        `tools/` directory and produced an `AssetEntry` per file.
        This method maps each config-supplied ref to its catalog
        entry's `path` field alongside the owner-bundle derivation.

        Missing refs surface as
        [`ConfigUnknownTool`][errors.ConfigUnknownTool] (Fatal) with
        the available refs listed in the error detail so the user
        sees an actionable error rather than a silent-skip.
        """
        from errors import AssetsNotFound, ConfigUnknownTool  # noqa: PLC0415

        paths: list[Path] = []
        owner_bundles: dict[Path, str] = {}
        for entry in declared_entries:
            # The Layer 3 validator rejects non-dict declared entries
            # at config-load time (see validate_standard_config), so
            # at this point every entry is a dict — defensive
            # isinstance was stripped to satisfy mypy's narrowing.
            ref = entry.get("ref")
            if not isinstance(ref, str) or not ref:
                continue
            try:
                asset = await self.engine._assets.get_asset(
                    ref, field_asset_type="tool"
                )
            except AssetsNotFound as exc:
                # list_assets is sync (no await); the catalog is a
                # snapshot built at runtime bootstrap.
                available = [
                    a.ref for a in self.engine._assets.list_assets(asset_type="tool")
                ]
                raise ConfigUnknownTool(
                    ref=ref,
                    available_refs=available,
                    agent_id=str(self.agent_id),
                ) from exc
            path = Path(asset.path)
            paths.append(path)
            # Owner bundle from the ref's leading segment. Refs are
            # 3-segment catalog refs (`<bundle>/tools/<name>`) by
            # convention; a stray 1-segment ref (test path) falls
            # back to "kolsch" mirroring _slash.py:1755-1756.
            segments = ref.split("/")
            owner_bundles[path] = segments[0] if len(segments) > 1 else "kolsch"
        return paths, owner_bundles

    async def _process_force_invoke(self) -> None:
        """Emit a synthetic read for each force-invoke skill.

        For each entry in the trigger's `force_skills` list, emits a
        `tool.called` / `tool.result` pair for `read` on the
        SKILL.md path, with `principal="system"`.
        """
        trigger = self.context.trigger
        if trigger.get("kind") != "user_message_with_force_skill":
            return
        force_skills = list(trigger.get("force_skills", []) or [])
        if not force_skills:
            return

        for skill_ref in force_skills:
            # Verify enabled — SkillsSubsystem.force_invoke raises
            # SkillForcedNotEnabled if not.
            invocation = await self.engine._skills.force_invoke(
                skill_ref=str(skill_ref),
                config=self.config,
            )

            tool_call_id = f"tc_force_{uuid4()}"
            # Emit synthetic tool.called (principal: system).
            await self._emit(
                type_=EventType.TOOL_CALLED,
                data=ToolCalledData(
                    tool_name="read",
                    tool_call_id=tool_call_id,
                    arguments={"path": invocation.skill_md_path},
                ).model_dump(mode="json"),
                actor=Actor(kind="system"),
            )
            # spec-068 Part A — register the span. The observer's
            # symmetric wiring covers model-driven tool calls; this
            # force-invoke path is a separate emit site with the
            # same shape. Register so the exchange finally's
            # backstop can still close a span that slips past a
            # `CancelledError` propagating up from `invoke_tool`
            # (the `except PlatformError` below does not catch that).
            from engine.observers import (  # noqa: PLC0415
                _build_session_interrupted_tool_failed,
            )

            _force_span_id = tool_call_id

            def _force_terminal_factory(
                _span_id: str = _force_span_id,
            ) -> tuple[EventType, dict[str, Any], Actor]:
                return _build_session_interrupted_tool_failed(
                    tool_call_id=_span_id,
                    tool_name="read",
                )

            self.register_span(tool_call_id, _force_terminal_factory)
            # Drive the read tool via the Tools subsystem with
            # principal=system.
            try:
                result = await self.engine._tools.invoke_tool(
                    tool_name="read",
                    args={"path": invocation.skill_md_path},
                    context=ToolContext(
                        agent_id=self.agent_id,  # type: ignore[arg-type]
                        session_id=self.session_id,  # type: ignore[arg-type]
                        thread_id=self.thread_id,  # type: ignore[arg-type]
                        principal="system",
                        secrets={},
                        tool_call_id=tool_call_id,
                    ),
                )
            except PlatformError as exc:
                # Even synthetic reads must surface as failed events.
                from schemas.events import ToolFailedData  # noqa: PLC0415

                err = Error.from_platform_error(exc)
                await self._emit(
                    type_=EventType.TOOL_FAILED,
                    data=ToolFailedData(
                        tool_call_id=tool_call_id,
                        tool_name="read",
                        error=err,
                    ).model_dump(mode="json"),
                    actor=Actor(kind="system"),
                )
                # Terminal already emitted — mark the span closed so
                # the exchange finally's backstop doesn't double-emit.
                self.close_span(tool_call_id)
                # A force-invoke read failure is fatal — the spec
                # treats force-invoke as a pre-flight prerequisite.
                raise

            raw_result = result.result
            result_payload = (
                raw_result if isinstance(raw_result, dict) else {"value": raw_result}
            )
            await self._emit(
                type_=EventType.TOOL_RESULT,
                data=ToolResultData(
                    tool_call_id=tool_call_id,
                    tool_name="read",
                    result=result_payload,
                ).model_dump(mode="json"),
                actor=Actor(kind="system"),
            )
            # Happy-path terminal emitted — close the span so the
            # exchange finally's backstop skips it.
            self.close_span(tool_call_id)

    # ---- Framework setup + driver loop --------------------------------

    async def _run_framework(self) -> str:
        # spec-068 Part B resume reconciliation runs earlier — from
        # `_preflight` between the `thread.exchange_started` emit and
        # the trigger `user.message` emit — so terminals for dangling
        # spans land in the log BEFORE `user.message`. See the block
        # docstring at the reconciliation call site in `_preflight` for
        # why the ordering is load-bearing.

        # Build assembled context.
        events = [
            e
            async for e in self.engine._store.read_path_to_leaf(
                self.context.thread_id,
            )
        ]

        # Skills are rendered into the prompt as the `{{ skills }}`
        # Jinja variable during pre-flight, so the assembly here only
        # handles the message projection and tool schemas.
        registered = self.engine._tools.list_registered_tools(
            self.agent_id,  # type: ignore[arg-type]
        )
        tool_schemas = [
            {"name": t.name, "description": t.description, "schema": t.args_schema}
            for t in registered
        ]

        # feat-054 Phase 2: compaction trigger check. Only fires when a
        # provider is wired on the engine AND the model declares a
        # context_length. Skips on missing context_length (opencode
        # models frequently omit it), on provider-None, and on any
        # summariser failure — Phase 4 introduces the retry ladder.
        events = await self._maybe_compact_events(events, tool_schemas)

        assembled = self.engine._assemble_context_cached(
            events,
            tool_schemas=tool_schemas,
        )

        # Register the LLM/tool seams the framework will invoke.
        #
        # The LLM seam is `FallbackLLMHandle`: it wraps per-attempt
        # observation plus the chain loop. The factory we hand it
        # produces a per-attempt
        # `Callable[[LLMRequest], Awaitable[LLMResponse]]`:
        #
        # - If the engine was constructed with `llm_for_model=` (the
        #   per-spec factory — production wires this via
        #   `platform_runtime`'s `build_llm`), use it directly. Each
        #   chain entry gets its own client built lazily.
        # - If only `llm=` was passed (tests + the default-tier
        #   EchoLLMDriver bootstrap), wrap it so every spec returns
        #   the same callable. The chain still works; just every
        #   entry calls the same underlying llm.
        # - If neither, raise PlatformInternalError — the engine has
        #   no way to make an LLM call.
        llm_factory = self._build_llm_factory()
        from engine._fallback import FallbackLLMHandle, StreamState  # noqa: PLC0415

        # The orchestrator consults this StreamState before advancing
        # the chain on a Recoverable error. Once a streaming
        # connector marks `mark_content_emitted()`, fallback is
        # short-circuited and the engine emits
        # `thread.exchange_failed` with the truncation visible.
        # Non-streaming connectors (today: all of them) never set
        # the flag, so behaviour is unchanged.
        self.stream_state = StreamState()
        llm_handle = FallbackLLMHandle(
            emit=self._emit_void,
            agent_id=self.agent_id,
            primary_spec=self.config.model,
            chain=list(self.fallback_chain),
            llm_factory=llm_factory,
            config_event_id=self.config_event_id,
            stream_state=self.stream_state,
        )
        tool_handles = [
            _ObservedToolHandle(
                emit=self._emit_void,
                agent_id=self.agent_id,
                session_id=self.session_id,
                thread_id=self.thread_id,
                tools_subsystem=self.engine._tools,
                defn=self.engine._tools._registry.get_tool(  # type: ignore[arg-type]
                    self.agent_id,  # type: ignore[arg-type]
                    t.name,
                ),
                # Thread the exchange's CancelToken so tool bodies
                # receive it via `ctx.cancellation`.
                cancel=self.cancel,
                # spec-068 Part A — wire the exchange's span registry
                # so the observer can register on `tool.called` and
                # close on any terminal arm. The exchange finally
                # emits terminals for any span still open at unwind.
                register_span=self.register_span,
                close_span=self.close_span,
            )
            for t in registered
        ]
        # Filter any None defns (shouldn't happen if registration is sound).
        tool_handles = [h for h in tool_handles if h.name]

        # Hand to the framework. The user_message text is the most
        # recent user.message's content text projection; history is
        # everything BEFORE it (prior turns from the path).
        user_message_text = _last_user_text(assembled.messages)
        history_messages = _history_before_last_user(assembled.messages)

        # Engine-owned retry on Recoverable failures.
        max_attempts = self.config.runtime.max_retries + 1
        attempts_data: list[dict[str, Any]] = []
        last_exc: PlatformError | None = None
        for _attempt in range(max_attempts):
            if self.cancel.is_cancelled():
                from errors import ExchangeCancelled  # noqa: PLC0415

                raise ExchangeCancelled(
                    cancelled_at_event_id=self.exchange_started_id or "",
                )
            try:
                return await self.engine._framework.run_exchange(
                    system_prompt=assembled.system_prompt,
                    user_message=user_message_text,
                    history=history_messages,
                    llm=llm_handle,
                    tools=tool_handles,
                    cancellation=self.cancel.is_cancelled,
                    # The raw token gives the framework access to
                    # `wait_cancelled()` so it can race in-flight
                    # awaits instead of only polling at iteration
                    # boundaries.
                    cancel_token=self.cancel,
                )
            except PlatformError as exc:
                if exc.level.name != "RECOVERABLE":
                    raise
                attempts_data.append(
                    Error.from_platform_error(exc).model_dump(mode="json")
                )
                last_exc = exc
                continue
            except asyncio.CancelledError:
                from errors import ExchangeCancelled  # noqa: PLC0415

                raise ExchangeCancelled(
                    cancelled_at_event_id=self.exchange_started_id or "",
                ) from None
            except Exception as exc:
                # IN-LOOP cause-chain classifier. A transport failure
                # whose `__cause__` carries a known typed exception
                # (e.g. `openai.APITimeoutError` through CrewAI's
                # adapter's `raise ConnectionError(...) from e`)
                # classifies as ModelUpstreamError (Recoverable) and
                # the loop retries up to `max_retries+1` attempts
                # before exhausting. `runtime.max_retries` finally
                # means something for transport hiccups.
                #
                # Branch table:
                #   Recoverable match → append to attempts_data,
                #                       continue (mirrors the
                #                       PlatformError-Recoverable
                #                       branch above).
                #   Fatal match       → `raise classified from exc`
                #                       so the outer
                #                       `except PlatformError` at
                #                       run() picks it up as the
                #                       typed surface (e.g.
                #                       `agent.unauthorized` instead
                #                       of `platform.internal_error`).
                #   No match          → bare `raise`; the outer
                #                       boundary's cause-chain safety
                #                       net takes over, ultimately
                #                       falling through to
                #                       PlatformInternalError for
                #                       genuine engine bugs.
                from engine._classify import (  # noqa: PLC0415
                    classify_from_cause_chain,
                )

                classified = classify_from_cause_chain(exc, agent_id=self.agent_id)
                if classified is None:
                    raise
                if classified.level.name != "RECOVERABLE":
                    raise classified from exc
                attempts_data.append(
                    Error.from_platform_error(classified).model_dump(mode="json")
                )
                last_exc = classified
                continue

        # Exhausted retries — Fatal.
        raise ModelRetryExhausted(
            attempts=attempts_data,
        ) from last_exc

    # ---- Finalize -----------------------------------------------------

    async def _finalize(self, agent_text: str) -> None:
        agent_msg = await self._emit(
            type_=EventType.AGENT_MESSAGE,
            data=AgentMessageData(
                content={"text": agent_text},
                exchange_id=EventId(self.exchange_started_id or ""),
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=self.agent_id),
        )
        await self._emit(
            type_=EventType.THREAD_EXCHANGE_COMPLETED,
            data=ThreadExchangeCompletedData(
                exchange_started_id=EventId(self.exchange_started_id or ""),
                agent_message_id=agent_msg.event_id,
            ).model_dump(mode="json"),
            actor=Actor(kind="system"),
        )

    async def emit_exchange_failed(
        self,
        exc: PlatformError,
        *,
        allow_no_started: bool = False,
    ) -> None:
        err = Error.from_platform_error(exc)
        if self.exchange_started_id is None and not allow_no_started:
            # Without a started id we cannot satisfy the data shape.
            return
        # Pre-flight failures (e.g. authz denial) MUST emit
        # thread.exchange_failed even when no thread.exchange_started
        # preceded. The data shape requires a non-empty EventId;
        # synthesize a placeholder when missing.
        started_id = self.exchange_started_id or f"ev_{uuid4()}"
        await self._emit(
            type_=EventType.THREAD_EXCHANGE_FAILED,
            data=ThreadExchangeFailedData(
                exchange_started_id=EventId(started_id),
                error=err,
            ).model_dump(mode="json"),
            actor=Actor(kind="system"),
        )

    # ---- Compaction trigger (feat-054 Phase 2 + Phase 4 failure ladder) -

    async def _maybe_compact_events(
        self,
        events: list[Event],
        tool_schemas: list[dict[str, Any]],
    ) -> list[Event]:
        """Run the compaction pre-flight and return the (possibly-updated) event list.

        Returns the same ``events`` unchanged when compaction is
        disabled, when the model has no declared ``context_length``,
        or when the projected estimate is below the trigger. Otherwise
        runs the summariser through the Phase 4 failure ladder:

        1. **Tier 1 — retry once.** If the first summariser call raises
           OR the returned payload fails JSON parse, retry once with
           the same model.
        2. **Tier 2 — mechanical truncation fallback.** If tier 1 also
           fails, emit ``CONTEXT_COMPACTION_FAILED``, then construct a
           mechanical-truncation ``ContextCompactedData`` (no
           summariser prose; just ``refs[]`` one-liners the model can
           feed to :func:`context.reference_lookup`) and emit
           ``CONTEXT_COMPACTED`` with ``trigger="auto_truncation"``.
        3. **Tier 3 — hard-fail.** If mechanical truncation can't get
           below the budget either (recent-tail alone exceeds the
           model's window), raise
           :class:`ContextOverflowUncompactable`. The engine's outer
           ``execute`` translates the raise into
           ``thread.exchange_failed`` so the user's turn ends with an
           actionable error rather than a mid-stream provider bailout.

        Non-shrinking guard (Phase 4 §B): if the successful summary
        does not reduce projected tokens below ``0.90 *
        tokens_before``, the compaction is discarded, a
        ``CONTEXT_COMPACTION_SKIPPED`` event is emitted with
        ``reason="no_net_savings"``, and the trigger retries with a
        tighter ``keep_recent_fraction`` (halved, then quartered — max
        two retries). If all retries fail to shrink, the tier-2
        fallback fires.
        """
        provider = self.engine._compaction_provider
        if provider is None:
            return events

        # Look up the model's declared context_length via the runtime
        # cache the engine already carries. `context_length_for`
        # returns None when the model doesn't declare one, so
        # compaction is silently skipped for opencode-style models.
        context_length = _context_length_for_spec(
            self.engine._llm_providers_config,
            self.config.model,
        )
        if context_length is None:
            _log.debug(
                "compaction: model %s/%s has no declared context_length; skipping",
                self.config.model.provider,
                self.config.model.model,
            )
            # qol-092 — surface the silent skip as a chat-visible event
            # so the operator sees a distinct notice (with actionable
            # remediation) instead of the session running unchecked to
            # the LLM's real ceiling. Mirrors the ``no_net_savings``
            # emit shape at :2519 — same event type, same actor kind.
            #
            # bug-097 — gate the emit on a per-thread guard so a wedged
            # session doesn't stack N ``CompactionBlock`` widgets (one
            # per user turn). Debug log above still fires every turn
            # for engine-side observability. The guard is marked ONLY
            # after a successful ``_emit`` — if the underlying
            # ``store.append_event`` raises, the thread stays un-marked
            # and the next turn retries the emit rather than silently
            # skipping forever.
            if self.thread_id not in self.engine._no_context_length_skip_emitted:
                await self._emit(
                    type_=EventType.CONTEXT_COMPACTION_SKIPPED,
                    data={
                        "reason": "no_context_length",
                        "tokens_before": 0,
                        "tokens_after": 0,
                        "detail": {
                            "provider": self.config.model.provider,
                            "model": self.config.model.model,
                            "remediation_hint": _no_context_length_remediation(
                                self.engine._llm_providers_config,
                                self.config.model,
                            ),
                        },
                    },
                    actor=Actor(kind="system"),
                )
                self.engine._no_context_length_skip_emitted.add(self.thread_id)
            return events

        compaction_cfg = (self.engine._assets._platform_config or {}).get(
            "compaction",
            {},
        ) or {}
        # Preview projection: what would the model see today?
        preview = self.engine._assemble_context_cached(
            events,
            tool_schemas=tool_schemas,
            respect_compaction=True,
        )
        from providers.compaction import estimate_tokens_for_messages  # noqa: PLC0415

        preview_msgs = [m.model_dump() for m in preview.messages]
        estimate = estimate_tokens_for_messages(preview_msgs, preview.tools)

        # Reserve for max output tokens; default 4096 when unset.
        max_output = int(
            compaction_cfg.get("config", {}).get("max_output_tokens", 4096)
        )
        budget = max(context_length - max_output, 1)
        trigger_at = float(compaction_cfg.get("trigger_at", 0.80))
        if estimate < budget * trigger_at:
            return events

        # Trigger. Compute keep_tail from the fraction and delegate.
        base_keep_recent_fraction = float(
            compaction_cfg.get("config", {}).get("keep_recent_fraction", 0.20)
        )

        # Phase 4 §B — non-shrinking guard with two retries at tighter
        # keep_recent_fraction. Each attempt runs the tier 1 (retry-once)
        # summariser ladder; the outer loop steps down the tail fraction
        # if the produced summary didn't shrink enough.
        keep_fractions: list[float] = [
            base_keep_recent_fraction,
            base_keep_recent_fraction * 0.5,
            base_keep_recent_fraction * 0.25,
        ]

        last_error: Exception | None = None
        for attempt_index, kf in enumerate(keep_fractions):
            keep_tail_tokens = max(int(budget * kf), 1)
            inner_config: dict[str, Any] = dict(compaction_cfg.get("config", {}) or {})
            inner_config["keep_recent_fraction"] = kf
            outcome, error = await self._run_summariser_with_retry(
                provider=provider,
                events=events,
                keep_tail_tokens=keep_tail_tokens,
                context_length=context_length,
                inner_config=inner_config,
            )
            if outcome is None:
                last_error = error
                # Tier 1 exhausted on this attempt — jump straight to
                # tier 2 rather than continuing the shrink-retry loop
                # (a summariser that raised won't succeed with a
                # narrower tail).
                break
            if not outcome.compacted_event_ids:
                # No-op payload (short session). Nothing to do.
                return events
            # Non-shrinking guard.
            if outcome.tokens_before > 0 and (
                outcome.tokens_after > outcome.tokens_before * 0.90
            ):
                await self._emit(
                    type_=EventType.CONTEXT_COMPACTION_SKIPPED,
                    data={
                        "reason": "no_net_savings",
                        "tokens_before": outcome.tokens_before,
                        "tokens_after": outcome.tokens_after,
                        "keep_recent_fraction": kf,
                        "attempt": attempt_index + 1,
                    },
                    actor=Actor(kind="system"),
                )
                if attempt_index + 1 < len(keep_fractions):
                    continue
                # Exhausted retries — fall through to tier 2.
                last_error = RuntimeError(
                    "compaction produced no net token savings on any retry"
                )
                break
            # Success — emit and re-read the path.
            await self._emit(
                type_=EventType.CONTEXT_COMPACTED,
                data=outcome.model_dump(mode="json"),
                actor=Actor(kind="system"),
            )
            return [
                e
                async for e in self.engine._store.read_path_to_leaf(
                    self.context.thread_id,
                )
            ]

        # Tier 1 + shrink-retries exhausted. Move to tier 2: emit a
        # failure event, then attempt mechanical truncation.
        await self._emit(
            type_=EventType.CONTEXT_COMPACTION_FAILED,
            data={
                "error": {
                    "type": (
                        type(last_error).__name__
                        if last_error is not None
                        else "unknown"
                    ),
                    "message": (
                        str(last_error)
                        if last_error is not None
                        else "summariser exhausted retries"
                    ),
                    "where": "engine.compaction._maybe_compact_events",
                    "detail": {},
                },
                "span_first_event_id": None,
                "span_last_event_id": None,
            },
            actor=Actor(kind="system"),
        )

        # Tier 2 — mechanical truncation. Snap a boundary at the
        # base keep_recent_fraction (widest tail we're comfortable with)
        # and emit a mechanical CONTEXT_COMPACTED whose summary is
        # the stock "context truncated" stub. reference_lookup still
        # works because `refs[]` and `compacted_event_ids` cover the
        # dropped span.
        from providers.compaction import (  # noqa: PLC0415
            build_mechanical_truncation_payload,
        )

        keep_tail_tokens = max(int(budget * base_keep_recent_fraction), 1)
        mechanical = build_mechanical_truncation_payload(
            events=events,
            keep_tail_tokens=keep_tail_tokens,
        )
        if mechanical is None:
            # Tier 3 — even mechanical truncation cannot reduce below
            # budget. Raise; the engine's outer `execute` translates
            # into thread.exchange_failed with our distinct error type.
            from errors import ContextOverflowUncompactable  # noqa: PLC0415

            raise ContextOverflowUncompactable(
                thread_id=str(self.context.thread_id),
                keep_tail_tokens=keep_tail_tokens,
                budget_tokens=budget,
            )

        await self._emit(
            type_=EventType.CONTEXT_COMPACTED,
            data=mechanical.model_dump(mode="json"),
            actor=Actor(kind="system"),
        )
        return [
            e
            async for e in self.engine._store.read_path_to_leaf(
                self.context.thread_id,
            )
        ]

    async def _run_summariser_with_retry(
        self,
        *,
        provider: ContextCompactionProvider,
        events: list[Event],
        keep_tail_tokens: int,
        context_length: int,
        inner_config: dict[str, Any],
    ) -> tuple[Any, Exception | None]:
        """Invoke ``provider.compact`` up to twice; return ``(outcome, error)``.

        Tier 1 of the failure ladder — one retry with the same model +
        prompt to absorb transient rate-limits / network flakes.
        Success returns ``(payload, None)``; two failures return
        ``(None, last_error)``.
        """
        attempts = 2
        last_error: Exception | None = None
        for i in range(attempts):
            try:
                outcome = await provider.compact(
                    events=events,
                    keep_head=0,
                    keep_tail=keep_tail_tokens,
                    model_context_length=context_length,
                    thread_id=self.context.thread_id,
                    config=inner_config,
                )
            except Exception as exc:  # noqa: BLE001 — ladder catches any raise
                last_error = exc
                _log.warning(
                    "compaction: summariser attempt %d/%d failed (%s)",
                    i + 1,
                    attempts,
                    exc,
                )
                continue
            return outcome, None
        return None, last_error

    # ---- Emit helpers --------------------------------------------------

    async def _emit(
        self,
        *,
        type_: EventType,
        data: dict[str, Any],
        actor: Actor,
        event_id_override: EventId | None = None,
    ) -> Event:
        """Append, publish, and queue one event. Serialized per thread.

        Every event the engine emits carries the
        [`ExecutionContext.trace_id`][engine.context.ExecutionContext]
        set by the caller (the workflow agent runner stamps it from
        the workflow run's `trace_id`). Chat / API surfaces leave it
        `None` and the envelope field stays `None`; cross-JSONL
        correlation is opt-in at the `ExecutionContext` level, but
        the engine's stamping is unconditional so a callsite that
        DOES set it can't accidentally forget to surface it.
        """
        async with self._lock:
            leaf = await self.engine._store.get_leaf(self.context.thread_id)
            event = Event(
                event_id=event_id_override or EventId(f"ev_{uuid4()}"),
                parent_event_id=leaf.event_id if leaf is not None else None,
                thread_id=self.context.thread_id,
                session_id=self.session_id,  # type: ignore[arg-type]
                type=type_,
                data=data,
                actor=actor,
                created_at=datetime.now(UTC),
                trace_id=(
                    str(self.context.trace_id)
                    if self.context.trace_id is not None
                    else None
                ),
            )
            appended = await self.engine._store.append_event(
                self.context.thread_id,
                event,
            )
            self.queue.put_nowait(appended)
            # Bus publish — best-effort; never raises out of execute.
            try:
                await self.engine._bus.publish(appended)
            except Exception as bus_exc:  # noqa: BLE001
                _log.warning("bus.publish failed: %s", bus_exc)
            return appended

    async def _emit_void(
        self,
        *,
        type_: EventType,
        data: dict[str, Any],
        actor: Actor,
    ) -> None:
        """Observer-facing emit that drops the appended-event return.

        Matches the `Callable[..., Awaitable[None]]` shape the
        observers expect — they don't need the appended event.
        """
        await self._emit(type_=type_, data=data, actor=actor)


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _entry_context_length(entry: Any) -> int | None:
    """Extract ``context_length`` from a provider-catalog entry, or ``None``.

    Defensive: returns ``None`` for non-dict entries, missing keys,
    booleans (``bool`` subclasses ``int`` in Python so ``True`` would
    otherwise become ``1``), and non-int values.
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get("context_length")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _model_ids_match(a: str, b: str) -> bool:
    """Match two provider-catalog model IDs allowing vendor-prefix suffixes.

    Two IDs match when they are equal, or when one is a ``vendor/name``
    form whose trailing segment (after the final ``/``) equals the
    other. Handles the common opencode↔openrouter shape:
    ``kimi-k2.6`` ↔ ``moonshotai/kimi-k2.6``. Empty strings never match.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]


def _lookup_context_length_in_provider(
    provider: Any,
    model: str,
    *,
    allow_suffix: bool,
) -> int | None:
    """Walk ``provider.models`` for a matching entry and return its context_length.

    ``allow_suffix=False`` restricts to exact ``id == model`` (used for
    the direct-lookup fast path so it stays byte-identical to the
    pre-qol behavior). ``allow_suffix=True`` also accepts the
    vendor-prefix suffix shape (see :func:`_model_ids_match`).
    """
    for entry in getattr(provider, "models", []) or []:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str):
            continue
        if allow_suffix:
            if not _model_ids_match(entry_id, model):
                continue
        elif entry_id != model:
            continue
        value = _entry_context_length(entry)
        if value is not None:
            return value
    return None


def _context_length_for_spec(
    providers: dict[str, Any] | None,
    spec: Any,
) -> int | None:
    """Look up the declared ``context_length`` for ``spec`` across ``providers``.

    Three-tier lookup, in order:

    1. **Direct match** in the caller's own provider (byte-identical
       to the pre-qol fast path — ``spec.model == entry.id``).
    2. **Own-provider suffix match** — same provider, trailing-segment
       equality after splitting each candidate ``entry.id`` on ``/``.
       Handles a short-form spec (``kimi-k2.6``) against a catalog
       carrying vendor prefixes (``moonshotai/kimi-k2.6``).
    3. **Cross-provider fallback** — iterate every OTHER configured
       provider in ``platform.yaml`` insertion order, applying
       exact-then-suffix rules. First int-valued hit wins;
       ``None``-valued candidates carry no signal and are skipped
       entirely. If a second candidate exists with a *different*
       ``int`` value, a ``warning``-level log fires naming both
       candidates + values (silent when they agree).

    ``spec.provider == "mock"`` short-circuits to the direct fast path
    only — the mock provider's ID space (``mock-model``,
    ``mock-summariser``) can collide with real-provider suffixes
    ending in ``/mock``; silent ``None`` is safer than borrowed
    metadata.

    Never raises. Every degenerate input returns ``None``.
    """
    if not providers:
        return None
    spec_provider = getattr(spec, "provider", None)
    spec_model = getattr(spec, "model", None)
    if not isinstance(spec_provider, str) or not isinstance(spec_model, str):
        return None

    own = providers.get(spec_provider)
    if own is not None:
        # Tier 1 — direct exact match in the caller's own provider.
        direct = _lookup_context_length_in_provider(own, spec_model, allow_suffix=False)
        if direct is not None:
            return direct

    # Mock short-circuit: never borrow metadata for the mock provider.
    if spec_provider == "mock":
        return None

    if own is not None:
        # Tier 2 — same provider, suffix match.
        own_suffix = _lookup_context_length_in_provider(
            own, spec_model, allow_suffix=True
        )
        if own_suffix is not None:
            return own_suffix

    # Tier 3 — cross-provider fallback. Iterate every other configured
    # provider in insertion order; first int-valued hit wins. Warn on
    # disagreement.
    matches: list[tuple[str, int]] = []
    for other_name, other_provider in providers.items():
        if other_name == spec_provider:
            continue
        value = _lookup_context_length_in_provider(
            other_provider, spec_model, allow_suffix=True
        )
        if value is None:
            continue
        matches.append((other_name, value))

    if not matches:
        return None

    winner_name, winner_value = matches[0]
    disagreements = [
        (name, value) for name, value in matches[1:] if value != winner_value
    ]
    if disagreements:
        conflict_summary = ", ".join(
            f"{name}={value}"
            for name, value in [(winner_name, winner_value), *matches[1:]]
        )
        _log.warning(
            "context_length fallback for %s/%s: candidates disagree (%s); "
            "using %s=%d (first-in-config-order wins)",
            spec_provider,
            spec_model,
            conflict_summary,
            winner_name,
            winner_value,
        )
    return winner_value


def _no_context_length_remediation(
    providers: dict[str, Any] | None,
    spec: Any,
) -> str:
    """Return an operator-facing hint for the ``no_context_length`` skip.

    Same "compute engine-side, thread through the event" pattern
    qol-090 established for tool-execution recovery hints. The chat
    adapter renders the returned string verbatim.

    Two branches, decided by whether any currently-configured
    :class:`llm.protocol.MetadataSourceConnector` provider is present:

    - **Metadata source connected** — recommend
      ``kolsch refresh <spec.provider>`` (qol-091 self-heal picks up
      the missing ``context_length`` on the next refresh).
    - **No metadata source connected** — recommend
      ``kolsch connect openrouter`` OR a direct ``platform.yaml``
      edit to pin ``context_length`` on the specific model.
    """
    spec_provider = getattr(spec, "provider", None) or "?"
    spec_model = getattr(spec, "model", None) or "?"

    has_metadata_source = False
    if providers:
        from llm import connected_metadata_sources  # noqa: PLC0415

        try:
            has_metadata_source = bool(connected_metadata_sources(providers))
        except Exception:  # noqa: BLE001 — hint is best-effort; never raise
            has_metadata_source = False

    if has_metadata_source:
        return (
            f"Run `kolsch refresh {spec_provider}` to backfill "
            f"`context_length` from the connected metadata source."
        )
    return (
        "Connect a metadata source (`kolsch connect openrouter`) to "
        f"auto-populate, OR add `context_length: <N>` under "
        f"`llm_providers.{spec_provider}.models[id={spec_model}]` in "
        "`platform.yaml`."
    )


def _extract_trigger_content(trigger: dict[str, Any]) -> dict[str, Any]:
    """Produce the `user.message.data.content` dict from a trigger."""
    kind = trigger.get("kind")
    if kind in ("user_message", "user_message_with_force_skill"):
        content = trigger.get("content")
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            return {"text": content}
        return {"text": str(content) if content is not None else ""}
    if kind == "invocation":
        # Single-shot: inputs ARE the materialized content.
        inputs = trigger.get("inputs", {})
        return dict(inputs) if isinstance(inputs, dict) else {"text": str(inputs)}
    # Unknown trigger kind — capture verbatim under "text".
    return {"text": str(trigger)}


def _last_user_text(messages: list[Any]) -> str:
    """Return the most recent user message's text from the projection.

    The framework receives the *current* user message as a plain
    string + the rest of the history separately.
    """
    for m in reversed(messages):
        if getattr(m, "role", None) == "user":
            return getattr(m, "content", "") or ""
    return ""


def _history_before_last_user(messages: list[Any]) -> list[Any]:
    """Return everything strictly before the most recent user message.

    Together with :func:`_last_user_text`, this lets the engine hand
    the framework a (system_prompt, history, user_message) triple that
    a no-wrap framework (MinimalFramework) can reassemble byte-equal to
    what the Assembly Procedure produced.
    """
    last_user_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "role", None) == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return []
    return list(messages[:last_user_idx])


def _stable_hash(payload: dict[str, Any]) -> str:
    """Deterministic content hash for config equality + replay."""
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# Re-export for static checkers.
__all__ = ["ENGINE_VERSION", "AgentEngine"]

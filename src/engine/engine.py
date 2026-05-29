"""The :class:`AgentEngine` — Module API Reference §11.

The recorder around the framework, per agent-engine-contract §0:
"The underlying framework (CrewAI in v1) does the agentic work — model
calls, tool dispatch, retry loops. The engine prepares inputs,
registers hooks, observes execution, emits events per the schema,
returns the result."

This file ships the engine wrapping the Framework Protocol from
:mod:`engine.framework`. The default Framework in v1 is
:class:`engine.fake_framework.FakeFramework` (engine-internal). A real
CrewAI adapter ships in Step 12b.

The execute path:

1. :meth:`execute_stream` — the streaming primitive. Spawns an internal
   runner task that walks pre-flight (§2.1) + setup (§2.2) + framework
   driver loop (§2.3-2.4) + finalize (§2.5), pushing events into an
   asyncio.Queue. The generator yields events from the queue as they
   land.
2. :meth:`execute` — collects the stream; returns the final
   :class:`schemas.Event` of type ``agent.message``; raises
   :class:`AgentExecutionError` on Fatal failure.

Per-thread serialization: a lock per active thread ensures the
``parent_event_id`` chain is well-formed even when the framework
invokes the LLM/tool seams from concurrent callbacks. (Agent-engine
spec §2.3 "Note on concurrent callbacks".)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from engine.assembly import ASSEMBLY_VERSION, assemble_context
from engine.context import (
    AgentExecutionError,
    CancelToken,
    EngineCapabilities,
)
from engine.observers import _ObservedLLMHandle, _ObservedToolHandle
from errors import (
    AgentUnauthorized,
    CapabilityMismatch,
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
from skills import format_skills_listing
from tools import ToolContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import crewai

    from assets import AssetManagement
    from bus import EventBus
    from engine.context import ExecutionContext
    from engine.framework import Framework, LLMRequest, LLMResponse
    from llm import ProviderConfig
    from providers import (
        AuthorizationProvider,
        RecentBufferMemoryProvider,
        SandboxProvider,
        SecretsProvider,
    )
    from skills import SkillsSubsystem
    from store import SessionStore
    from tools import ToolsSubsystem


_log = logging.getLogger("engine")

# Sentinel pushed to the stream queue when the runner finishes.
_QUEUE_END: Any = object()

#: Phase 1 engine version — bump when the public contract changes.
ENGINE_VERSION = "0.1"


class AgentEngine:
    """The platform's Agent Engine — per Module API Reference §11.

    Constructor takes every subsystem the engine collaborates with
    (per the dependency matrix in Module API Reference §12) plus the
    chosen :class:`Framework` and an LLM callable. The LLM callable
    is what the engine wraps inside :class:`_ObservedLLMHandle` and
    hands to the framework — for tests, a Fake-LLM adapter; for the
    Step 12b CrewAI adapter, a litellm-backed callable.
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
        # Phase 1.1 — pre-flight step 5.5 acquires a crewai.LLM via
        # `build_llm` when `self._llm is None`. Tests still pass `llm=`
        # directly to bypass this path; production wires
        # `llm_providers_config` so build_llm has something to look up.
        self._llm_providers_config: dict[str, ProviderConfig] = (
            llm_providers_config or {}
        )
        # The acquired crewai.LLM lives on the exchange instance, NOT
        # the engine — each exchange may target a different model.
        # Per-thread lock map. Each execute call shares the lock so
        # concurrent framework callbacks serialize their event-construct
        # paths. The map persists across calls because engine instances
        # are long-lived (I8 — statelessness is about exchange state,
        # not in-process structural state like locks).
        self._thread_locks: dict[str, asyncio.Lock] = {}

    # ---- Public API ----------------------------------------------------

    def capabilities(self) -> EngineCapabilities:
        """Module API Reference §11.1 — declare what this engine can do."""
        return EngineCapabilities(
            engine_version=ENGINE_VERSION,
            assembly_versions=[ASSEMBLY_VERSION],
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
        """Sync convenience — collect the stream, return agent.message.

        Raises :class:`AgentExecutionError` on Fatal failure, carrying
        the ``thread.exchange_failed`` event (spec §1.6).
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
        """Streaming primitive — yield each event as it is emitted.

        Per spec §1.6: this does NOT raise on Fatal failure. The
        failed event is the final yielded item; the iterator ends
        after it.
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
            queue.put_nowait(_QUEUE_END)

    # ---- Internal helpers used by _Exchange ---------------------------

    def _lock_for_thread(self, thread_id: str) -> asyncio.Lock:
        lock = self._thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[thread_id] = lock
        return lock


# ---------------------------------------------------------------------------
# _Exchange — per-execute state holder
# ---------------------------------------------------------------------------


class _Exchange:
    """Owns the state of one execute call.

    Not reusable; a fresh instance per execute. The engine instance
    itself is durable across calls (I8 — statelessness is about
    *exchange* state, not the engine object).
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
        # Wired in pre-flight step 5.5 — see _acquire_llm_if_needed.
        self.acquired_llm: crewai.LLM | None = None
        # Wired in pre-flight step 6.
        self.config_event_id: str | None = None
        # Wired in pre-flight step 8.
        self.exchange_started_id: str | None = None
        # Wired in pre-flight step 9.
        self.user_message_event_id: str | None = None
        # Cancellation token — engine-internal default if caller omitted.
        self.cancel = context.cancellation or CancelToken()
        # Per-thread lock — shared across emits in this exchange.
        self._lock = engine._lock_for_thread(self.thread_id)

    # ---- The orchestrator ---------------------------------------------

    async def run(self) -> None:
        try:
            await self._preflight()
        except PlatformError as exc:
            # Pre-flight Fatal — emit thread.exchange_failed iff we got
            # far enough to know the session_id. If not, the caller
            # gets an empty stream and the wrapper at the boundary
            # raises.
            if self.session_id and self.exchange_started_id is None:
                # Authz / capability denial — spec §2.1 step 3 requires
                # emitting the failed event even without an exchange_started.
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
            wrapped = PlatformInternalError.from_exception(exc)
            await self.emit_exchange_failed(wrapped)
            return

        await self._finalize(agent_text)

    # ---- Pre-flight (spec §2.1) ---------------------------------------

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

        # Step 3 — authorization. principal = user.local per §1.8 v1.
        principal = "user.local"
        action = f"agent.execute.{self.agent_id}"
        # Look up thread/session early so we have session_id when raising.
        thread = await self.engine._store.get_thread(self.context.thread_id)
        self.session_id = str(thread.session_id)

        auth = await self.engine._authz.allow(principal, action)
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
        # + compute the thread's current leaf for parent_event_id wiring.
        # SessionStore.get_leaf is called per-emit; nothing to do here
        # beyond storing the session_id, which we already did.

        # Step 5 — bind memory context, IF the agent has a memory
        # subsystem configured. Phase 1 keeps the wired result on the
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

        # Step 5.5 — acquire LLM client (Phase 1.1 patch §5.5).
        # When the engine was constructed without an `llm` callable,
        # build one from this exchange's ModelSpec via `build_llm`.
        # Tests inject `llm=` directly and skip this seam; production
        # wires `llm_providers_config` so the lookup has something to
        # resolve. The crewai.LLM lives on the exchange because each
        # exchange may target a different model.
        self.acquired_llm = await self._acquire_llm_if_needed()

        # Step 6 — emit config.set if the current effective config
        # differs from the most recent config.set on the thread.
        await self._emit_config_set_if_changed()

        # Register tools with the Tools subsystem now (NOT with the
        # framework — that happens in setup). The synthetic force-invoke
        # read in step 7 routes through tools_subsystem.invoke_tool,
        # which requires registration to have happened.
        await self._register_tools_with_subsystem()

        # Step 7 — process force-invoke skills, if any.
        await self._process_force_invoke()

        # Step 8 — emit thread.exchange_started. The trigger_event_id
        # is what we materialize in step 9; emit a placeholder then
        # patch? Spec says it's the trigger event id. Phase 1 simplest
        # path: pre-allocate the user.message id, emit exchange_started
        # carrying that id, then emit user.message under that id.
        self.user_message_event_id = f"ev_{uuid4()}"
        started = await self._emit(
            type_=EventType.THREAD_EXCHANGE_STARTED,
            data=ThreadExchangeStartedData(
                trigger_event_id=EventId(self.user_message_event_id),
            ).model_dump(mode="json"),
            actor=Actor(kind="system"),
        )
        self.exchange_started_id = str(started.event_id)

        # Step 9 — materialize the trigger as user.message.
        await self._emit(
            type_=EventType.USER_MESSAGE,
            data=UserMessageData(
                content=_extract_trigger_content(self.context.trigger),
            ).model_dump(mode="json"),
            actor=Actor(kind="user", id="user.local"),
            event_id_override=EventId(self.user_message_event_id),
        )

    async def _acquire_llm_if_needed(self) -> crewai.LLM | None:
        """Spec §2.1 step 5.5 — acquire a crewai.LLM via build_llm.

        Skipped (returns ``None``) when the engine was constructed
        with a callable `llm` — that's the test path. The test path
        keeps working unchanged.

        When `engine._llm is None`, this is the production path:

        - Look up the configured provider in
          :attr:`AgentEngine._llm_providers_config` (read from
          platform.yaml at runtime bootstrap).
        - Call :func:`llm.build_llm` with the agent's `ModelSpec` +
          the provider config + secrets.
        - Return the constructed ``crewai.LLM`` for use by the
          framework adapter.

        The framework adapter that turns the ``crewai.LLM`` into the
        engine's ``LLMHandle`` shape ships in a follow-up patch; this
        method only realises the lookup + construction seam.
        """
        if self.engine._llm is not None:
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

    async def _emit_config_set_if_changed(self) -> None:
        """Spec §2.1 step 6 — emit config.set when the effective config
        differs from the most recent config.set on the path (or none)."""
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

    async def _register_tools_with_subsystem(self) -> None:
        """Register builtins/declared tools with the Tools subsystem.

        Re-registration is idempotent — :meth:`ToolsSubsystem.register_for_agent`
        overwrites in-place. We do it on every execute so a config
        change (different tools enabled) takes effect (I8 — no exchange
        state carried).
        """
        tools_sub = self.config.subsystems.get("tools")
        if tools_sub is None:
            return
        body = tools_sub.config
        await self.engine._tools.register_for_agent(
            agent_id=self.agent_id,  # type: ignore[arg-type]
            builtin_names=list(body.get("builtin", []) or []),
        )
        # Declared and MCP tools registered similarly in real configs;
        # the Phase 1 default tests use builtin or programmatic-only.

    async def _process_force_invoke(self) -> None:
        """Spec §2.1 step 7 — for each force-invoke skill, emit a
        synthetic ``tool.called``/``tool.result`` pair for ``read`` on
        the SKILL.md path, with principal=system."""
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
            # Emit synthetic tool.called (principal: system per §1.8).
            await self._emit(
                type_=EventType.TOOL_CALLED,
                data=ToolCalledData(
                    tool_name="read",
                    tool_call_id=tool_call_id,
                    arguments={"path": invocation.skill_md_path},
                ).model_dump(mode="json"),
                actor=Actor(kind="system"),
            )
            # Drive the read tool via the Tools subsystem with
            # principal=system per §1.8.
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

    # ---- Framework setup + driver loop --------------------------------

    async def _run_framework(self) -> str:
        # §2.2 step 10 — build assembled context.
        events = [
            e
            async for e in self.engine._store.read_path_to_leaf(
                self.context.thread_id,
            )
        ]

        # Skills listing — non-disabled skills only (Sk-I2).
        enabled_skills = await self.engine._skills.get_enabled_skills(self.config)
        skills_listing = format_skills_listing(enabled_skills)

        # Tool schemas for the framework.
        registered = self.engine._tools.list_registered_tools(
            self.agent_id,  # type: ignore[arg-type]
        )
        tool_schemas = [
            {"name": t.name, "description": t.description, "schema": t.args_schema}
            for t in registered
        ]

        assembled = assemble_context(
            events,
            skills_listing=skills_listing,
            tool_schemas=tool_schemas,
        )

        # §2.2 step 12 — register observers as the LLM/tool seams the
        # framework will invoke. When `engine._llm` is None, pre-flight
        # step 5.5 acquired a crewai.LLM into `self.acquired_llm`; a
        # follow-up patch wires the crewai.LLM → LLMHandle adapter
        # here. Until then, this path requires a direct LLMHandle
        # callable to have been injected.
        if self.engine._llm is None:
            msg = (
                "engine was constructed without a direct LLMHandle and the "
                "crewai.LLM → LLMHandle adapter is not yet wired. Pass "
                "`llm=` at engine construction, or wait for the follow-up "
                "patch that wires `acquired_llm` into the framework."
            )
            raise PlatformInternalError(
                original_type="engine.llm_adapter_missing",
                args=[msg],
            )
        llm_handle = _ObservedLLMHandle(
            emit=self._emit_void,
            agent_id=self.agent_id,
            model_spec=self.config.model.model_dump(mode="json"),
            config_event_id=self.config_event_id,
            llm=self.engine._llm,
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
            )
            for t in registered
        ]
        # Filter any None defns (shouldn't happen if registration is sound).
        tool_handles = [h for h in tool_handles if h.name]

        # §2.3 — hand to the framework. The user_message text is the
        # most recent user.message's content text projection; history
        # is everything BEFORE it (prior turns from the path).
        user_message_text = _last_user_text(assembled.messages)
        history_messages = _history_before_last_user(assembled.messages)

        # §2.7 — engine-owned retry on Recoverable failures.
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
                    max_iterations=10,
                    cancellation=self.cancel.is_cancelled,
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
        # Spec §2.1 step 3 — pre-flight failures (e.g. authz denial)
        # MUST emit thread.exchange_failed even when no
        # thread.exchange_started preceded. The data shape requires a
        # non-empty EventId; synthesize a placeholder when missing.
        started_id = self.exchange_started_id or f"ev_{uuid4()}"
        await self._emit(
            type_=EventType.THREAD_EXCHANGE_FAILED,
            data=ThreadExchangeFailedData(
                exchange_started_id=EventId(started_id),
                error=err,
            ).model_dump(mode="json"),
            actor=Actor(kind="system"),
        )

    # ---- Emit helpers --------------------------------------------------

    async def _emit(
        self,
        *,
        type_: EventType,
        data: dict[str, Any],
        actor: Actor,
        event_id_override: EventId | None = None,
    ) -> Event:
        """Append + publish + queue. Serialized per thread."""
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
        """Observer-facing emit — returns None to match the
        :class:`Callable[..., Awaitable[None]]` shape the observers
        expect (they don't need the appended event)."""
        await self._emit(type_=type_, data=data, actor=actor)


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _extract_trigger_content(trigger: dict[str, Any]) -> dict[str, Any]:
    """§2.1 step 9 — produce the ``user.message.data.content`` dict."""
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
    a no-wrap framework (FakeFramework) can reassemble byte-equal to
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

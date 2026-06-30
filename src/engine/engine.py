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
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from engine.assembly import ASSEMBLY_VERSION, assemble_context
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
    from engine.context import ExecutionContext
    from engine.framework import Framework, LLMRequest, LLMResponse
    from llm import ProviderConfig
    from providers import (
        AuthorizationProvider,
        RecentBufferMemoryProvider,
        SandboxProvider,
        SecretsProvider,
    )
    from schemas import ModelSpec
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
        llm_for_model: (
            Callable[
                [ModelSpec],
                Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]],
            ]
            | None
        ) = None,
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
        # Phase 1.3 (Step 20) — async factory `llm_for_model` lets the
        # fallback orchestrator lazily build per-attempt LLM clients.
        # Tests can wire a `spec → FakeLLM` map; production wires
        # `build_llm` via the bootstrap (`platform_runtime`). When the
        # factory is None, fall back to wrapping `self._llm` so every
        # spec returns the same callable — backward-compatible with
        # Phase 1.1 / 1.2 tests that pass `llm=` directly.
        self._llm_for_model = llm_for_model
        # Phase 1.1 — pre-flight step 6 acquires a crewai.LLM via
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
            # bug-platform-internal-error-boundary-swallow.md Step 2 (B)
            # — safety-net cause-chain inspection before falling back to
            # PlatformInternalError. The primary translation lives in
            # the framework adapter (A); this catches anything that
            # slipped past (future framework releases, paths the adapter
            # doesn't yet cover). When a known typed exception appears
            # in __cause__, re-classify so the user sees the right
            # surface (e.g., model.upstream_error) instead of an
            # opaque "internal platform error".
            from engine._classify import classify_from_cause_chain  # noqa: PLC0415

            classified = classify_from_cause_chain(exc, agent_id=self.agent_id)
            if classified is not None:
                await self.emit_exchange_failed(classified)
                return
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

        # Phase 3.2 §13.x — pass the proximate-first caller_chain so
        # workflow-spawned invocations surface the workflow→agent frame
        # in the authz audit decision. Chat / API surfaces leave the
        # chain empty and existing behaviour is unchanged.
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

        # Step 6 — acquire LLM client for the primary model (spec §2.1
        # step 6, post-Phase-1.3 FB-R3 retitle). When the engine was
        # constructed without an `llm` callable, build one from this
        # exchange's ModelSpec via `build_llm`. Tests inject `llm=`
        # directly and skip this seam; production wires
        # `llm_providers_config` so the lookup has something to
        # resolve. The crewai.LLM lives on the exchange because each
        # exchange may target a different model.
        self.acquired_llm = await self._acquire_llm_if_needed()

        # Step 7 — resolve fallback chain (Phase 1.3, FB-R3). Each
        # entry's provider must be in `llm_providers`; clients are
        # NOT built here (lazy construction is the engine loop's
        # job per patch §2.1).
        self.fallback_chain = await self._resolve_fallback_chain()

        # Register tools with the Tools subsystem now (NOT with the
        # framework — that happens in setup). The synthetic force-invoke
        # read in step 9 routes through tools_subsystem.invoke_tool,
        # which requires registration to have happened.
        #
        # Phase 2.2 Step 14 — registration is moved BEFORE step 8 so
        # the prompt-render step has access to the registered tool
        # metadata when building the ``tools`` variable.
        await self._register_tools_with_subsystem()

        # Phase 2.2 Step 14 — render the prompt template with the
        # standardized variable context (spec §4 + §3 + agent-engine-
        # contract §5.6.6). Replaces ``self.config.prompt`` with the
        # rendered string so config.set persists the prompt the model
        # actually saw. Plain-text prompts (no Jinja markers) survive
        # the round-trip byte-identical via the StrictUndefined render
        # harness.
        await self._render_prompt_if_needed()

        # Step 8 — emit config.set if the current effective config
        # differs from the most recent config.set on the thread.
        await self._emit_config_set_if_changed()

        # Step 9 — process force-invoke skills, if any.
        await self._process_force_invoke()

        # Step 10 — emit thread.exchange_started. The trigger_event_id
        # is what we materialize in step 11; emit a placeholder then
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
        """Spec §2.1 step 6 — acquire a crewai.LLM via build_llm.

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

        Phase 1.3+ (Step 20 + bug-echo-on-real-provider follow-up): when
        ``llm_for_model`` is wired, the per-spec factory handles
        construction lazily at call time (including ``provider:
        "fake"`` → EchoLLMDriver). The pre-flight acquire becomes
        redundant — skip it. Pre-Phase-1.3 callers that only wire
        ``llm=`` still get the test-path short-circuit at the top of
        this method.
        """
        if self.engine._llm is not None:
            return None
        if self.engine._llm_for_model is not None:
            # Factory handles construction (including provider: fake)
            # at call time; pre-flight acquire would only duplicate
            # work AND fail for `provider: fake` because no `fake`
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
        """Return the per-spec LLM factory the
        :class:`FallbackLLMHandle` uses to lazily construct attempt
        clients.

        Production callers wire ``llm_for_model=`` at engine
        construction (typically a closure around `llm.build_llm` +
        the runtime's `llm_providers_config`). Pre-Phase-1.3 callers
        passing only `llm=` get a degenerate factory that returns
        the same single callable for every spec — fallback still
        works in tests, but every chain entry calls the same
        underlying LLM.
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
        """Spec §2.1 step 7 (Phase 1.3 FB-R3) — resolve + validate the
        agent's fallback chain. Clients are NOT built here.

        - If the agent specifies ``fallback_models`` (even ``[]``),
          that wins per FB-R2 precedence.
        - Otherwise the platform default (``defaults.fallback_models``)
          is used. Phase 1.3 keeps this lookup out of the engine
          (`platform_runtime.bootstrap_runtime` already merged
          defaults into the agent config), so the engine just reads
          ``config.fallback_models``.

        Validates each entry's provider is in ``llm_providers``.
        Emits a `config.fallback_duplicate` warning per duplicate
        (Diagnostic — see Error Model §4.15).
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
        # Phase 1.3 review #3 — surface duplicates visibly. For now
        # the engine forwards them as Diagnostic-level platform-log
        # warnings; a future patch may add a real CLI banner. Tests
        # observe them via `_Exchange.fallback_warnings` (kept for
        # introspection).
        self.fallback_warnings: list[ConfigFallbackDuplicate] = warnings
        for w in warnings:
            _log.warning("%s", str(w))
        return chain

    async def _emit_config_set_if_changed(self) -> None:
        """Spec §2.1 step 8 — emit config.set when the effective config
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

    async def _render_prompt_if_needed(self) -> None:
        """Phase 2.2 Step 14 / spec §3 + §4 — render the agent's
        Jinja2 prompt template with the standardized variable
        context.

        Build the five top-level variables (``tools`` / ``docs`` /
        ``workspace`` / ``agent`` / ``platform``) from the resolved
        config + runtime state, call
        :func:`templating.render.render_prompt`, and replace
        ``self.config`` with a :meth:`pydantic.BaseModel.model_copy`
        carrying the rendered string. A prompt with no Jinja markers
        renders byte-identical to itself, so every Phase 1.x agent
        survives the round-trip untouched.

        Walks the prompt's Jinja AST after render and emits
        ``agent.template_warning`` (Recoverable) for variable
        accesses whose data is empty (e.g. ``{{ tools }}``
        referenced + agent declares no tools) per spec §9.
        """
        prompt = self.config.prompt
        if not isinstance(prompt, str):
            # Layered list-form prompts pre-date Phase 2.2 — engine
            # treats them as fully pre-resolved by the asset
            # manager. Skip the render.
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
            defn = self.engine._tools._registry.get_tool(  # type: ignore[arg-type]
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

        # SkillsVar — qol-skills-jinja-variable-symmetric-with-tools.
        # The Jinja path renders the skills block from a template
        # variable instead of the pre-QoL post-append at
        # ``assembly.py:83-87`` (deleted). ``get_enabled_skills``
        # returns disabled skills too (so force-invoke can route to
        # them — Sk-I2 / spec §6.5); we filter them out here so the
        # model-facing prompt matches the pre-QoL
        # ``format_skills_listing`` behaviour.
        enabled_skills = await self.engine._skills.get_enabled_skills(self.config)
        skill_items: list[SkillItem] = [
            SkillItem(name=s.name, description=s.description, ref=s.ref)
            for s in enabled_skills
            if not s.disable_model_invocation
        ]
        skills_var = SkillsVar(skill_items)

        # DocsVar — load each ContextFileSpec via Step 12.
        from templating.context_files import (  # noqa: PLC0415
            load_context_file,
        )

        workspace_root = Path(self.engine._assets._root)  # type: ignore[attr-defined]
        doc_items: list[DocItem] = []
        for spec in self.config.context_files:
            doc_items.append(
                await asyncio.to_thread(
                    load_context_file, spec, workspace_root
                )
            )
        docs_var = DocsVar(doc_items)

        # WorkspaceVar from platform.yaml.
        platform_cfg = self.engine._assets._platform_config or {}  # type: ignore[attr-defined]
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

        # bug-engine-extends-parent-not-rendered.md Step 2 — three
        # paths for prompt render:
        #
        #   1. orphan: prompt has `{% extends parent %}` but the
        #      YAML has no `extends:` field → typed Fatal so the
        #      author gets a useful error instead of the
        #      loader-less Environment's `TypeError: no loader …`.
        #   2. inheritance: prompt has an `extends:` field → walk
        #      the parent chain via the catalog and render via
        #      `render_with_parents` (DictLoader-backed env).
        #   3. plain: the historical Phase 2.2 path —
        #      `render_prompt` with the no-loader StrictUndefined
        #      env.
        #
        # Order is load-bearing: the orphan check must fire BEFORE
        # `render_prompt` because that path's env has no loader and
        # would crash on the directive.
        template_name = f"agent:{self.config.agent_id}"
        if (
            self.config.extends is None
            and prompt_uses_extends_directive(prompt)
        ):
            raise ConfigExtendsTemplateWithoutInheritance(
                agent_path=template_name,
            )
        if self.config.extends is not None:
            parent_chain_yaml = await asyncio.to_thread(
                resolve_parent_chain_from_extends,
                self.config.extends,
                workspace_root,
                self.engine._assets.catalog(),  # noqa: SLF001
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

        # Phase 2.2 §9 — emit agent.template_warning for empty-data
        # references. Minimal first cut: warn when the prompt
        # references the variable name but the corresponding data is
        # empty. Step 15 can broaden this to the full AST walk.
        from schemas import AgentTemplateWarningData  # noqa: PLC0415

        warnings: list[tuple[str, str, str]] = []
        if "tools" in prompt and tools_var.count == 0:
            warnings.append(
                (
                    "tools",
                    "prompt references {{ tools }} but agent declares "
                    "no tools",
                    "add tools to subsystems.tools.config.declared or "
                    "remove the reference",
                )
            )
        if "docs" in prompt and docs_var.count == 0:
            warnings.append(
                (
                    "docs",
                    "prompt references {{ docs }} but agent declares "
                    "no context_files",
                    "add a context_files: entry or remove the reference",
                )
            )
        # qol-skills-jinja-variable-symmetric-with-tools — symmetric
        # warning for the ``{{ skills }}`` variable.
        if "skills" in prompt and skills_var.count == 0:
            warnings.append(
                (
                    "skills",
                    "prompt references {{ skills }} but agent declares "
                    "no skills",
                    "add a skills entry under subsystems.skills.config.enabled "
                    "or remove the reference",
                )
            )
        # qol-skills-jinja-variable-symmetric-with-tools — migration
        # warning. Pre-QoL, every rendered prompt got an auto-appended
        # ``## Available Skills`` block whether the template asked for
        # it or not. Post-QoL the prompt owns rendering; agents that
        # declare skills but whose template doesn't reference them
        # would silently lose the listing. Fire a Recoverable so the
        # author sees the gap on first agent-load.
        if skills_var.count > 0 and "skills" not in prompt:
            warnings.append(
                (
                    "skills",
                    "agent declares skills but prompt does not reference "
                    "{{ skills }} — the auto-appended listing was removed "
                    "in qol-skills-jinja-variable-symmetric-with-tools.md",
                    "add a `{{ skills }}` block to the prompt template "
                    "(see docs/explanation/tools-subsystem.md §7.5)",
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
        """Register builtins/declared tools with the Tools subsystem.

        Per bug-engine-tools-wiring-gap.md (resolved Step D+E, PR 1):
        resolve every ``subsystems.tools.config.declared[*].ref`` via
        :meth:`AssetManagement.get_asset` to its on-disk Path, then
        pass the collected paths to ``register_for_agent`` alongside
        any builtins. Without this resolution, the helper agent's
        four declared tools (read_file / write_file / list_files /
        bash) never reach the framework — the model gets zero tools
        and hallucinates output.

        Re-registration is idempotent — :meth:`ToolsSubsystem.register_for_agent`
        overwrites in-place. We do it on every execute so a config
        change (different tools enabled) takes effect (I8 — no exchange
        state carried). The asset-management catalog is built once at
        bootstrap so the ``get_asset`` lookups are cheap; the actual
        Python imports happen inside ``discover_tools_in_file`` and
        hit Python's module cache on the second-and-subsequent
        invocations.

        Missing refs raise :class:`ConfigUnknownTool` (Fatal) per
        coordinator resolution to bug-engine-tools-wiring-gap.md Q1
        (option (b)): enforce at engine pre-flight rather than at
        Layer 3 validator. FUL-1 tracks the validator-side
        enforcement for server / lint-style usage.

        MCP servers are still deferred — Phase 2.0 spec §3.3
        documents the shape; engine wiring lands in a later phase.
        """
        tools_sub = self.config.subsystems.get("tools")
        if tools_sub is None:
            return
        body = tools_sub.config

        declared_paths = await self._resolve_declared_tool_paths(
            list(body.get("declared", []) or []),
        )

        # qol-tool-permission-discovery-uniform-with-stacking.md Step 2 —
        # thread the agent's permissions + its home bundle's
        # permissions to the tools subsystem so the dispatcher hands
        # them to ``authz.allow``. The provider walks
        # agent → bundle → platform → no-rule callback → default-deny.
        # PermissionRule is structurally identical to Rule; the
        # provider's compile path accepts either via duck-typing of
        # the .who / .what / .effect fields.
        from providers.authz import Rule  # noqa: PLC0415

        agent_rules: list[Rule] = [
            Rule(id=r.id, who=r.who, what=r.what, effect=r.effect)
            for r in self.config.permissions
        ]
        bundle_rules: list[Rule] = []
        home_bundle = self.engine._assets.get_bundle_for_agent_id(self.agent_id)
        if home_bundle is not None:
            manifest = self.engine._assets.get_bundle(home_bundle)
            if manifest is not None:
                bundle_rules = [
                    Rule(id=r.id, who=r.who, what=r.what, effect=r.effect)
                    for r in manifest.permissions
                ]

        await self.engine._tools.register_for_agent(
            agent_id=self.agent_id,  # type: ignore[arg-type]
            builtin_names=list(body.get("builtin", []) or []),
            declared_paths=declared_paths,
            agent_permissions=agent_rules,
            bundle_permissions=bundle_rules,
            # qol-action-namespace-bundle-scoped.md Step 2 — the same
            # home_bundle resolved above for sourcing bundle_permissions
            # also stamps each ToolDefinition's home_bundle so its
            # action_name resolves to the 3-seg catalog ref
            # ``<home_bundle>/tools/<name>``. Falls through as None for
            # synthetic test configs not backed by a catalog bundle —
            # the subsystem falls back to "kolsch" for builtins and to
            # the legacy 1-seg action form for declared tools in that
            # case (preserves Phase 1 programmatic test behaviour).
            home_bundle=home_bundle,
        )
        # MCP server registration lands when the MCP wiring patch ships.

    async def _resolve_declared_tool_paths(
        self,
        declared_entries: list[dict[str, Any]],
    ) -> list[Path]:
        """Resolve each declared tool's ``ref`` to an on-disk Path.

        The asset catalog (built at runtime bootstrap via
        :meth:`AssetManagement.initialize`) already scanned every
        bundle's ``tools/`` directory and produced an
        :class:`AssetEntry` per file. This method maps each
        config-supplied ref to its catalog entry's ``path`` field.

        Missing refs surface as :class:`ConfigUnknownTool` (Fatal)
        with the available refs listed in the error detail so the
        user sees an actionable error rather than a silent-skip.
        """
        from errors import AssetsNotFound, ConfigUnknownTool  # noqa: PLC0415

        paths: list[Path] = []
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
            paths.append(Path(asset.path))
        return paths

    async def _process_force_invoke(self) -> None:
        """Spec §2.1 step 9 — for each force-invoke skill, emit a
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

        # qol-skills-jinja-variable-symmetric-with-tools — the
        # ``format_skills_listing`` post-append is gone. Skills are
        # rendered into the prompt as the ``{{ skills }}`` Jinja
        # variable during pre-flight (``_render_prompt_if_needed``)
        # so the assembly here only handles the message projection +
        # tool schemas.
        registered = self.engine._tools.list_registered_tools(
            self.agent_id,  # type: ignore[arg-type]
        )
        tool_schemas = [
            {"name": t.name, "description": t.description, "schema": t.args_schema}
            for t in registered
        ]

        assembled = assemble_context(
            events,
            tool_schemas=tool_schemas,
        )

        # §2.2 step 12 — register the LLM/tool seams the framework
        # will invoke.
        #
        # The LLM seam is :class:`FallbackLLMHandle` from Phase 1.3
        # (Step 20): it wraps the per-attempt observation that the
        # pre-1.3 :class:`_ObservedLLMHandle` did, plus the
        # chain loop. The factory we hand it produces a per-attempt
        # ``Callable[[LLMRequest], Awaitable[LLMResponse]]``:
        #
        # - If the engine was constructed with `llm_for_model=`
        #   (the per-spec factory — production wires this via
        #   `platform_runtime`'s build_llm), use it directly. Each
        #   chain entry gets its own client built lazily.
        # - If only `llm=` was passed (Phase 1.1/1.2 tests + the default-tier
        #   EchoLLMDriver bootstrap), wrap it so every spec returns
        #   the same callable. The chain still works; just every
        #   entry calls the same underlying llm.
        # - If neither, raise PlatformInternalError — the engine
        #   has no way to make an LLM call.
        llm_factory = self._build_llm_factory()
        from engine._fallback import FallbackLLMHandle, StreamState  # noqa: PLC0415

        # Phase 1.3 patch §2.3 — the orchestrator consults this
        # StreamState before advancing the chain on a Recoverable
        # error. Once a streaming connector marks
        # `mark_content_emitted()`, fallback is short-circuited and
        # the engine emits `thread.exchange_failed` with the
        # truncation visible. Non-streaming connectors (today: all
        # of them) never set the flag, so behaviour is unchanged.
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
                # bug-mid-tool-cancellation-latency.md — thread the
                # exchange's CancelToken so tool bodies receive it
                # via ``ctx.cancellation``.
                cancel=self.cancel,
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
                    cancellation=self.cancel.is_cancelled,
                    # bug-framework-cancellation-mid-call-latency.md —
                    # the raw token gives the framework access to
                    # ``wait_cancelled()`` so it can race in-flight
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
            except Exception as exc:  # noqa: BLE001 — in-loop classifier
                # bug-platform-internal-error-boundary-swallow.md 0.3
                # — IN-LOOP cause-chain classifier (the primary fix per
                # 0.3). 0.2 put the classifier at the outer boundary,
                # which correctly re-typed non-PlatformErrors but ran
                # too late — the retry loop had already exited. With
                # this block, a transport failure whose ``__cause__``
                # carries a known typed exception (e.g.
                # ``openai.APITimeoutError`` through CrewAI's
                # adapter's ``raise ConnectionError(...) from e``)
                # classifies as ``ModelUpstreamError`` (Recoverable)
                # and the loop retries up to ``max_retries+1`` attempts
                # before exhausting. ``runtime.max_retries`` finally
                # means something for transport hiccups.
                #
                # Branch table:
                #   Recoverable match → append to attempts_data,
                #                       continue (mirrors the
                #                       PlatformError-Recoverable
                #                       branch above).
                #   Fatal match       → ``raise classified from exc``
                #                       so the outer ``except
                #                       PlatformError`` at run() picks
                #                       it up as the typed surface
                #                       (e.g. ``agent.unauthorized``
                #                       instead of
                #                       ``platform.internal_error``).
                #   No match          → bare ``raise``; the outer
                #                       boundary's cause-chain
                #                       safety net (0.2's wiring at
                #                       engine.py:364-385) takes
                #                       over, ultimately falling
                #                       through to
                #                       PlatformInternalError for
                #                       genuine engine bugs.
                from engine._classify import (  # noqa: PLC0415
                    classify_from_cause_chain,
                )

                classified = classify_from_cause_chain(
                    exc, agent_id=self.agent_id
                )
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
        """Append + publish + queue. Serialized per thread.

        Phase 3.2 §11.6.4 — every event the engine emits carries the
        :attr:`ExecutionContext.trace_id` set by the caller (the workflow
        agent runner stamps it from the workflow run's ``trace_id``).
        Chat / API surfaces leave it ``None`` and the envelope field
        stays ``None``; the cross-JSONL correlation is opt-in at the
        ``ExecutionContext`` level, but the engine's stamping is
        unconditional so a callsite that DOES set it can't accidentally
        forget to surface it.
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
        """Observer-facing emit — returns None to match the
        :class:`Callable[..., Awaitable[None]]` shape the observers
        expect (they don't need the appended event)."""
        await self._emit(type_=type_, data=data, actor=actor)


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _extract_trigger_content(trigger: dict[str, Any]) -> dict[str, Any]:
    """§2.1 step 11 — produce the ``user.message.data.content`` dict."""
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

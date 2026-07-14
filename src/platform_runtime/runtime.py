"""The shared composition root — `PlatformRuntime` and `bootstrap_runtime`.

One place that knows how to instantiate every subsystem and wire them
into an [`AgentEngine`][engine.AgentEngine]. The CLI and the API each
construct one of these at startup and route their commands through it.

The default tier — SQLite entity backend, in-process event bus,
dotenv secrets, native sandbox, rule-based authz, recent-buffer memory,
`MinimalFramework`, and `EchoLLMDriver` — runs end-to-end with zero
external services. Production frameworks (`CrewAIFramework`) and
real-LLM providers are opt-in upgrades injected at bootstrap.

See the [v1 scope](../explanation/v1-scope.md) for the
default-tier contract and the
[agent engine contract](../explanation/agent-engine-contract.md)
for the engine-side wiring.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from assets import AssetManagement
from bus import EventBus, InProcessEventBus
from engine import (
    AgentEngine,
    LLMRequest,
    LLMResponse,
    MinimalFramework,
)
from engine.dev_llm import EchoLLMDriver
from engine.framework import Framework
from files import PathPolicy, load_policy
from providers import (
    AuthorizationProvider,
    ContextCompactionProvider,
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
    SandboxProvider,
    SecretsProvider,
    make_compaction_provider,
    validate_recent_buffer_config,
    validate_reference_summary_config,
)
from providers.secrets import DotenvSecretsProvider
from schemas import ResolvedAgentConfig
from skills import SkillsSubsystem, make_standard_validator
from store import SessionStore, SqliteBackend
from tools import ToolsSubsystem, validate_standard_config

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from typing import Any

    from kernel import ConfigValidator
    from llm import ProviderConfig
    from schemas import ModelSpec


@dataclass(frozen=True)
class PlatformRuntime:
    """The bundle of wired subsystems and engine the CLI and API consume.

    Frozen because once built, the composition shouldn't shift under a
    running process. Construct via
    [`bootstrap_runtime`][platform_runtime.runtime.bootstrap_runtime].

    `subsystem_validators` is the Layer 3 validator dispatch dict —
    keyed by subsystem name, then variant name — defined by the
    [kernel resolver contract](../explanation/kernel-resolver-contract.md).
    Both CLI and API route config builds through `build_resolved_config`
    so wildcard write_tags, unknown variants, and missing skill refs
    fail at config-resolution time rather than mid-exchange.
    """

    root: Path
    asset_management: AssetManagement
    store: SessionStore
    bus: EventBus
    secrets: SecretsProvider
    sandbox: SandboxProvider
    authz: AuthorizationProvider
    memory: RecentBufferMemoryProvider
    tools_subsystem: ToolsSubsystem
    skills_subsystem: SkillsSubsystem
    path_policy: PathPolicy
    engine: AgentEngine
    subsystem_validators: dict[str, dict[str, ConfigValidator]] = field(
        default_factory=dict,
    )
    # feat-054: kept on the runtime so the context-compaction pre-flight
    # can look up the model's declared context window via
    # `context_length_for(spec)`. `None` when compaction is disabled.
    compaction_provider: ContextCompactionProvider | None = None
    # Cached ``llm_providers`` slice from ``platform.yaml``. The engine
    # already carries a copy for fallback dispatch; the runtime keeps
    # its own reference so :meth:`context_length_for` can answer
    # without reaching into the engine's private state.
    llm_providers_config: dict[str, ProviderConfig] = field(default_factory=dict)
    # Default `run_workflow` blocking timeout (seconds). Populated at
    # bootstrap from `platform.yaml:workflows.default_timeout_seconds`;
    # 300 preserves the pre-existing bug-031 default when the section
    # is absent. WorkflowsImpl reads this at construction time and
    # exposes it to tools via `Workflows.default_timeout_seconds`.
    workflow_default_timeout_seconds: int = 300

    async def build_resolved_config(
        self,
        agent_ref: str,
        call_overrides: dict[str, Any] | None = None,
    ) -> ResolvedAgentConfig:
        """Resolve an agent's config with the runtime's validators wired.

        Wraps
        [`AssetManagement.build_resolved_config`][assets.asset_manager.AssetManagement.build_resolved_config]
        and pins `subsystem_validators` so wildcard write_tags,
        unknown builtin tools, and missing skill refs surface at
        config-resolution time. CLI and API callers MUST route through
        this method rather than the bare asset-management call so the
        Layer 3 validators always fire.

        Args:
            agent_ref: The catalog ref of the agent to resolve.
            call_overrides: Per-call overrides merged on top of the
                resolved config — `None` for no overrides.

        Returns:
            The resolved agent config, ready to hand to
            [`AgentEngine.execute`][engine.engine.AgentEngine.execute].
        """
        return await self.asset_management.build_resolved_config(
            agent_ref,
            call_overrides=call_overrides,
            subsystem_validators=self.subsystem_validators,
        )

    def context_length_for(self, spec: ModelSpec) -> int | None:
        """Return the declared ``context_length`` (tokens) for ``spec``, or ``None``.

        Walks :attr:`llm_providers_config` for a provider matching
        ``spec.provider`` and looks up an entry in that provider's
        ``models`` list whose ``id`` matches ``spec.model``. Returns
        the entry's ``context_length`` when the model declares one.

        Returns ``None`` when:

        - The provider is not present in ``llm_providers_config``
          (e.g. ``spec.provider == "mock"`` on the default tier).
        - No model entry matches ``spec.model``.
        - The matching entry has ``context_length: null`` (many
          ``opencode`` models legitimately don't declare one).

        The compaction pre-flight (feat-054, lands in Phase 2) uses
        this to compute the trigger budget. ``None`` is a signal to
        skip compaction for the current exchange and emit a diagnostic
        rather than hard-failing.
        """
        provider = self.llm_providers_config.get(spec.provider)
        if provider is None:
            return None
        for entry in provider.models:
            # `ProviderConfig.models` is typed as `list[dict[str, Any]]`.
            # A malformed non-dict slipped in at load time would raise
            # `AttributeError` here — that's the correct signal (bug in
            # config parsing) rather than silently masking the miss.
            if entry.get("id") != spec.model:
                continue
            value = entry.get("context_length")
            # `bool` is a subclass of `int`; reject it before the int
            # check so `context_length: true` does not become `1`.
            if isinstance(value, bool):
                return None
            if isinstance(value, int):
                return value
            return None
        return None


async def bootstrap_runtime(
    *,
    root: Path,
    framework: Framework | None = None,
    llm: Callable[[LLMRequest], Awaitable[LLMResponse]] | None = None,
) -> PlatformRuntime:
    """Stand up the default-tier `PlatformRuntime` rooted at `root`.

    Default framework is
    [`MinimalFramework`][engine.MinimalFramework] regardless of whether
    a real LLM factory is present: kolsch's shipped default is the
    minimal message/tool-dispatch loop, not CrewAI. Users who need
    production affordances only `CrewAIFramework` provides —
    cancellation-during-hang polling, `ModelUpstreamError` retry
    translation, stdout/stderr capture around CrewAI's noisy
    singletons, tracing-consent short-circuit — pass
    `framework=CrewAIFramework()` explicitly.

    Rationale for the default: `MinimalFramework` has no third-party
    agent-lib dependency, no wrapping semantics, and the smallest
    possible surface between the engine and the LLM. The trade-offs
    listed above are documented so users can make an informed choice
    per workspace. See `process/feat-clarify-framework-selection.md`
    for the historical context (this class was renamed from
    `FakeFramework` to `MinimalFramework`, which misled users into
    treating it as a test fixture; the containing module was later
    renamed from `fake_framework.py` to `mock_framework.py` — see
    `process/qol-046-fake-to-mock-rename.md`).

    Default `llm` is [`EchoLLMDriver`][engine.dev_llm.EchoLLMDriver]
    when no `llm_providers` are configured, so a fresh install runs
    end-to-end without credentials. When providers ARE configured
    (`llm_providers` in `platform.yaml`), the runtime wires a real
    per-model factory via `make_build_llm_factory`.

    Args:
        root: The workspace root — every subsystem's on-disk state
            (entities DB, JSONL store, dotenv secrets) sits beneath
            this path.
        framework: Optional framework override. Defaults to
            `MinimalFramework`; pass `CrewAIFramework()` for the
            production affordances documented above.
        llm: Optional explicit LLM callable. When set, takes priority
            over the per-model factory wired from `llm_providers`
            config — test paths that pin a single callable continue
            to work unchanged.

    Returns:
        A fully wired `PlatformRuntime` ready to serve CLI or API
        callers.
    """
    am = AssetManagement(root=root)
    await am.initialize()

    secrets_cfg = am.get_platform_config("secrets")
    sandbox_cfg = am.get_platform_config("sandbox")
    authz_cfg = am.get_platform_config("authorization")
    file_access_cfg = am.get_platform_config("file_access")

    secrets = DotenvSecretsProvider(root=root, config=secrets_cfg.get("config", {}))
    sandbox = NativeSandboxProvider(config=sandbox_cfg.get("config", {}))
    # feat-041 — sidecar source-line metadata for the platform-layer
    # rules. Loader routes through `_yamlio.load_with_positions`, so
    # every rule the provider compiles carries `(source_file,
    # source_line)` and `/why` + the inline denial hint can display
    # `~/.kolsch/platform.yaml:<line>` breadcrumbs.
    from assets.platform_config import (  # noqa: PLC0415
        authorization_rule_source_map,
    )

    authz_source_map = authorization_rule_source_map(root)
    # Pass the catalog so the provider can validate qualified tool refs
    # in rule `what:` fields AND build the action→tool_ref map so
    # `what: kolsch/bash` matches the `tool.bash` action namespace.
    authz = RuleBasedAuthorizationProvider(
        config=authz_cfg.get("config", {}),
        catalog=am.catalog(),
        source_map=authz_source_map,
    )
    path_policy = load_policy(file_access_cfg)

    storage_cfg = am.get_platform_config("storage")
    db_path = root / storage_cfg.get("entity_path", ".platform/entities.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backend = SqliteBackend(db_path)
    await backend.initialize()
    store = SessionStore(root=root, backend=backend)

    memory = RecentBufferMemoryProvider(store=store)
    # feat-054: build the compaction provider from platform.yaml. The
    # section is optional; when missing (the default), the factory
    # returns `None` and the engine skips the trigger check entirely.
    try:
        compaction_cfg = am.get_platform_config("compaction")
    except KeyError:
        compaction_cfg = {}
    bus = InProcessEventBus()

    tools_sub = ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        path_policy=path_policy,
        root=root,
    )
    skills_sub = SkillsSubsystem(asset_management=am)

    chosen_framework = framework or MinimalFramework()

    # Load the `llm_providers` section (if present) into the engine so
    # pre-flight can dispatch to build_llm when an exchange needs a
    # fresh client. Importing `llm.providers` here also triggers
    # connector registration for the reference providers (OpenCode,
    # OpenRouter). The import-by-name form avoids binding `llm` as a
    # local name that would shadow this function's `llm` parameter —
    # the side-effect is what we want.
    importlib.import_module("llm.providers")
    from llm import ProviderConfig  # noqa: PLC0415

    llm_providers_config: dict[str, ProviderConfig] = {}
    try:
        raw_llm = am.get_platform_config("llm_providers")
    except KeyError:
        raw_llm = {}
    for provider_name, raw in (raw_llm or {}).items():
        if not isinstance(raw, dict):
            continue
        # `type:` defaults to the provider dictionary key (e.g. an
        # entry keyed `opencode` with no explicit `type:` resolves to
        # the `opencode` connector). Explicit `type:` still wins —
        # only set it when the connector type differs from the key
        # you chose. See bug-055.
        llm_providers_config[provider_name] = ProviderConfig(
            name=provider_name,
            type=raw.get("type") or provider_name,
            api_key_secret=raw.get("api_key_secret"),
            base_url=raw.get("base_url"),
            timeout_seconds=raw.get("timeout_seconds"),
            extras={
                k: v
                for k, v in raw.items()
                if k
                not in {
                    "type",
                    "api_key_secret",
                    "base_url",
                    "timeout_seconds",
                    "models",
                }
            },
            models=list(raw.get("models") or []),
        )

    # Wire build_llm via `llm_for_model=` so an agent whose ModelSpec
    # points at a real provider actually reaches that provider.
    # Decision tree:
    #
    # - Caller passed `llm=` explicitly (test path) → honour it, skip
    #   the factory. Pre-existing tests that pin a single callable
    #   continue to work unchanged.
    # - Otherwise and `llm_providers_config` is populated → wire
    #   `llm_for_model=` to the per-spec factory (`provider == "mock"`
    #   still gets EchoLLMDriver from inside the factory, preserving
    #   the no-credentials tutorial behaviour).
    # - Otherwise (no providers AND no explicit `llm=`) → fall back to
    #   EchoLLMDriver across the board, matching the default-tier
    #   "fresh install runs end-to-end without credentials" guarantee.
    chosen_llm: Callable[[LLMRequest], Awaitable[LLMResponse]] | None = llm
    chosen_llm_for_model: (
        Callable[[ModelSpec], Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]]]
        | None
    ) = None
    if chosen_llm is None and llm_providers_config:
        from engine._crewai_adapter import make_build_llm_factory  # noqa: PLC0415

        chosen_llm_for_model = make_build_llm_factory(
            llm_providers_config=llm_providers_config,
            secrets=secrets,
        )
    elif chosen_llm is None:
        chosen_llm = EchoLLMDriver().__call__

    # feat-054 Phase 1: construct the compaction provider (if any) so
    # the engine holds a reference. The invocation site lands in
    # Phase 2. `llm_for_model` is passed through so the v1 variant can
    # dispatch to a summariser model of its choice.
    compaction_provider: ContextCompactionProvider | None = (
        make_compaction_provider(
            config=compaction_cfg,
            llm_for_model=chosen_llm_for_model,
        )
        if compaction_cfg
        else None
    )

    engine = AgentEngine(
        store=store,
        bus=bus,
        asset_management=am,
        tools_subsystem=tools_sub,
        skills_subsystem=skills_sub,
        memory_provider=memory,
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        framework=chosen_framework,
        llm=chosen_llm,
        llm_for_model=chosen_llm_for_model,
        llm_providers_config=llm_providers_config,
        compaction_provider=compaction_provider,
    )

    # Per-subsystem Layer 3 validators — dispatch shape: subsystem
    # name → variant name → validator. Adding a new subsystem variant
    # here is the one-line lever to make its validator fire on every
    # `build_resolved_config` call.
    subsystem_validators: dict[str, dict[str, ConfigValidator]] = {
        "memory": {"recent_buffer": validate_recent_buffer_config},
        "tools": {"standard": validate_standard_config},
        "skills": {"standard": make_standard_validator(am)},
        "compaction": {"reference_summary": validate_reference_summary_config},
    }

    # Workflows section (optional). Currently one knob:
    # `default_timeout_seconds` — how long `run_workflow` blocks.
    try:
        workflows_cfg = am.get_platform_config("workflows")
    except KeyError:
        workflows_cfg = {}
    workflow_default_timeout_seconds = int(
        (workflows_cfg or {}).get("default_timeout_seconds", 300)
    )

    return PlatformRuntime(
        root=root,
        asset_management=am,
        store=store,
        bus=bus,
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        memory=memory,
        tools_subsystem=tools_sub,
        skills_subsystem=skills_sub,
        path_policy=path_policy,
        engine=engine,
        subsystem_validators=subsystem_validators,
        compaction_provider=compaction_provider,
        llm_providers_config=llm_providers_config,
        workflow_default_timeout_seconds=workflow_default_timeout_seconds,
    )

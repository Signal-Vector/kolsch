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
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
    SandboxProvider,
    SecretsProvider,
    validate_recent_buffer_config,
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
    `FakeFramework`, which misled users into treating it as a test
    fixture).

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
    # Pass the catalog so the provider can validate qualified tool refs
    # in rule `what:` fields AND build the action→tool_ref map so
    # `what: kolsch/bash` matches the `tool.bash` action namespace.
    authz = RuleBasedAuthorizationProvider(
        config=authz_cfg.get("config", {}),
        catalog=am.catalog(),
    )
    path_policy = load_policy(file_access_cfg)

    storage_cfg = am.get_platform_config("storage")
    db_path = root / storage_cfg.get("entity_path", ".platform/entities.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backend = SqliteBackend(db_path)
    await backend.initialize()
    store = SessionStore(root=root, backend=backend)

    memory = RecentBufferMemoryProvider(store=store)
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
        llm_providers_config[provider_name] = ProviderConfig(
            name=provider_name,
            type=raw.get("type", ""),
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
    #   `llm_for_model=` to the per-spec factory (`provider == "fake"`
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
    )

    # Per-subsystem Layer 3 validators — dispatch shape: subsystem
    # name → variant name → validator. Adding a new subsystem variant
    # here is the one-line lever to make its validator fire on every
    # `build_resolved_config` call.
    subsystem_validators: dict[str, dict[str, ConfigValidator]] = {
        "memory": {"recent_buffer": validate_recent_buffer_config},
        "tools": {"standard": validate_standard_config},
        "skills": {"standard": make_standard_validator(am)},
    }

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
    )

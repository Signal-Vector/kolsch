"""PlatformRuntime — the shared composition root.

One place that knows how to instantiate every Phase 1 subsystem +
wire them into an :class:`engine.AgentEngine`. The CLI and the API
each construct one of these at startup and route their commands
through it.

Default tier (v1-scope §2.4): SQLite entity backend + in-process
event bus + dotenv secrets + native sandbox + rule-based authz +
recent-buffer memory + FakeFramework + EchoLLM. **Zero external
services required.** Real production frameworks (CrewAIFramework)
and providers (real LLMs, Postgres, Redis) are opt-in upgrades.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from assets import AssetManagement
from bus import EventBus, InProcessEventBus
from engine import (
    AgentEngine,
    FakeFramework,
    LLMRequest,
    LLMResponse,
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
    """The bundle of wired subsystems + engine the CLI + API consume.

    Frozen because once built, the composition shouldn't shift under
    a running process. Construct via :func:`bootstrap_runtime`.

    ``subsystem_validators`` is the Layer 3 dispatch dict per the
    Kernel contract (§4.1) — keyed by subsystem name, then variant
    name. Both CLI and API MUST pass this through
    :meth:`build_resolved_config` so wildcard write_tags, unknown
    variants, etc. fail at config-resolution time (Steps 8/10/11
    landed the validators specifically to surface these errors here,
    not at runtime).
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
        """Wrap :meth:`AssetManagement.build_resolved_config` with the
        runtime's Layer 3 validators pre-wired.

        Centralising the validator dispatch here closes the Step 13
        review #1 gap — CLI and API were calling
        ``asset_management.build_resolved_config`` directly without
        validators, so memory's wildcard-write_tag (MEM I5), tools'
        unknown-builtin checks, and skills' ref-existence checks were
        all silently skipped on the production paths. Routing every
        config build through this method makes that impossible.
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
    """Stand up the v1 default tier rooted at ``root``.

    ``framework`` and ``llm`` default to :class:`FakeFramework` +
    :class:`EchoLLMDriver` so a fresh install runs end-to-end without
    any LLM keys. Production callers (Step 13+ deployments) inject
    :class:`CrewAIFramework` and a real-LLM callable here.
    """
    am = AssetManagement(root=root)
    await am.initialize()

    secrets_cfg = am.get_platform_config("secrets")
    sandbox_cfg = am.get_platform_config("sandbox")
    authz_cfg = am.get_platform_config("authorization")
    file_access_cfg = am.get_platform_config("file_access")

    secrets = DotenvSecretsProvider(root=root, config=secrets_cfg.get("config", {}))
    sandbox = NativeSandboxProvider(config=sandbox_cfg.get("config", {}))
    # qol-permission-system-human-readable.md Step 2 — pass the
    # catalog so the provider can validate qualified tool refs in
    # rule `what:` fields AND build the action→tool_ref map so
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

    chosen_framework = framework or FakeFramework()

    # Phase 1.1 — load the `llm_providers` section (if present) into
    # the engine so pre-flight step 5.5 can dispatch to build_llm
    # when an exchange needs a fresh client. Importing
    # :mod:`llm.providers` here also triggers connector registration
    # for the two reference providers (OpenCode, OpenRouter).
    # NOTE: cannot bind `import llm.providers` to a name that shadows
    # this function's `llm` parameter; the import-by-name form is
    # enough — the side-effect is what we want.
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

    # Phase 1.1 deferred work (bug-echo-on-real-provider, hypotheses
    # H1+H3) — wire build_llm via `llm_for_model=` so an agent whose
    # ModelSpec points at a real provider actually reaches the real
    # provider. Decision tree:
    #
    # - Caller passed `llm=` explicitly (test path) → honour it,
    #   skip the factory. Pre-existing tests that pin a single
    #   callable continue to work unchanged.
    # - Otherwise and `llm_providers_config` is populated → wire
    #   `llm_for_model=` to the per-spec factory (`provider == "fake"`
    #   still gets EchoLLMDriver from inside the factory, preserving
    #   the no-creds tutorial behaviour the bug ticket's AC #3
    #   requires).
    # - Otherwise (no providers AND no explicit llm=) → fall back to
    #   EchoLLMDriver across the board, matching the original
    #   v1-default-tier "fresh install runs end-to-end without
    #   credentials" guarantee.
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

    # Per-subsystem Layer 3 validators (kernel §4.1 dispatch shape:
    # subsystem name → variant name → validator). Adding a new
    # subsystem variant here is the one-line lever to make its
    # validator fire on every build_resolved_config call.
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

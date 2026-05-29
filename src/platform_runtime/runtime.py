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

from dataclasses import dataclass
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
)
from providers.secrets import DotenvSecretsProvider
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tools import ToolsSubsystem

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


@dataclass(frozen=True)
class PlatformRuntime:
    """The bundle of wired subsystems + engine the CLI + API consume.

    Frozen because once built, the composition shouldn't shift under
    a running process. Construct via :func:`bootstrap_runtime`.
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
    authz = RuleBasedAuthorizationProvider(config=authz_cfg.get("config", {}))
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
    chosen_llm = llm or EchoLLMDriver().__call__

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
    )

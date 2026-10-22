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
import logging
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
from engine.subprocess_framework import ClaudeCodeHarness, SubprocessAgentFramework
from files import PathPolicy, load_policy
from platform_runtime.hooks import HookDispatcher
from providers import (
    AuthorizationProvider,
    ContextCompactionProvider,
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
    SandboxProvider,
    SecretsProvider,
    describe_default_deny_tail_problem,
    inspect_default_deny_tail,
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

    from engine import LLMFactory
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
    # spec-069 Part A — default PER-NODE execution timeout (seconds)
    # threaded into `IterativeExecutor` so a stalled node
    # (agent stream never yields, LLM stall the token can't cooperatively
    # unwind, etc.) is hard-cancelled via `asyncio.wait_for`. Populated
    # at bootstrap from `platform.yaml:workflows.default_node_timeout_seconds`;
    # 240s default leaves comfortable headroom under the 300s
    # `run_workflow` long-poll (see
    # `_starter_bundle/kolsch/tools/run_workflow.py:_DEFAULT_TIMEOUT_SECONDS`)
    # so the node's own timeout fires BEFORE the poll returns
    # "still running" — otherwise the wedge shape from spec-069's
    # Background (defect 1) recurs. `None` disables the default
    # (raw runner runs unwrapped, matching pre-spec-069 behaviour).
    workflow_default_node_timeout_seconds: float | None = 240.0
    # bug-114 — approval-response window (seconds) that bounds an
    # approval prompt raised INSIDE a workflow node. On expiry the
    # prompt auto-resolves to a deny (via the normal answered bridge)
    # instead of the node's compute timeout absorbing the wait and
    # dying with an opaque `NodeTimeoutExceeded`. Populated at bootstrap
    # from `platform.yaml:workflows.approval_response_seconds`. `None`
    # (default) = OFF: unbounded wait, exactly the pre-bug-114 behavior.
    # Applies only to workflow-scoped prompts (chat approvals unchanged).
    workflow_approval_response_seconds: float | None = None
    # bug-086 — default per-LLM-call timeout (seconds) that wraps
    # each `crewai.LLM.call` in `asyncio.wait_for`. Populated at
    # bootstrap from `platform.yaml:workflows.llm_call_timeout_seconds`;
    # when absent, derived as `max(configured provider timeout_seconds)
    # + 30s margin` so the cap never undercuts a purpose-tuned
    # provider timeout (spec-069's original 120s constant did — see
    # bug-086 Background). `None` disables the wrap (unbounded call).
    # The `KOLSCH_LLM_CALL_TIMEOUT_SECONDS` env var still overrides
    # per-process; this field is what bootstrap installed into the
    # engine adapter via `set_llm_call_timeout_default`, kept here
    # for observability + parity with the sibling timeout fields.
    llm_call_timeout_seconds: float | None = 210.0
    # spec-131 Step 9 — the same `HookDispatcher` instance passed into
    # `AgentEngine(observer=...)` below, ALSO stashed here so CLI-layer
    # code (e.g. `PromptsImpl._append_ui_event`) has one clean,
    # intentional, shared reference point instead of reaching into
    # `AgentEngine`'s private `_observer` attribute. `None` only when a
    # `PlatformRuntime` is hand-constructed outside `bootstrap_runtime`
    # (e.g. some test doubles) without wiring one — `bootstrap_runtime`
    # itself always sets a real `HookDispatcher`, even with zero
    # configured rules (see the field's construction site below).
    hook_dispatcher: HookDispatcher | None = None

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

        Delegates to the engine's :func:`engine._context_length_for_spec`,
        which walks :attr:`llm_providers_config` for the caller's own
        provider first (direct and vendor-prefix suffix match), then
        falls back to every other configured provider (typically a
        metadata source like OpenRouter) so a short-form entry in one
        provider can borrow ``context_length`` from a vendor-prefixed
        entry in another. See qol-091.

        Returns ``None`` when:

        - The provider is not present in ``llm_providers_config``
          (e.g. ``spec.provider == "mock"`` on the default tier —
          the mock provider short-circuits fallback intentionally).
        - No model entry matches ``spec.model`` in any configured
          provider.
        - The matching entry has ``context_length: null`` (many
          ``opencode`` models legitimately don't declare one, and no
          other configured provider supplies a value either).

        The compaction pre-flight uses this to compute the trigger
        budget. ``None`` is a signal to skip compaction for the current
        exchange and emit a diagnostic rather than hard-failing.
        """
        from engine.engine import _context_length_for_spec  # noqa: PLC0415

        return _context_length_for_spec(self.llm_providers_config, spec)


def _resolve_llm_call_timeout_default(
    *,
    workflows_cfg: dict[str, Any] | None,
    llm_providers_config: dict[str, ProviderConfig],
) -> float | None:
    """Resolve the default LLM-call timeout for bug-086.

    Resolution order (env var overrides this at read time — see
    ``engine._crewai_adapter._read_llm_call_timeout_seconds``):

    * ``workflows.llm_call_timeout_seconds`` present and ``null`` →
      ``None`` (explicit opt-out; unbounded call).
    * present and numeric → ``float(value)``.
    * absent → derive ``max(provider.timeout_seconds) + 30s`` across
      providers that declare a timeout so the cap never undercuts
      a purpose-tuned provider timeout (the spec-069 regression
      bug-086 fixes). Falls back to 210s (= opencode's tuned 180s
      + 30s margin) when no providers declare a timeout.
    """
    if workflows_cfg is not None and "llm_call_timeout_seconds" in workflows_cfg:
        raw = workflows_cfg["llm_call_timeout_seconds"]
        return None if raw is None else float(raw)
    provider_timeouts = [
        provider.timeout_seconds
        for provider in llm_providers_config.values()
        if provider.timeout_seconds is not None
    ]
    if provider_timeouts:
        return float(max(provider_timeouts)) + 30.0
    return 210.0


def _resolve_approval_window(
    workflows_cfg: dict[str, Any] | None,
) -> float | None:
    """Resolve `workflows.approval_response_seconds` for bug-114.

    OFF by default: key absent OR explicitly ``null`` → ``None``
    (unbounded approval wait, the pre-bug-114 behavior). A number →
    ``float(value)`` — the seconds an approval prompt raised inside a
    workflow node may sit unanswered before auto-resolving to a deny.
    """
    if workflows_cfg is None or "approval_response_seconds" not in workflows_cfg:
        return None
    raw = workflows_cfg["approval_response_seconds"]
    return None if raw is None else float(raw)


_log = logging.getLogger("platform_runtime")

# bug-152 — workspace roots (resolved) already warned about a missing /
# misplaced default-deny tail in this process. Bootstrap-only on
# purpose: the chat `/reload` rebuild and every other direct
# `RuleBasedAuthorizationProvider(...)` construction stay silent, so a
# stderr write can never land while the inline Textual app is running.
_TAIL_WARNED_ROOTS: set[Path] = set()


def _warn_if_default_deny_tail_missing(root: Path, authz_config: object) -> None:
    """Log one WARNING per process per root when the tail is missing/not last.

    Observability only — never refuses, never edits rules. `authz_config`
    is the `authorization.config` mapping handed to the provider; a
    non-mapping config or non-list `rules` is skipped (the provider owns
    validating those).
    """
    rules = authz_config.get("rules") if isinstance(authz_config, dict) else None
    if not isinstance(rules, list):
        return
    message = describe_default_deny_tail_problem(inspect_default_deny_tail(rules))
    if message is None:
        return
    key = root.resolve()
    if key in _TAIL_WARNED_ROOTS:
        return
    _TAIL_WARNED_ROOTS.add(key)
    _log.warning("%s: %s", key / "platform.yaml", message)


async def bootstrap_runtime(  # noqa: PLR0915 — linear runtime-assembly sequence
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
    authz_config = authz_cfg.get("config", {})
    authz = RuleBasedAuthorizationProvider(
        config=authz_config,
        catalog=am.catalog(),
        source_map=authz_source_map,
    )
    _warn_if_default_deny_tail_missing(root, authz_config)
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
    from llm import provider_config_from_yaml  # noqa: PLC0415

    llm_providers_config: dict[str, ProviderConfig] = {}
    try:
        raw_llm = am.get_platform_config("llm_providers")
    except KeyError:
        raw_llm = {}
    for provider_name, raw in (raw_llm or {}).items():
        if not isinstance(raw, dict):
            continue
        # bug-144: the ONE shared factory (also used by
        # `cli.llm_commands`) — applies the bug-055 `type:` default
        # and validates `request_policy`.
        llm_providers_config[provider_name] = provider_config_from_yaml(
            provider_name, raw
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
    chosen_llm_for_model: LLMFactory | None = None
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

    # spec-131 Step 6 — the real HookDispatcher, backed by
    # `platform.yaml`'s `hooks:` section (Step 1's
    # `AssetManagement.hooks_config()`), constructed here so it can be
    # injected as the engine's `observer=`. `bootstrap_runtime` is
    # kolsch's single `AgentEngine` construction site (every CLI
    # command and the HTTP API route through `PlatformRuntime.engine`
    # — see this module's docstring), so wiring it once here covers
    # every current and future live-turn surface, matching Step 5's
    # single-choke-point design (Acceptance Criterion #7).
    #
    # Constructed unconditionally, even with zero configured rules —
    # deliberately, not defaulted to `None` on an empty list:
    # `HookDispatcher.__init__` is trivially cheap for `rules=[]` (a
    # list store plus a zero-iteration warning scan, no I/O), so there
    # is no meaningful cost difference between an empty-rules
    # dispatcher and `observer=None`. Keeping construction unconditional
    # avoids a second branch here, mirrors how `subprocess_framework`
    # just below is "constructed unconditionally — cheap" rather than
    # conditionally omitted, and means `runtime.engine._observer` is
    # always a real `HookDispatcher` — one less "is hooks config even
    # wired" question when debugging. `execute_stream`'s own
    # `observer is not None` branch-skip (Step 5) is what actually
    # keeps the zero-hooks case at "zero measurable behavior/latency
    # regression" (Acceptance Criterion #8), not withholding
    # construction here — an empty-rules `handle()` call is a for-loop
    # over `[]`, not a meaningfully different cost.
    #
    # Loaded once, here, at process start — never re-read.
    # `hooks_config()` is deliberately NOT part of whatever `/reload`
    # (`authz_introspection.py`) re-reads (spec-131 Step 7's trust
    # boundary control); this call site is the only place hook config
    # is read for the lifetime of the process.
    #
    # spec-131 Step 9 — this same instance is ALSO stashed on the
    # returned `PlatformRuntime` (`hook_dispatcher=` below) rather than
    # only injected into the engine, so CLI-layer code that emits
    # events outside `execute_stream`'s queue (e.g. `PromptsImpl`,
    # which writes `kolsch.prompt.requested`/`.answered` straight to
    # the store — see `prompts_impl.py`'s `_append_ui_event`) can reach
    # the exact same dispatcher via `runtime.hook_dispatcher` instead of
    # reaching into `AgentEngine`'s private `_observer`.
    hook_dispatcher = HookDispatcher(am.hooks_config())

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
        # spec-121: constructed unconditionally — cheap (no I/O until
        # `run_exchange`) and only AUTO-selected by the engine for a
        # `claude_code` agent with ≥1 registered tool. Every other
        # exchange stays on `chosen_framework`.
        subprocess_framework=SubprocessAgentFramework(descriptor=ClaudeCodeHarness()),
        llm=chosen_llm,
        llm_for_model=chosen_llm_for_model,
        llm_providers_config=llm_providers_config,
        compaction_provider=compaction_provider,
        # spec-131 Step 6 — real hook dispatch for every event
        # `execute_stream` yields, from this single construction site.
        observer=hook_dispatcher,
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
    # spec-069 Part A — read the per-node execution-timeout default
    # from the same `workflows:` section. Optional. Resolution:
    #   * key absent          → 240s default (below)
    #   * key present, `null` → None (explicit opt-out; unbounded await)
    #   * key present, number → float(value)
    raw_node_timeout: Any
    if workflows_cfg is not None and "default_node_timeout_seconds" in workflows_cfg:
        raw_node_timeout = workflows_cfg["default_node_timeout_seconds"]
    else:
        raw_node_timeout = 240.0
    workflow_default_node_timeout_seconds: float | None
    if raw_node_timeout is None:
        workflow_default_node_timeout_seconds = None
    else:
        workflow_default_node_timeout_seconds = float(raw_node_timeout)

    # bug-114 — approval-response window from the same `workflows:`
    # section (OFF by default). Extracted to `_resolve_approval_window`
    # so `bootstrap_runtime` stays under the PLR0912 branch threshold
    # (same reason the LLM-call-timeout resolution is a helper).
    workflow_approval_response_seconds = _resolve_approval_window(workflows_cfg)

    # bug-086 — resolve the per-LLM-call timeout default from the same
    # `workflows:` section, install it into the engine adapter, and
    # store on the runtime for observability. Extracted to a helper
    # so `bootstrap_runtime` stays under the PLR0912/PLR0915 style
    # thresholds — the resolution has enough branches (explicit key
    # present/absent, null-vs-number, provider-derivation fallback)
    # to be worth its own function.
    llm_call_timeout_seconds = _resolve_llm_call_timeout_default(
        workflows_cfg=workflows_cfg,
        llm_providers_config=llm_providers_config,
    )
    from engine._crewai_adapter import (  # noqa: PLC0415
        set_llm_call_timeout_default,
    )

    set_llm_call_timeout_default(llm_call_timeout_seconds)

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
        workflow_default_node_timeout_seconds=workflow_default_node_timeout_seconds,
        workflow_approval_response_seconds=workflow_approval_response_seconds,
        llm_call_timeout_seconds=llm_call_timeout_seconds,
        # spec-131 Step 9 — same instance just injected into the engine
        # as `observer=hook_dispatcher` above, so CLI-layer emitters
        # (`PromptsImpl`) can reach the identical dispatcher through
        # `runtime.hook_dispatcher` rather than `runtime.engine._observer`.
        hook_dispatcher=hook_dispatcher,
    )

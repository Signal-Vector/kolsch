"""bug-044 — `/reload` rescans bundle + workflow + agent YAMLs, not just platform.

Six tests spanning ACs #2, #4, and #5 in the bug-044 spec:

- `test_reload_picks_up_bundle_yaml_change` — write a rule in a bundle.yaml,
  call `/reload`, verify the new rule fires on the next authz decision.
- `test_reload_picks_up_workflow_policy_change` — same for workflow
  `policy:` blocks.
- `test_reload_picks_up_agent_permissions_change` — same for agent
  `permissions:` fields.
- `test_reload_reports_4_layer_count_on_success` — success summary
  matches feat-041 Mockup 3 ("N rules across 4 layers").
- `test_reload_partial_failure_preserves_old_provider` — AC #2
  invariant: a single broken YAML aborts the reload with old state
  intact.
- `test_reload_ac5_manual_verify_three_layer_edit` — AC #5 ship-gate
  scenario: edit platform + bundle + workflow simultaneously, /reload,
  verify all three edits take effect on the next authz decision.

All tests build a scratch platform root with `platform.yaml` + a
`bundles/` tree and drive `handle_reload` against a `_MockRuntime`
whose tools subsystem carries a real `ToolRegistry` — the bundle /
agent layer pushes require access to `_agents` on the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.services.authz_introspection import handle_reload
from providers.authz import RuleBasedAuthorizationProvider

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers — scratch workspace + mock runtime.
# ---------------------------------------------------------------------------


class _MockStore:
    async def read_path_to_leaf(self, thread_id: object) -> Any:
        _ = thread_id
        if False:
            yield  # keep as async-generator


@dataclass
class _MockRuntime:
    """Duck-typed stand-in for ``PlatformRuntime``.

    Mirrors the same fields ``handle_reload`` touches: ``root`` for
    the on-disk YAML lookup, ``authz`` for the swap, and
    ``asset_management`` + ``tools_subsystem`` for the catalog +
    per-agent registration push.
    """

    root: Path
    authz: Any
    asset_management: Any
    tools_subsystem: Any
    store: _MockStore = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = _MockStore()


def _write_platform_yaml_raw(root: Path, yaml_text: str) -> None:
    """Emit a ``platform.yaml`` with author-supplied YAML text.

    Tests pass raw YAML rather than dict-round-tripping through the
    project's dumper — round-tripping a rule dict through ``dump``
    triggers ruamel's quoting rules for special characters (globs
    containing ``*``, brackets in patterns), which mangles the visible
    on-disk shape enough to hurt debugging.
    """
    (root / "platform.yaml").write_text(yaml_text, encoding="utf-8")


def _write_bundle(
    root: Path,
    name: str,
    *,
    permissions: list[dict[str, Any]] | None = None,
    agent_permissions: dict[str, list[dict[str, Any]]] | None = None,
    workflow_policy: dict[str, list[dict[str, Any]]] | None = None,
) -> Path:
    """Materialize a bundle directory under ``root/bundles/<name>``.

    ``permissions`` populates ``bundle.yaml``'s ``permissions:`` field.
    ``agent_permissions[name]`` writes one agent YAML per key with the
    given rules. ``workflow_policy[name]`` writes one workflow YAML
    (single-node graph, sufficient to parse) per key.
    """
    bundle_dir = root / "bundles" / name
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_yaml = {
        "name": name,
        "version": "0.1.0",
        "description": f"Test bundle {name}.",
        "permissions": permissions or [],
    }
    _dump_yaml(bundle_dir / "bundle.yaml", bundle_yaml)
    if agent_permissions:
        (bundle_dir / "agents").mkdir(exist_ok=True)
        for agent_name, rules in agent_permissions.items():
            _dump_yaml(
                bundle_dir / "agents" / f"{agent_name}.yaml",
                {
                    "name": agent_name,
                    "description": f"Test agent {agent_name}",
                    "prompt": "you are a test agent",
                    "model": {"provider": "anthropic", "model": "claude-opus-4-7"},
                    "permissions": rules,
                },
            )
    if workflow_policy:
        (bundle_dir / "workflows").mkdir(exist_ok=True)
        for wf_name, rules in workflow_policy.items():
            _dump_yaml(
                bundle_dir / "workflows" / f"{wf_name}.yaml",
                {
                    "description": f"Test workflow {wf_name}",
                    "policy": rules,
                    "nodes": {
                        "start": {
                            "type": "kolsch/nodes/echo",
                            "message": "hello",
                        },
                    },
                    "graph": {"edges": []},
                },
            )
    return bundle_dir


def _dump_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as YAML via the project's ``_yamlio.dump``."""
    from kolsch._yamlio import dump

    path.write_text(dump(payload), encoding="utf-8")


class _MockAssetManagement:
    """Wraps a real ``AssetCatalog`` scanned off the scratch workspace.

    ``catalog()`` returns the same catalog on every call — the reload
    mutates ``catalog.bundles`` in place so a second call still sees
    the fresh manifests.
    """

    def __init__(self, root: Path) -> None:
        from assets import catalog as catalog_module

        self._catalog = catalog_module.scan(root)

    def catalog(self) -> Any:
        return self._catalog


class _MockToolsSubsystem:
    """Wraps a real ``ToolRegistry`` so bundle / agent pushes land.

    The reload walks ``_registry._agents`` and overwrites each
    registration's ``bundle_permissions`` + ``agent_permissions``.
    Tests pre-populate the registry with one registration per agent
    the platform expects to reload for.
    """

    def __init__(self) -> None:
        from tools.registry import ToolRegistry

        self._authz: Any = None
        self._registry = ToolRegistry()


def _build_runtime(root: Path) -> _MockRuntime:
    """Assemble the mock runtime + scan the scratch workspace's catalog."""
    from assets.platform_config import load_or_create

    # Ensure platform.yaml exists so `handle_reload`'s first step
    # doesn't burn on a missing-file OSError. The tests overwrite it
    # with the exact contents they want.
    if not (root / "platform.yaml").exists():
        _write_platform_yaml_raw(
            root,
            "authorization:\n"
            "  variant: rule_based\n"
            "  config:\n"
            "    rules:\n"
            "      - {what: '*', pattern: '*', effect: allow}\n",
        )
    _ = load_or_create(root)  # warm up
    asset_management = _MockAssetManagement(root)
    tools_subsystem = _MockToolsSubsystem()
    original = RuleBasedAuthorizationProvider(
        config={"rules": [{"what": "*", "pattern": "*", "effect": "allow"}]},
        catalog=asset_management.catalog(),
    )
    return _MockRuntime(
        root=root,
        authz=original,
        asset_management=asset_management,
        tools_subsystem=tools_subsystem,
    )


# ---------------------------------------------------------------------------
# AC #4 — new tests for the extended scope.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_picks_up_bundle_yaml_change(tmp_path: Path) -> None:
    """Editing a bundle.yaml permission and calling `/reload` refreshes
    the tools subsystem's per-agent `bundle_permissions` list.

    Precondition: the runtime holds an `_AgentRegistration` for the
    catalogued agent (mimics what `engine.setup_agent` would have
    landed at startup). We assert the registration's
    `bundle_permissions` starts empty, edit `bundle.yaml`, /reload,
    and verify the new rule appears on the registration.
    """
    from schemas import AgentId, derive_agent_id

    _write_bundle(
        tmp_path,
        "andre",
        permissions=[],  # empty at "startup"
        agent_permissions={"butler": [{"what": "*", "effect": "allow"}]},
    )
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: allow}\n",
    )
    runtime = _build_runtime(tmp_path)

    # Pin an empty registration for the catalogued agent, matching
    # what the engine's tool-subsystem setup would have done at boot.
    agent_id: AgentId = derive_agent_id("andre", "butler")
    runtime.tools_subsystem._registry.ensure_agent(agent_id)

    # Edit the bundle.yaml on disk — add a new deny rule.
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[{"what": "kolsch/tools/bash", "effect": "deny"}],
        agent_permissions={"butler": [{"what": "*", "effect": "allow"}]},
    )

    result = await handle_reload(runtime)

    assert result.error is None, result.error
    registration = runtime.tools_subsystem._registry._agents[agent_id]
    assert len(registration.bundle_permissions) == 1
    rule = registration.bundle_permissions[0]
    assert rule.what == "kolsch/tools/bash"
    assert rule.effect == "deny"


@pytest.mark.asyncio
async def test_reload_picks_up_workflow_policy_change(tmp_path: Path) -> None:
    """Workflow ``policy:`` blocks are already re-read on each
    invocation (via ``load_workflow(entry.path)``), so /reload doesn't
    need to push state anywhere. This test verifies the reload
    (a) counts the fresh policy rules in its 4-layer total and
    (b) re-parses the edited workflow YAML — a broken policy after
    the edit surfaces as a reload error, per AC #2.
    """
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[],
        workflow_policy={
            "job_review": [{"what": "kolsch/tools/write_file", "effect": "deny"}],
        },
    )
    runtime = _build_runtime(tmp_path)

    result = await handle_reload(runtime)
    assert result.error is None, result.error
    # 1 (platform allow-all) + 0 (bundle) + 1 (workflow policy) + 0 (agent) = 2.
    assert "reloaded 2 rules" in (result.summary or "")

    # Edit the workflow YAML's policy — the count updates on the next
    # /reload without a restart.
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[],
        workflow_policy={
            "job_review": [
                {"what": "kolsch/tools/write_file", "effect": "deny"},
                {"what": "kolsch/tools/bash", "effect": "deny"},
            ],
        },
    )
    result2 = await handle_reload(runtime)
    assert result2.error is None
    assert "reloaded 3 rules" in (result2.summary or "")


@pytest.mark.asyncio
async def test_reload_picks_up_agent_permissions_change(tmp_path: Path) -> None:
    """Editing an agent YAML's `permissions:` field and calling `/reload`
    refreshes the registration's `agent_permissions` list.
    """
    from schemas import AgentId, derive_agent_id

    _write_bundle(
        tmp_path,
        "andre",
        permissions=[],
        agent_permissions={"butler": []},
    )
    runtime = _build_runtime(tmp_path)
    agent_id: AgentId = derive_agent_id("andre", "butler")
    runtime.tools_subsystem._registry.ensure_agent(agent_id)

    # Author a new rule on the agent YAML.
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[],
        agent_permissions={
            "butler": [
                {"what": "kolsch/tools/write_file", "effect": "prompt"},
            ]
        },
    )

    result = await handle_reload(runtime)
    assert result.error is None, result.error
    registration = runtime.tools_subsystem._registry._agents[agent_id]
    assert len(registration.agent_permissions) == 1
    rule = registration.agent_permissions[0]
    assert rule.what == "kolsch/tools/write_file"
    assert rule.effect == "prompt"


@pytest.mark.asyncio
async def test_reload_reports_4_layer_count_on_success(tmp_path: Path) -> None:
    """Mockup 3: `reloaded N rules across 4 layers (platform, bundle,
    workflow, agent)`. Total is the sum across every layer.

    Uses `pattern:` targets rather than qualified tool refs so the
    scratch catalog (which doesn't ship any tools) doesn't trip the
    provider's `what:` catalog-existence check. Bundle / workflow /
    agent rules aren't validated against the catalog at reload time
    — only the platform provider's rules are — so those layers can
    freely reference non-existent tools.
    """
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '/tmp/*', effect: allow}\n"
        "      - {what: '*', pattern: '*', effect: deny}\n",
    )
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[
            {"what": "kolsch/tools/read_file", "effect": "allow"},
            {"what": "kolsch/tools/list_files", "effect": "allow"},
        ],
        agent_permissions={
            "butler": [{"what": "kolsch/tools/write_file", "effect": "prompt"}],
        },
        workflow_policy={
            "job_review": [
                {"what": "kolsch/tools/bash", "effect": "deny"},
                {"what": "kolsch/tools/browse_url", "effect": "deny"},
                {"what": "kolsch/tools/write_file", "effect": "deny"},
            ],
        },
    )
    runtime = _build_runtime(tmp_path)

    result = await handle_reload(runtime)
    assert result.error is None, result.error
    summary = result.summary or ""
    # 2 platform + 2 bundle + 3 workflow + 1 agent = 8.
    assert "reloaded 8 rules across 4 layers" in summary
    assert "platform, bundle, workflow, agent" in summary


@pytest.mark.asyncio
async def test_reload_partial_failure_preserves_old_provider(
    tmp_path: Path,
) -> None:
    """One broken YAML among many valid ones aborts the whole reload.

    Setup: 3 valid YAMLs + 1 malformed one (bundle.yaml). The reload
    must NOT swap `runtime.authz`, NOT push new bundle/agent rules,
    and the error must cite the offending file.
    """
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: allow}\n",
    )
    # Two valid bundles. Runtime built here scans them.
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[{"what": "kolsch/tools/bash", "effect": "deny"}],
    )
    _write_bundle(
        tmp_path,
        "helper",
        permissions=[{"what": "kolsch/tools/read_file", "effect": "allow"}],
    )
    runtime = _build_runtime(tmp_path)
    old_authz = runtime.authz

    # Now corrupt one of the bundles' YAMLs mid-session.
    (tmp_path / "bundles" / "andre" / "bundle.yaml").write_text(
        "name: andre\n"
        "version: 0.1.0\n"
        "description: broken\n"
        "permissions:\n"
        "  - {what: [malformed",  # unterminated flow sequence
        encoding="utf-8",
    )

    result = await handle_reload(runtime)

    assert result.summary is None
    assert result.error is not None
    assert "Old rules still active" in result.error
    assert "andre" in result.error  # cites the offending file
    # Provider was NOT swapped.
    assert runtime.authz is old_authz


# ---------------------------------------------------------------------------
# AC #5 — manual verify (captured as a test so the proof is reproducible).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_ac5_manual_verify_three_layer_edit(tmp_path: Path) -> None:
    """AC #5: edit `platform.yaml` + one `bundle.yaml` + one workflow
    `policy:` block simultaneously, run `/reload`, verify all three
    take effect on the next authz decision.

    This is the "ship gate" scenario feat-041 promised in Mockup 3.
    Rather than paste terminal captures into the PR description, this
    test walks the exact code path `/reload` fires — the assertions
    are the pastable proof.
    """
    from providers.authz import AuthzEffect
    from schemas import AgentId, derive_agent_id

    # Startup state: empty rules everywhere, allow-all catchall in
    # platform.yaml.
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: allow}\n",
    )
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[],
        agent_permissions={"butler": []},
        workflow_policy={"job_review": []},
    )
    runtime = _build_runtime(tmp_path)
    agent_id: AgentId = derive_agent_id("andre", "butler")
    runtime.tools_subsystem._registry.ensure_agent(agent_id)

    # Sim the operator: edit all three layers between the last
    # invocation and the /reload.
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '/tmp/deny/*', effect: deny}\n"
        "      - {what: '*', pattern: '*', effect: allow}\n",
    )
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[
            {"what": "kolsch/tools/bash", "effect": "deny"},
        ],
        agent_permissions={
            "butler": [
                {"what": "kolsch/tools/write_file", "effect": "prompt"},
            ],
        },
        workflow_policy={
            "job_review": [
                {"what": "kolsch/tools/browse_url", "effect": "deny"},
            ],
        },
    )

    result = await handle_reload(runtime)
    assert result.error is None, result.error
    # 2 platform + 1 bundle + 1 workflow + 1 agent = 5. All 4 layers
    # counted in the summary.
    summary = result.summary or ""
    assert "reloaded 5 rules across 4 layers" in summary

    # Verify the PLATFORM edit took effect: the new deny rule fires.
    new_authz = runtime.authz
    denial = await new_authz.allow("user.local", "*", target="/tmp/deny/x")
    assert denial.effect is AuthzEffect.DENY

    # Verify the BUNDLE edit took effect: the registration's
    # bundle_permissions carries the fresh rule.
    registration = runtime.tools_subsystem._registry._agents[agent_id]
    assert len(registration.bundle_permissions) == 1
    assert registration.bundle_permissions[0].what == "kolsch/tools/bash"
    assert registration.bundle_permissions[0].effect == "deny"

    # Verify the AGENT edit took effect: the registration's
    # agent_permissions carries the fresh rule.
    assert len(registration.agent_permissions) == 1
    assert registration.agent_permissions[0].what == "kolsch/tools/write_file"
    assert registration.agent_permissions[0].effect == "prompt"

    # Verify the WORKFLOW edit took effect: the count includes the
    # workflow policy rule (workflows re-read on invocation, so
    # any new run naturally sees the edit).
    # No push-target for workflow rules — the counter proves the reload
    # rescanned the file. If it had failed to parse, the reload would
    # have aborted with an error.


# ---------------------------------------------------------------------------
# qol-051 C1 — /reload discovers new + deleted bundle/agent/workflow YAMLs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_discovers_new_bundle_yaml_added_since_startup(
    tmp_path: Path,
) -> None:
    """A bundle authored AFTER the boot-time scan is picked up on /reload.

    Boot-time scan sees one bundle; operator authors a second bundle
    on disk; /reload rescans; the fresh catalog's ``bundles`` and
    ``assets`` maps carry the new bundle's manifest + agent entries
    and the summary flags the ``+1 bundle`` addition.
    """
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: allow}\n",
    )
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[],
        agent_permissions={"butler": []},
    )
    runtime = _build_runtime(tmp_path)

    # Sanity: the boot catalog only carries "andre".
    boot_catalog = runtime.asset_management.catalog()
    assert set(boot_catalog.bundles.keys()) == {"andre"}

    # Operator drops a second bundle on disk mid-session — this is the
    # workflow the boot-time-only reload could never catch.
    _write_bundle(
        tmp_path,
        "second",
        permissions=[{"what": "kolsch/tools/write_file", "effect": "deny"}],
        agent_permissions={"scribe": [{"what": "*", "effect": "allow"}]},
    )

    result = await handle_reload(runtime)

    assert result.error is None, result.error
    summary = result.summary or ""
    # The bundle diff clause fires with +1 bundle (the "second" one).
    # +1 agent is also possible (the new scribe agent).
    assert "+1 bundle" in summary
    assert "since last reload" in summary

    # Fresh catalog installed on the runtime — future asset_management
    # reads pick up "second".
    fresh_catalog = runtime.asset_management.catalog()
    assert "second" in fresh_catalog.bundles
    # And the new bundle's rules are queryable via the /reload's rule
    # count — 1 platform allow-all + 1 bundle deny + 1 new agent
    # allow-all = 3.
    assert "reloaded 3 rules" in summary


@pytest.mark.asyncio
async def test_reload_notices_deleted_bundle_yaml(tmp_path: Path) -> None:
    """A bundle whose YAML is deleted mid-session drops out on /reload.

    Boot-time scan sees two bundles; operator deletes one's
    ``bundle.yaml``; /reload rescans; the fresh catalog no longer
    carries the removed bundle and the summary flags the ``-1
    bundle`` removal.
    """
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: allow}\n",
    )
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[{"what": "kolsch/tools/bash", "effect": "deny"}],
    )
    _write_bundle(
        tmp_path,
        "removed",
        permissions=[{"what": "kolsch/tools/read_file", "effect": "allow"}],
    )
    runtime = _build_runtime(tmp_path)

    # Sanity: boot catalog carries both bundles.
    boot_catalog = runtime.asset_management.catalog()
    assert set(boot_catalog.bundles.keys()) == {"andre", "removed"}

    # Delete the "removed" bundle's YAML mid-session — the directory
    # stays but the manifest is gone.
    (tmp_path / "bundles" / "removed" / "bundle.yaml").unlink()

    result = await handle_reload(runtime)

    assert result.error is None, result.error
    summary = result.summary or ""
    assert "-1 bundle" in summary
    assert "since last reload" in summary

    # Fresh catalog no longer carries "removed" — subsequent
    # asset_management reads see the deletion.
    fresh_catalog = runtime.asset_management.catalog()
    assert "removed" not in fresh_catalog.bundles
    assert "andre" in fresh_catalog.bundles


# ---------------------------------------------------------------------------
# qol-051 C2 — missing-YAML contract is symmetric across bundle + agent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_handles_missing_bundle_yaml_consistently(
    tmp_path: Path,
) -> None:
    """Bundle YAML disappearing between rescan and reload aborts loudly.

    C1's rescan drops deleted bundles from the fresh catalog, so
    ``_reload_bundles`` sees a missing YAML only in a mid-reload race
    — this test synthesizes the race by monkey-patching
    ``catalog_module.scan`` to return the stale catalog even after the
    on-disk YAML is gone.

    The invariant under test (qol-051 C2): the bundle layer's
    missing-YAML branch fires the SAME abort-with-typed-error path
    ``_reload_agents`` takes on ``OSError``. Silent preservation of
    stale state (the pre-C2 behavior) is a regression trap this test
    locks out.
    """
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: allow}\n",
    )
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[{"what": "kolsch/tools/bash", "effect": "deny"}],
    )
    runtime = _build_runtime(tmp_path)

    # Synthesize the mid-reload race: monkey-patch
    # ``catalog_module.scan`` so the rescan returns the stale (still
    # carrying "andre") catalog even though the YAML has been deleted.
    from assets import catalog as catalog_module

    stale_catalog = runtime.asset_management.catalog()
    (tmp_path / "bundles" / "andre" / "bundle.yaml").unlink()

    original_scan = catalog_module.scan
    try:
        catalog_module.scan = lambda _root: stale_catalog  # type: ignore[assignment]
        result = await handle_reload(runtime)
    finally:
        catalog_module.scan = original_scan  # type: ignore[assignment]

    # Bundle layer surfaces a typed error when the YAML disappears
    # mid-reload — mirroring the agent layer's OSError abort.
    assert result.summary is None
    assert result.error is not None
    assert "Old rules still active" in result.error
    # File path cited in the message so the operator can find it.
    assert "andre" in result.error and "bundle.yaml" in result.error


@pytest.mark.asyncio
async def test_reload_handles_missing_agent_yaml_consistently(
    tmp_path: Path,
) -> None:
    """Agent YAML disappearing between rescan and reload aborts loudly.

    Mirror of the bundle case above — same race, agent layer. The
    ``_reload_agents`` helper aborts on OSError (has since bug-044);
    this test locks that behavior in AND documents the symmetry with
    ``_reload_bundles``'s new missing-YAML branch (qol-051 C2).
    """
    _write_platform_yaml_raw(
        tmp_path,
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: allow}\n",
    )
    _write_bundle(
        tmp_path,
        "andre",
        permissions=[],
        agent_permissions={"butler": [{"what": "*", "effect": "allow"}]},
    )
    runtime = _build_runtime(tmp_path)

    from assets import catalog as catalog_module

    stale_catalog = runtime.asset_management.catalog()
    (tmp_path / "bundles" / "andre" / "agents" / "butler.yaml").unlink()

    original_scan = catalog_module.scan
    try:
        catalog_module.scan = lambda _root: stale_catalog  # type: ignore[assignment]
        result = await handle_reload(runtime)
    finally:
        catalog_module.scan = original_scan  # type: ignore[assignment]

    # Same observable behavior as the bundle case: typed error,
    # "Old rules still active" clause, offending file cited.
    assert result.summary is None
    assert result.error is not None
    assert "Old rules still active" in result.error
    assert "butler" in result.error or "agent YAML" in result.error



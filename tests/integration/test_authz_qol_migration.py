"""qol-permission-system-human-readable.md Step 2 — integration roundtrip.

Wires the migration shim against a real on-disk workspace bootstrap:

1. Create a workspace with the canonical pre-QoL ``platform.yaml`` rule
   set (legacy ``{id, who, what, effect}`` shape).
2. Build an :class:`AssetCatalog` against the real bundles dir
   (so the migrator can resolve ``tool.bash`` → ``<bundle>/bash``).
3. Run the migrator end-to-end.
4. Re-load the migrated rules into a fresh
   :class:`RuleBasedAuthorizationProvider` and exercise the
   actual ``allow()`` paths.

This catches integration regressions the unit tests miss:
- ``parse_three_segment`` / catalog key shape drift.
- ``AssetManagement.catalog()`` accessor wiring.
- Action→tool_ref map building from a real scan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from assets import AssetManagement
from cli._migrations.authz_rules import migrate_authz_rules
from providers import RuleBasedAuthorizationProvider

if TYPE_CHECKING:
    from pathlib import Path

_CANONICAL_LEGACY_RULES = [
    {"id": "rl_a", "who": ".*", "what": r"agent\.execute\..*", "effect": "allow"},
    {"id": "rl_b", "who": ".*", "what": r"tool\.read_file", "effect": "allow"},
    {"id": "rl_c", "who": ".*", "what": r"tool\.list_files", "effect": "allow"},
    {"id": "rl_d", "who": ".*", "what": r"tool\.write_file", "effect": "prompt"},
    {"id": "rl_e", "who": ".*", "what": r"^find .*$", "effect": "allow"},
    {"id": "rl_f", "who": ".*", "what": r"^ls .*$", "effect": "allow"},
    {"id": "rl_g", "who": ".*", "what": r"^grep .*$", "effect": "allow"},
    {"id": "rl_h", "who": ".*", "what": r"^cat .*$", "effect": "allow"},
    {"id": "rl_i", "who": ".*", "what": r"tool\.bash", "effect": "prompt"},
    {"id": "rl_default_deny", "who": ".*", "what": r".*", "effect": "deny"},
]


def _seed_workspace(root: Path) -> None:
    """Build a minimal workspace on disk: platform.yaml + kolsch bundle."""
    (root / "bundles" / "kolsch" / "tools").mkdir(parents=True)
    (root / "bundles" / "kolsch" / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "kolsch",
                "version": "0.1.0",
                "description": "kolsch builtin bundle (test fixture)",
            }
        ),
        encoding="utf-8",
    )
    # Tools are discovered by file presence; the migration shim only
    # cares that each tool name lives somewhere in the catalog. The
    # files don't need to be importable for catalog scanning to work.
    for name in ("bash", "read_file", "write_file", "list_files"):
        (root / "bundles" / "kolsch" / "tools" / f"{name}.py").write_text(
            f"# stub for {name}\n",
            encoding="utf-8",
        )
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "authorization": {
                    "variant": "rule_based",
                    "config": {"rules": _CANONICAL_LEGACY_RULES},
                },
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# TC-PERM-SCHEMA-MIG-01 — full canonical workspace migrates cleanly
# ---------------------------------------------------------------------------


async def test_canonical_workspace_migrates_roundtrip(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    am = AssetManagement(root=tmp_path)
    await am.initialize()
    catalog = am.catalog()

    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text(encoding="utf-8"))
    legacy = payload["authorization"]["config"]["rules"]
    result = migrate_authz_rules(legacy, catalog=catalog)

    # All ten rules convert. No deprecation events.
    assert result.deprecated == []
    assert len(result.rules) == len(legacy)
    # The tail collapses to the new-shape catch-all.
    assert result.rules[-1] == {"what": "*", "pattern": "*", "effect": "deny"}
    # qol-action-namespace-bundle-scoped.md Step 2 — `tool\.read_file`
    # resolves to the 3-seg catalog ref `kolsch/tools/read_file`.
    assert result.rules[1] == {
        "what": "kolsch/tools/read_file",
        "pattern": "*",
        "effect": "allow",
    }
    # `^find .*$` resolves to `kolsch/tools/bash` + `find *`.
    assert result.rules[4] == {
        "what": "kolsch/tools/bash",
        "pattern": "find *",
        "effect": "allow",
    }

    # The migrated rules load into a fresh provider and exercise
    # the real matcher path. Actions are now 3-seg directly; the
    # bridge map carries node entries only.
    provider = RuleBasedAuthorizationProvider(
        config={"rules": result.rules},
        catalog=catalog,
    )

    # bash `find …` is allowed by the migrated `find *` rule.
    res = await provider.allow(
        "user.local", "kolsch/tools/bash", target="find / -name x"
    )
    assert res.allowed

    # bash `rm …` falls through to the bash prompt rule → no
    # callback → collapsed to deny.
    res = await provider.allow("user.local", "kolsch/tools/bash", target="rm -rf /")
    assert not res.allowed

    # read_file is silently allowed.
    res = await provider.allow("user.local", "kolsch/tools/read_file", target="/tmp/x")
    assert res.allowed

    # agent.execute.<id> is allowed by the pre-flight rule.
    res = await provider.allow("user.local", "agent.execute.helper")
    assert res.allowed


# ---------------------------------------------------------------------------
# TC-PERM-SCHEMA-MIG-02 — pre-fix verification (provider rejects bare names)
# ---------------------------------------------------------------------------


async def test_provider_rejects_bare_tool_ref_in_real_workspace(
    tmp_path: Path,
) -> None:
    """User hand-edits a rule to `what: bash` (no slash) → load fails."""
    _seed_workspace(tmp_path)
    am = AssetManagement(root=tmp_path)
    await am.initialize()
    catalog = am.catalog()

    # Hand-authored rule list with a bare tool name. The provider
    # should refuse to load with AuthzBareToolRef.
    import pytest

    from errors import AuthzBareToolRef

    with pytest.raises(AuthzBareToolRef) as excinfo:
        RuleBasedAuthorizationProvider(
            config={
                "rules": [
                    {"what": "bash", "pattern": "*", "effect": "allow"},
                    {"what": "*", "pattern": "*", "effect": "deny"},
                ],
            },
            catalog=catalog,
        )
    # The error names the qualified form the user should write.
    # qol-action-namespace-bundle-scoped.md Step 2 — candidate is 3-seg.
    assert "kolsch/tools/bash" in excinfo.value.detail["candidates"]


# ---------------------------------------------------------------------------
# bug-159 — `kolsch setup` re-run migrates a scoped legacy rule before
# anything builds the provider from it (spec S1 escape hatch)
# ---------------------------------------------------------------------------


async def test_setup_rerun_migrates_scoped_legacy_rule_keeping_from(
    tmp_path: Path,
) -> None:
    """A legacy rule with `from:` bricks provider load, but setup fixes it.

    Drives the real re-run path (`_step_b_skeleton` on an initialized
    workspace) — it migrates before Step C/E build the runtime — and
    asserts the rewritten rule keeps its `from:`.
    """
    import io

    import pytest

    from cli._setup import NonInteractivePrompter, _SetupIO, _step_b_skeleton
    from errors import AuthzMalformedRule

    _seed_workspace(tmp_path)
    agents = tmp_path / "bundles" / "kolsch" / "agents"
    agents.mkdir()
    (agents / "a.yaml").write_text("name: a\n", encoding="utf-8")
    scoped = {"who": ".*", "what": r"tool\.bash", "from": "kolsch/agents/a"}
    scoped["effect"] = "allow"
    rules = [scoped, *_CANONICAL_LEGACY_RULES]
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {"authorization": {"variant": "rule_based", "config": {"rules": rules}}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(AuthzMalformedRule):
        RuleBasedAuthorizationProvider(config={"rules": rules})

    out = io.StringIO()
    setup_io = _SetupIO(
        root=tmp_path,
        out=out,
        err=io.StringIO(),
        prompter=NonInteractivePrompter(),
        connect_io_factory=lambda *a, **k: None,
        run_handler=lambda *a, **k: None,
    )
    await _step_b_skeleton(setup_io, non_interactive=True)

    post = yaml.safe_load((tmp_path / "platform.yaml").read_text(encoding="utf-8"))
    post_rules = post["authorization"]["config"]["rules"]
    assert post_rules[0] == {
        "what": "kolsch/tools/bash",
        "pattern": "*",
        "from": "kolsch/agents/a",
        "effect": "allow",
    }
    am = AssetManagement(root=tmp_path)
    await am.initialize()
    provider = RuleBasedAuthorizationProvider(
        config={"rules": post_rules}, catalog=am.catalog()
    )
    allowed = await provider.allow(
        "user.local",
        "kolsch/tools/bash",
        target="rm -rf /",
        caller_chain=("kolsch/agents/a",),
    )
    other = await provider.allow(
        "user.local",
        "kolsch/tools/bash",
        target="rm -rf /",
        caller_chain=("kolsch/agents/b",),
    )
    assert allowed.allowed
    assert not other.allowed

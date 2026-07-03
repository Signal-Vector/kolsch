"""PERM-SYS-01..05 — qol-permission-system-human-readable.md Step 4
end-to-end integration suite.

Five integration cases pinning the load-bearing properties of the
QoL across the four-step build:

- **PERM-SYS-01**: `kolsch setup` re-run migrates a legacy
  ``platform.yaml`` to the new ``{what, pattern, effect}`` schema;
  every canonical rule round-trips cleanly and the backup is
  written.
- **PERM-SYS-02**: a workspace with a bare-tool-ref rule fails to
  start with the expected ``authz.bare_tool_ref`` Fatal naming
  the qualified candidate.
- **PERM-SYS-03**: ``/perm allow`` writes a new-shape rule and
  the next ``allow()`` call against that tool ref returns
  ``allowed=True`` — persistence + matcher integration.
- **PERM-SYS-04**: rendering a ``tool.failed`` event with
  ``error.type=tool.unauthorized`` AND ``recovery_hint`` produces
  the distinct "permission" capsule + the exact ``/perm`` command.
- **PERM-SYS-05**: ``ToolFailedData`` carries ``recovery_hint``
  end-to-end through ``model_dump`` so the model + the renderer
  both see the actionable text.

PERM-SYS-04 and -05 reuse the renderer + schema directly (no real
engine wiring) because the engine's ``ToolUnauthorized`` catch
block is already covered by Step 3's component tests; the value
here is the SHAPE of the event that crosses the wire.

Tests build a real workspace on disk + bootstrap the runtime so
PERM-SYS-01/-02/-03 exercise the actual ``cli/_setup.py``,
``cli/_slash.py``, and ``platform_runtime/runtime.py`` paths.
"""

from __future__ import annotations

import io as _io
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
import yaml

from assets import AssetManagement
from cli._renderer import Renderer
from cli._slash import (
    ChatSessionState,
    SlashContext,
    dispatch_slash,
)
from errors import AuthzBareToolRef
from platform_runtime import bootstrap_runtime
from providers import RuleBasedAuthorizationProvider
from schemas import (
    Actor,
    Event,
    EventId,
    EventType,
    SessionId,
    ThreadId,
    ToolFailedData,
)
from schemas.error import Error

if TYPE_CHECKING:
    from pathlib import Path

    from platform_runtime import PlatformRuntime


# ---------------------------------------------------------------------------
# Fixtures — synthetic workspace + runtime
# ---------------------------------------------------------------------------


def _seed_kolsch_bundle(root: Path) -> None:
    """Drop a minimal kolsch bundle into ``root/bundles/`` so the
    catalog has tools the qualified-ref rules can resolve."""
    (root / "bundles" / "kolsch" / "tools").mkdir(parents=True, exist_ok=True)
    (root / "bundles" / "kolsch" / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "kolsch",
                "version": "0.1.0",
                "description": "kolsch bundle (test fixture)",
            }
        ),
        encoding="utf-8",
    )
    for name in ("bash", "read_file", "write_file", "list_files"):
        (root / "bundles" / "kolsch" / "tools" / f"{name}.py").write_text(
            f"# stub for {name}\n",
            encoding="utf-8",
        )


def _seed_pre_qol_workspace(root: Path) -> Path:
    """Build a workspace whose platform.yaml has the legacy
    ``{id, who, what, effect}`` schema for every rule.

    Mirrors the canonical post-Phase-2.0 user workspace before
    the QoL Step 4 migration runs."""
    from assets.platform_config import default_platform_payload

    _seed_kolsch_bundle(root)
    payload = default_platform_payload()
    payload["defaults"] = {}
    payload["authorization"] = {
        "variant": "rule_based",
        "config": {
            "rules": [
                {
                    "id": "rl_a",
                    "who": ".*",
                    "what": r"agent\.execute\..*",
                    "effect": "allow",
                },
                {
                    "id": "rl_b",
                    "who": ".*",
                    "what": r"tool\.read_file",
                    "effect": "allow",
                },
                {
                    "id": "rl_c",
                    "who": ".*",
                    "what": r"tool\.list_files",
                    "effect": "allow",
                },
                {
                    "id": "rl_d",
                    "who": ".*",
                    "what": r"tool\.write_file",
                    "effect": "prompt",
                },
                {
                    "id": "rl_e",
                    "who": ".*",
                    "what": r"^find .*$",
                    "effect": "allow",
                },
                {
                    "id": "rl_f",
                    "who": ".*",
                    "what": r"tool\.bash",
                    "effect": "prompt",
                },
                {
                    "id": "rl_default_deny",
                    "who": ".*",
                    "what": ".*",
                    "effect": "deny",
                },
            ],
        },
    }
    yaml_path = root / "platform.yaml"
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return yaml_path


def _seed_new_shape_workspace(root: Path) -> Path:
    """Build a workspace whose platform.yaml is already in the new
    schema with only the default-deny catch-all (no broader
    bash-specific rule). PERM-SYS-03 then exercises ``/perm allow``
    landing above the catch-all + matching on next call.

    A workspace where a broader ``deny kolsch/tools/bash *`` sits above
    the catch-all is the smart-insertion shadow case tracked at
    E-QOL-PERMSYS-004 (Step 4 doc carryover); it isn't part of
    PERM-SYS-03's acceptance criterion."""
    from assets.platform_config import default_platform_payload

    _seed_kolsch_bundle(root)
    payload = default_platform_payload()
    payload["defaults"] = {}
    payload["authorization"] = {
        "variant": "rule_based",
        "config": {
            "rules": [
                {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
                {"what": "*", "pattern": "*", "effect": "deny"},
            ],
        },
    }
    yaml_path = root / "platform.yaml"
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return yaml_path


def _slash_ctx(runtime: PlatformRuntime, stdout: _io.StringIO) -> SlashContext:
    """Build a SlashContext that exercises ``/perm`` against a real
    runtime. The handler reads only ``ctx.state.runtime`` so we
    duck-type the state."""

    class _StubState:
        def __init__(self, runtime: PlatformRuntime) -> None:
            self.runtime = runtime

    state = cast("ChatSessionState", _StubState(runtime))
    return SlashContext(stdout=stdout, state=state)


# ===========================================================================
# PERM-SYS-01 — Migration round-trip (legacy → new shape)
# ===========================================================================


async def test_perm_sys_01_migration_roundtrip(tmp_path: Path) -> None:
    """``kolsch setup`` re-run migrates legacy rules to the new
    schema. Every canonical pre-Step-2 rule round-trips cleanly;
    a backup is written; the post-migration provider matches a
    ``tool.bash`` action against the migrated ``kolsch/tools/bash``
    qualified ref."""
    yaml_path = _seed_pre_qol_workspace(tmp_path)
    # Pre-migration assertion: every rule still carries `who`.
    pre = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert all("who" in r for r in pre["authorization"]["config"]["rules"])

    # Run the QoL schema-migration hook directly — the setup
    # runner calls this unconditionally on re-run (the public
    # ``run_setup`` path additionally drives provider-config and
    # test-exchange steps that aren't part of this assertion).
    from cli._setup import (
        NonInteractivePrompter,
        _maybe_migrate_authz_rules_to_qol_schema,
        _SetupIO,
    )

    stdout = _io.StringIO()
    io_ = _SetupIO(
        root=tmp_path,
        out=stdout,
        err=_io.StringIO(),
        prompter=NonInteractivePrompter(),
        connect_io_factory=lambda *a, **k: None,
        run_handler=lambda *a, **k: None,
    )
    await _maybe_migrate_authz_rules_to_qol_schema(io_)

    # Post-migration: rules are in new shape.
    post = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    post_rules = post["authorization"]["config"]["rules"]
    for rule in post_rules:
        assert "who" not in rule, f"legacy `who` survived: {rule}"
        assert "id" not in rule, f"legacy `id` survived: {rule}"
        assert "pattern" in rule, f"missing `pattern`: {rule}"
    # The migrated bash rule names the qualified ref.
    bash_rules = [r for r in post_rules if r["what"] == "kolsch/tools/bash"]
    assert bash_rules, f"no kolsch/tools/bash rule in {post_rules}"

    # Backup written.
    backups = sorted((tmp_path / ".platform").glob("platform.yaml.bak.*"))
    assert backups, "no backup file written"

    # The migrated rule matches a real action invocation.
    am = AssetManagement(root=tmp_path)
    await am.initialize()
    provider = RuleBasedAuthorizationProvider(
        config=post["authorization"]["config"],
        catalog=am.catalog(),
    )
    result = await provider.allow(
        "user.local",
        "kolsch/tools/bash",
        target="find / -name foo",
    )
    assert result.allowed
    assert result.matched_rule is not None
    assert result.matched_rule.what == "kolsch/tools/bash"


# ===========================================================================
# PERM-SYS-02 — Bare tool name blocks startup
# ===========================================================================


async def test_perm_sys_02_bare_tool_name_blocks_startup(tmp_path: Path) -> None:
    """A workspace whose platform.yaml carries a bare-name rule
    (``what: bash``) refuses to bootstrap: the provider's
    ``_validate_what`` raises :class:`AuthzBareToolRef` naming the
    qualified candidate."""
    from assets.platform_config import default_platform_payload

    _seed_kolsch_bundle(tmp_path)
    payload = default_platform_payload()
    payload["defaults"] = {}
    payload["authorization"] = {
        "variant": "rule_based",
        "config": {
            "rules": [
                {"what": "bash", "pattern": "*", "effect": "allow"},
                {"what": "*", "pattern": "*", "effect": "deny"},
            ],
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(AuthzBareToolRef) as excinfo:
        await bootstrap_runtime(root=tmp_path)
    assert excinfo.value.detail["bare_name"] == "bash"
    assert "kolsch/tools/bash" in excinfo.value.detail["candidates"]


# ===========================================================================
# PERM-SYS-03 — /perm allow persists + takes effect on next call
# ===========================================================================


async def test_perm_sys_03_perm_allow_persists_and_takes_effect(
    tmp_path: Path,
) -> None:
    """``/perm allow kolsch/tools/bash "find *"`` writes a rule above the
    catch-all; the next ``provider.allow("...", "kolsch/tools/bash",
    target="find / -name x")`` returns ``allowed=True``; reloading
    the workspace yields the same rule (persistence)."""
    _seed_new_shape_workspace(tmp_path)
    runtime = await bootstrap_runtime(root=tmp_path)

    # Pre-/perm: only the catch-all matches → deny by default.
    result = await runtime.authz.allow(
        "user.local", "kolsch/tools/bash", target="find /"
    )
    assert not result.allowed
    assert result.matched_rule is not None
    assert result.matched_rule.what == "*"  # the catch-all fired

    stdout = _io.StringIO()
    ctx = _slash_ctx(runtime, stdout)
    sig = await dispatch_slash('/perm allow kolsch/tools/bash "find *"', ctx)
    assert sig.value == "continue"
    assert "added rule" in stdout.getvalue()

    # Post-/perm: the in-memory provider sees the new rule because
    # ``add_rule`` mutates the rule list in place AND ``/perm`` writes
    # the YAML.
    result = await runtime.authz.allow(
        "user.local", "kolsch/tools/bash", target="find / -name x"
    )
    assert result.allowed
    assert result.matched_rule is not None
    assert result.matched_rule.what == "kolsch/tools/bash"
    assert result.matched_rule.pattern == "find *"

    # Persistence — reload from disk and assert the rule survives.
    am = AssetManagement(root=tmp_path)
    await am.initialize()
    reloaded = RuleBasedAuthorizationProvider(
        config={
            "rules": yaml.safe_load(
                (tmp_path / "platform.yaml").read_text(encoding="utf-8"),
            )["authorization"]["config"]["rules"],
        },
        catalog=am.catalog(),
    )
    result = await reloaded.allow(
        "user.local", "kolsch/tools/bash", target="find / -name x"
    )
    assert result.allowed


async def test_perm_sys_03b_perm_allow_with_from_scopes_rule(
    tmp_path: Path,
) -> None:
    """`/perm allow ... from:<ref>` writes a scoped rule. The
    persisted YAML carries the `from` key; broader unscoped
    rules with the same (what, pattern) are NOT shadow-removed
    because different `from_` values are genuinely different
    rules.
    """
    _seed_new_shape_workspace(tmp_path)
    runtime = await bootstrap_runtime(root=tmp_path)

    stdout = _io.StringIO()
    ctx = _slash_ctx(runtime, stdout)
    # Use a glob so the ref skips the catalog-existence check
    # (this test doesn't ship an agent fixture).
    sig = await dispatch_slash(
        "/perm allow kolsch/tools/bash * from:kolsch/agents/*",
        ctx,
    )
    assert sig.value == "continue"
    assert "added rule" in stdout.getvalue()
    assert "from='kolsch/agents/*'" in stdout.getvalue()

    payload = yaml.safe_load(
        (tmp_path / "platform.yaml").read_text(encoding="utf-8"),
    )
    scoped = [
        r
        for r in payload["authorization"]["config"]["rules"]
        if r.get("what") == "kolsch/tools/bash" and "from" in r
    ]
    assert len(scoped) == 1
    assert scoped[0]["from"] == "kolsch/agents/*"
    assert scoped[0]["pattern"] == "*"
    assert scoped[0]["effect"] == "allow"


# ===========================================================================
# PERM-SYS-04 — authz.denied renders distinct capsule with /perm hint
# ===========================================================================


def _denied_tool_failed_event(
    *,
    tool_name: str = "bash",
    matched_rule: dict[str, object] | None = None,
    recovery_hint: str = '/perm allow kolsch/tools/bash "find *"',
) -> Event:
    """Mirror what
    :class:`engine.observers._ObservedToolHandle` emits on
    :class:`errors.ToolUnauthorized`."""
    matched_rule = matched_rule or {
        "what": "kolsch/tools/bash",
        "pattern": "*",
        "effect": "deny",
    }
    error = Error(
        type="tool.unauthorized",
        message=f"Authorization denied for tool {tool_name!r}",
        where="tools.dispatch.invoke",
        detail={
            "tool_name": tool_name,
            "action": f"tool.{tool_name}",
            "principal": "agent.test",
            "matched_rule": matched_rule,
        },
    )
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        type=EventType.TOOL_FAILED,
        actor=Actor(kind="agent", id="ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        created_at=datetime.now(UTC),
        data=ToolFailedData(
            tool_call_id="tc_1",
            tool_name=tool_name,
            error=error,
            recovery_hint=recovery_hint,
        ).model_dump(mode="json"),
    )


def test_perm_sys_04_denied_capsule_renders_with_perm_hint() -> None:
    """The renderer surfaces the ``permission`` capsule (orange
    palette) with the tool ref, matched rule, and the exact
    ``/perm allow`` command when ``tool.unauthorized`` +
    ``recovery_hint`` arrive together."""
    out = _io.StringIO()
    renderer = Renderer(stdout=out, agent_name="test")
    renderer._render_tool_failed(_denied_tool_failed_event())
    rendered = out.getvalue()
    # Source label flips to `permission`, not `tool` — the visual
    # contract that makes a denial distinct from a generic failure.
    assert "permission" in rendered
    # Body lines: `denied:`, the matched rule, the /perm command.
    assert "denied: bash" in rendered
    assert "what='kolsch/tools/bash'" in rendered
    assert "pattern='*'" in rendered
    assert '/perm allow kolsch/tools/bash "find *"' in rendered


# ===========================================================================
# PERM-SYS-05 — tool.failed event carries recovery_hint end-to-end
# ===========================================================================


def test_perm_sys_05_tool_failed_carries_recovery_hint() -> None:
    """``ToolFailedData.recovery_hint`` round-trips through
    ``model_dump`` so the framework's payload (the model's view)
    and the JSONL (the replay view) both see the actionable text.
    The engine's ``_derive_authz_recovery_hint`` produces it from
    the matched rule + the in-flight args."""
    from engine.observers import _derive_authz_recovery_hint
    from errors import ToolUnauthorized

    # Step 1 — derive the hint from a denied invocation.
    exc = ToolUnauthorized(
        tool_name="bash",
        action="kolsch/tools/bash",
        principal="agent.test",
        matched_rule={"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
    )
    hint = _derive_authz_recovery_hint(exc, arguments={"command": "find / -name foo"})
    assert hint == '/perm allow kolsch/tools/bash "find *"'

    # Step 2 — the schema carries the hint round-trip.
    error = Error.from_platform_error(exc)
    data = ToolFailedData(
        tool_call_id="tc_1",
        tool_name="bash",
        error=error,
        recovery_hint=hint,
    )
    dump = data.model_dump(mode="json")
    assert dump["recovery_hint"] == hint
    # Step 3 — round-trip back into a model instance preserves the
    # field (replay equivalence).
    rebuilt = ToolFailedData.model_validate(dump)
    assert rebuilt.recovery_hint == hint

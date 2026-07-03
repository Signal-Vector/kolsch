"""qol-permission-system-human-readable.md Step 3 — /perm slash + capsule tests.

Four test groups covering Step 3's acceptance criteria:

1. Each ``/perm`` verb writes the right rule (or surfaces a
   friendly error for bad inputs).
2. Bare tool name → friendly slash-line error naming the
   qualified form (no AuthzBareToolRef exception leaking).
3. The denied capsule renders the new shape with the tool ref,
   matched rule, and the exact ``/perm`` recovery command.
4. ``ToolFailedData`` accepts and round-trips ``recovery_hint``.

Tests build a real synthetic workspace + runtime so the slash
handlers exercise the actual chat code paths (write_back, authz
provider, catalog). Faster than spinning up the full chat loop;
narrower than the integration suite.
"""

from __future__ import annotations

import io as _io
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import yaml

from cli._renderer import Renderer
from cli._slash import (
    ChatSessionState,
    SlashContext,
    dispatch_slash,
)
from platform_runtime import bootstrap_runtime
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
# Workspace + runtime fixtures
# ---------------------------------------------------------------------------


def _platform_seed() -> dict[str, object]:
    """Build a minimal but complete platform.yaml payload.

    Reuses :func:`assets.platform_config.default_platform_payload`
    so we get every required section (secrets, sandbox, storage,
    event_bus, file_access) — then overrides the authorization
    rules with the new-shape catch-all the /perm tests want."""
    from assets.platform_config import default_platform_payload

    payload = default_platform_payload()
    payload["authorization"] = {
        "variant": "rule_based",
        "config": {
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
                {"what": "*", "pattern": "*", "effect": "deny"},
            ],
        },
    }
    payload["defaults"] = {}
    return payload


def _seed_workspace(root: Path) -> None:
    """Build a workspace with a kolsch bundle that owns `bash` +
    a second `andre` bundle that also ships a tool named `note`
    so we can test ambiguous-bare-name validation."""
    (root / "bundles" / "kolsch" / "tools").mkdir(parents=True)
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
    for name in ("bash", "read_file"):
        (root / "bundles" / "kolsch" / "tools" / f"{name}.py").write_text(
            f"# stub for {name}\n",
            encoding="utf-8",
        )
    (root / "bundles" / "andre" / "tools").mkdir(parents=True)
    (root / "bundles" / "andre" / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "andre",
                "version": "0.1.0",
                "description": "andre bundle (test fixture)",
            }
        ),
        encoding="utf-8",
    )
    (root / "bundles" / "andre" / "tools" / "fetch_url.py").write_text(
        "# stub\n",
        encoding="utf-8",
    )
    (root / "platform.yaml").write_text(
        yaml.safe_dump(_platform_seed()), encoding="utf-8"
    )


async def _runtime(root: Path) -> PlatformRuntime:
    _seed_workspace(root)
    return await bootstrap_runtime(root=root)


def _ctx(runtime: PlatformRuntime, stdout: _io.StringIO) -> SlashContext:
    """Build a SlashContext with just enough state for ``/perm``.

    The handler reads only ``ctx.state.runtime`` — no other state
    fields. We bypass the heavy ResolvedAgentConfig fixture by
    duck-typing a ``_StubState`` and casting it through ``Any``."""
    from typing import cast

    class _StubState:
        def __init__(self, runtime: PlatformRuntime) -> None:
            self.runtime = runtime

    state = cast("ChatSessionState", _StubState(runtime))
    return SlashContext(stdout=stdout, state=state)


# ===========================================================================
# Group 1 — /perm verbs persist the right rules
# ===========================================================================


async def test_perm_allow_writes_new_shape_rule(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)

    sig = await dispatch_slash('/perm allow kolsch/tools/bash "find *"', ctx)
    assert sig.value == "continue"
    output = stdout.getvalue()
    assert "/perm allow:" in output
    assert "kolsch/tools/bash" in output
    assert "find *" in output

    # platform.yaml should now contain the new-shape rule.
    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text(encoding="utf-8"))
    rules = payload["authorization"]["config"]["rules"]
    new_rule = next(
        r
        for r in rules
        if r.get("what") == "kolsch/tools/bash" and r.get("pattern") == "find *"
    )
    assert new_rule["effect"] == "allow"
    # `who` and `id` are absent in the new shape.
    assert "who" not in new_rule
    assert "id" not in new_rule


async def test_perm_deny_writes_new_shape_rule(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    await dispatch_slash('/perm deny kolsch/tools/bash "rm *"', ctx)
    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text(encoding="utf-8"))
    rules = payload["authorization"]["config"]["rules"]
    assert any(
        r.get("what") == "kolsch/tools/bash"
        and r.get("pattern") == "rm *"
        and r.get("effect") == "deny"
        for r in rules
    )


async def test_perm_prompt_writes_new_shape_rule(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    await dispatch_slash("/perm prompt kolsch/tools/bash *", ctx)
    rules = yaml.safe_load(
        (tmp_path / "platform.yaml").read_text(encoding="utf-8"),
    )["authorization"]["config"]["rules"]
    assert any(
        r.get("what") == "kolsch/tools/bash" and r.get("effect") == "prompt"
        for r in rules
    )


async def test_perm_remove_drops_matching_new_shape_rule(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    # Add then remove.
    await dispatch_slash('/perm allow kolsch/tools/bash "find *"', ctx)
    stdout.truncate(0)
    stdout.seek(0)
    await dispatch_slash('/perm remove kolsch/tools/bash "find *"', ctx)
    output = stdout.getvalue()
    assert "dropped rule" in output
    rules = yaml.safe_load(
        (tmp_path / "platform.yaml").read_text(encoding="utf-8"),
    )["authorization"]["config"]["rules"]
    assert not any(
        r.get("what") == "kolsch/tools/bash" and r.get("pattern") == "find *"
        for r in rules
    )


async def test_perm_list_renders_all_rules(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    await dispatch_slash("/perm list", ctx)
    output = stdout.getvalue()
    assert "rules (first-match-wins" in output
    assert "kolsch/tools/bash" in output


async def test_perm_allow_replaces_existing_same_rule(tmp_path: Path) -> None:
    """``/perm allow`` twice with the same (tool, pattern) shouldn't
    create two duplicates; the second call replaces the first so
    the user's intent isn't shadowed."""
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    await dispatch_slash('/perm allow kolsch/tools/bash "find *"', ctx)
    stdout.truncate(0)
    stdout.seek(0)
    await dispatch_slash('/perm allow kolsch/tools/bash "find *"', ctx)
    output = stdout.getvalue()
    assert "replaced previous rule" in output
    rules = yaml.safe_load(
        (tmp_path / "platform.yaml").read_text(encoding="utf-8"),
    )["authorization"]["config"]["rules"]
    matching = [
        r
        for r in rules
        if r.get("what") == "kolsch/tools/bash" and r.get("pattern") == "find *"
    ]
    assert len(matching) == 1


# ===========================================================================
# Group 2 — bare tool name + bad input produce friendly errors
# ===========================================================================


async def test_perm_bare_tool_name_emits_friendly_error(tmp_path: Path) -> None:
    """No exception bubbles to the chat — just a polite message
    naming the qualified form."""
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    sig = await dispatch_slash('/perm allow bash "find *"', ctx)
    assert sig.value == "continue"
    output = stdout.getvalue()
    assert "bare tool name" in output
    assert "kolsch/tools/bash" in output  # the suggested qualified form


async def test_perm_pattern_with_regex_chars_emits_friendly_error(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    sig = await dispatch_slash('/perm allow kolsch/tools/bash "^find .*$"', ctx)
    assert sig.value == "continue"
    output = stdout.getvalue()
    assert "regex character" in output


async def test_perm_unknown_verb_emits_usage(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    await dispatch_slash("/perm forbid kolsch/tools/bash *", ctx)
    output = stdout.getvalue()
    assert "unknown verb" in output


async def test_perm_missing_args_emits_usage(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    stdout = _io.StringIO()
    ctx = _ctx(runtime, stdout)
    await dispatch_slash("/perm allow", ctx)
    output = stdout.getvalue()
    assert "verb" in output  # the usage line names verbs


# ===========================================================================
# Group 3 — denied capsule renders with /perm hint
# ===========================================================================


def _denied_event(
    *,
    tool_name: str = "bash",
    matched_rule: dict[str, object] | None = None,
    recovery_hint: str = "/perm allow kolsch/tools/bash *",
) -> Event:
    """Build a synthetic tool.failed event the renderer can consume.

    The shape mirrors what
    :class:`engine.observers._ObservedToolHandle` emits when
    :class:`errors.ToolUnauthorized` fires."""
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


def test_denied_capsule_renders_with_perm_hint() -> None:
    """The capsule names the tool, the matched rule, and the exact
    /perm command — the load-bearing usability property."""
    out = _io.StringIO()
    renderer = Renderer(stdout=out, agent_name="test")
    renderer._render_tool_failed(_denied_event())
    output = out.getvalue()
    # Source label flips to `permission` (orange palette), not `tool`.
    assert "permission" in output
    assert "denied: bash" in output
    assert "what='kolsch/tools/bash'" in output
    assert "pattern='*'" in output
    assert "/perm allow kolsch/tools/bash *" in output


def test_denied_capsule_legacy_rule_renders_without_pattern() -> None:
    """A legacy-shape matched rule (``who`` present) renders cleanly
    without a `pattern=` field — both schema shapes work."""
    out = _io.StringIO()
    renderer = Renderer(stdout=out, agent_name="test")
    legacy_rule = {
        "id": "rl_abc",
        "who": ".*",
        "what": r"tool\.bash",
        "effect": "deny",
    }
    renderer._render_tool_failed(
        _denied_event(
            matched_rule=legacy_rule,
            recovery_hint="/perm allow kolsch/tools/bash *",
        ),
    )
    output = out.getvalue()
    assert "permission" in output
    assert "pattern=" not in output  # legacy → no pattern field
    assert "/perm allow kolsch/tools/bash *" in output


def test_generic_tool_failure_still_renders_tool_capsule() -> None:
    """A non-authz failure (no recovery_hint) keeps the dim
    cyan tool-failure capsule — the new path is opt-in."""
    out = _io.StringIO()
    renderer = Renderer(stdout=out, agent_name="test")
    error = Error(
        type="tool.execution_failed",
        message="kaboom",
        where="tools.dispatch.invoke",
        detail={},
    )
    event = Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        type=EventType.TOOL_FAILED,
        actor=Actor(kind="agent", id="ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        created_at=datetime.now(UTC),
        data=ToolFailedData(
            tool_call_id="tc_1",
            tool_name="bash",
            error=error,
            recovery_hint=None,
        ).model_dump(mode="json"),
    )
    renderer._render_tool_failed(event)
    output = out.getvalue()
    # Permission capsule NOT used; generic `tool` capsule is.
    assert "denied:" not in output
    assert "tool.execution_failed" in output


# ===========================================================================
# Group 4 — ToolFailedData accepts recovery_hint
# ===========================================================================


def test_tool_failed_data_accepts_recovery_hint() -> None:
    """Schema-level: the field round-trips through model_dump."""
    error = Error(type="tool.unauthorized", message="m", where="w", detail={})
    data = ToolFailedData(
        tool_call_id="tc_1",
        tool_name="bash",
        error=error,
        recovery_hint='/perm allow kolsch/tools/bash "find *"',
    )
    dump = data.model_dump(mode="json")
    assert dump["recovery_hint"] == '/perm allow kolsch/tools/bash "find *"'


def test_tool_failed_data_recovery_hint_defaults_to_none() -> None:
    """Backward-compat: existing producers that don't set the field
    still construct cleanly with a None default."""
    error = Error(type="tool.execution_failed", message="m", where="w", detail={})
    data = ToolFailedData(
        tool_call_id="tc_1",
        tool_name="bash",
        error=error,
    )
    assert data.recovery_hint is None


# ===========================================================================
# Group 5 — engine derives the recovery hint
# ===========================================================================


def test_engine_derives_hint_for_bash_authz_denial() -> None:
    """The engine helper produces ``/perm allow <tool_ref> "<first-token> *"``
    for a bash invocation that was denied by a new-shape rule."""
    from engine.observers import _derive_authz_recovery_hint
    from errors import ToolUnauthorized

    exc = ToolUnauthorized(
        tool_name="bash",
        action="tool.bash",
        principal="agent.test",
        matched_rule={"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
    )
    hint = _derive_authz_recovery_hint(exc, arguments={"command": "find / -name foo"})
    assert hint == '/perm allow kolsch/tools/bash "find *"'


def test_engine_skips_hint_when_matched_rule_lacks_qualified_ref() -> None:
    """Legacy matched rule (regex `what`) → no hint (we'd have to
    guess the qualified ref, and a wrong guess is worse than no hint)."""
    from engine.observers import _derive_authz_recovery_hint
    from errors import ToolUnauthorized

    exc = ToolUnauthorized(
        tool_name="bash",
        action="tool.bash",
        principal="agent.test",
        matched_rule={
            "who": ".*",
            "what": r"tool\.bash",
            "effect": "deny",
        },
    )
    hint = _derive_authz_recovery_hint(exc, arguments={"command": "find /"})
    assert hint is None


def test_engine_hint_uses_star_when_no_args() -> None:
    """No args → `*` pattern, since we have nothing better to suggest."""
    from engine.observers import _derive_authz_recovery_hint
    from errors import ToolUnauthorized

    exc = ToolUnauthorized(
        tool_name="bash",
        action="tool.bash",
        principal="agent.test",
        matched_rule={"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
    )
    hint = _derive_authz_recovery_hint(exc, arguments={})
    assert hint == "/perm allow kolsch/tools/bash *"

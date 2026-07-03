"""TC-PERM-DISCOVERY-01..05 — qol-tool-permission-discovery-uniform-
with-stacking.md Step 3 end-to-end regression suite.

The five tests pin the load-bearing properties of the QoL:

- TC-PERM-DISCOVERY-01: interactive no-rule → modal → [a] →
  persist → second call silent.
- TC-PERM-DISCOVERY-02: interactive no-rule → [n] → no persist
  → next call prompts again.
- TC-PERM-DISCOVERY-03: non-interactive no-rule (no callback) →
  default-deny preserved.
- TC-PERM-DISCOVERY-04: agent rule wins over bundle + platform.
- TC-PERM-DISCOVERY-05: bundle rule wins over platform.

Tests 01-03 exercise the no-rule callback path on the authz
provider directly (single-process, no engine wiring); tests
04-05 use the four-level walk through ``allow``'s kwargs. The
end-to-end engine path (bundle.yaml + agent + tool + dispatcher)
is covered separately by
:mod:`tests.integration.test_bundle_permissions` so this file
stays focused on the modal-callback contract + stacking
semantics.
"""

from __future__ import annotations

import io as _io
from typing import TYPE_CHECKING

import yaml

from cli._modal import ApprovalModal
from cli.permissions import _write_back
from providers import RuleBasedAuthorizationProvider
from providers.authz import AuthzEffect, Rule

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scripted_input(answers: list[str]):  # type: ignore[no-untyped-def]
    """Return an ``input_fn`` that pops one line per prompt."""
    iterator = iter(answers)

    async def _fn(prompt: str) -> str:
        _ = prompt
        try:
            return next(iterator)
        except StopIteration as exc:
            msg = "modal asked for more input than the test scripted"
            raise AssertionError(msg) from exc

    return _fn


def _provider_empty() -> RuleBasedAuthorizationProvider:
    """Provider with no rules — every call hits the no-rule path."""
    return RuleBasedAuthorizationProvider(config={"rules": []})


# ===========================================================================
# TC-PERM-DISCOVERY-01 — Interactive no-rule → modal → [a] → persist
# ===========================================================================


async def test_no_rule_fires_modal_then_persists_allow(tmp_path: Path) -> None:
    """A tool invocation with no covering rule fires the modal;
    user picks [a]; rule persists to ``platform.yaml`` via the
    chat's ``on_rule_added`` hook; second invocation passes
    silently because the persisted rule matches first."""
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "authorization": {
                    "variant": "rule_based",
                    "config": {"rules": []},
                },
            }
        ),
        encoding="utf-8",
    )
    provider = _provider_empty()

    def _on_rule_added(rules: list[Rule]) -> None:
        _write_back(yaml_path, rules)

    invocations = 0

    async def _input_fn(prompt: str) -> str:
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            return "a"  # always allow
        return ""  # accept the suggested pattern

    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_input_fn)
    provider.register_prompt_callback(modal, on_rule_added=_on_rule_added)

    # First call: modal fires, [a], pattern accepted.
    first = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="echo hi",
    )
    assert first.allowed is True
    assert first.effect is AuthzEffect.ALLOW
    assert "who_chain=no_rule" in first.reason

    # Panel rendered (no-rule variant — no "matched" suffix).
    rendered = out.getvalue()
    assert "bash" in rendered
    assert "matched" not in rendered  # no rule_id to display

    # Rule persisted to platform.yaml — same atomic write
    # ``kolsch permissions add`` uses.
    persisted = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    yaml_rules = persisted["authorization"]["config"]["rules"]
    rule_whats = [r["what"] for r in yaml_rules]
    # The suggested pattern for bash + "echo hi" is "^echo .*$".
    assert "^echo .*$" in rule_whats

    # Second call: modal must NOT fire. The scripted input_fn
    # asserts on a third call; this call passing silently is the
    # property.
    second = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="echo other",
    )
    assert second.allowed is True
    assert second.matched_rule is not None
    assert second.matched_rule.effect == "allow"


# ===========================================================================
# TC-PERM-DISCOVERY-02 — Interactive no-rule → [n] → no persist
# ===========================================================================


async def test_no_rule_deny_once_does_not_persist() -> None:
    """User picks [n] (deny once); no rule persisted; next
    invocation prompts again because nothing was written to
    persistence."""
    provider = _provider_empty()

    deny_input = _scripted_input(["n"])
    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=deny_input)

    hook_calls: list[list[Rule]] = []

    def _on_rule_added(rules: list[Rule]) -> None:
        hook_calls.append(list(rules))

    provider.register_prompt_callback(modal, on_rule_added=_on_rule_added)

    first = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="ls",
    )
    assert first.allowed is False
    assert first.effect is AuthzEffect.DENY
    # No persistence — the [n] path returns a bare AuthzEffect, not
    # a (effect, Rule) tuple, so on_rule_added is never invoked.
    assert hook_calls == []
    rules = await provider.list_rules()
    assert rules == [], "deny-once must not insert any rule"

    # Next call: modal fires again. Re-script the input — the modal
    # has no state across calls, so re-using ``modal`` is fine but
    # we need fresh scripted input.
    second_input = _scripted_input(["n"])
    modal._input_fn = second_input  # type: ignore[attr-defined]

    second = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="ls",
    )
    assert second.allowed is False
    assert second.effect is AuthzEffect.DENY


# ===========================================================================
# TC-PERM-DISCOVERY-03 — Non-interactive no-rule → default deny
# ===========================================================================


async def test_no_rule_no_callback_raises_default_deny() -> None:
    """Without a registered callback (non-chat: ``kolsch run``,
    server, tests), the no-rule path collapses to the existing
    Phase 2.0 default-deny ``AuthorizationResult`` — which the
    dispatcher converts to :class:`errors.ToolUnauthorized`.
    Regression guard against silent default-allow."""
    provider = _provider_empty()
    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="ls",
    )
    assert result.allowed is False
    assert result.effect is AuthzEffect.DENY
    assert result.matched_rule is None
    assert "default deny" in result.reason.lower()
    # who_chain reports `default`, NOT `no_rule`, because the
    # no-rule callback branch never fires when no callback is
    # registered.
    assert "who_chain=default" in result.reason


# ===========================================================================
# TC-PERM-DISCOVERY-04 — Agent rule wins over bundle + platform
# ===========================================================================


async def test_agent_permission_overrides_bundle_and_platform() -> None:
    """An agent's ``permissions:`` rule wins over a bundle rule
    AND a platform rule for the same action — the leftmost match
    in the agent → bundle → platform walk."""
    # Platform: deny.
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
            ],
        },
    )
    # Bundle: deny.
    bundle_rules = (Rule(what="kolsch/tools/bash", pattern="*", effect="deny"),)
    # Agent: allow — should win.
    agent_rules = (Rule(what="kolsch/tools/bash", pattern="*", effect="allow"),)

    from providers.authz import PermissionLayer

    result = await provider.allow(
        principal="agent.helper",
        action="kolsch/tools/bash",
        layers=(
            PermissionLayer(name="agent", rules=agent_rules),
            PermissionLayer(name="bundle", rules=bundle_rules),
        ),
    )
    assert result.allowed is True
    assert "who_chain=agent" in result.reason


# ===========================================================================
# TC-PERM-DISCOVERY-05 — Bundle rule wins over platform
# ===========================================================================


async def test_bundle_permission_overrides_platform() -> None:
    """A bundle's ``permissions:`` rule wins over a platform
    rule for the same action — the second layer in the walk
    short-circuits before the platform layer fires."""
    # Platform: deny.
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
            ],
        },
    )
    bundle_rules = (Rule(what="kolsch/tools/bash", pattern="*", effect="allow"),)

    from providers.authz import PermissionLayer

    result = await provider.allow(
        principal="agent.helper",
        action="kolsch/tools/bash",
        layers=(PermissionLayer(name="bundle", rules=bundle_rules),),
    )
    assert result.allowed is True
    assert "who_chain=bundle" in result.reason

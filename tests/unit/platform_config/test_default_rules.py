"""Unit tests for `assets.default_rules` and the fail-closed default — bug-157.

Three contracts:

1. `assets.default_rules` is the single source of the default rules,
   and every helper returns fresh objects.
2. `cli._setup._workspace_only_default_rules()` (setup's set) is
   unchanged by the move: equal to a frozen copy of the 13 rules.
3. `default_platform_payload()`'s authorization is the fail-closed
   pair — no unconditional catch-all `allow`, tail last.
"""

from __future__ import annotations

import ast
from pathlib import Path

from assets.default_rules import (
    agent_execute_preflight_rule,
    default_deny_tail_rule,
    fail_closed_default_rules,
    workspace_only_default_rules,
)
from assets.platform_config import default_platform_payload
from cli._setup import _setup_platform_payload, _workspace_only_default_rules
from providers.authz import inspect_default_deny_tail

_TAIL = {"what": "*", "pattern": "*", "effect": "deny"}
_PREFLIGHT = {"what": "agent.execute.*", "pattern": "*", "effect": "allow"}

# Frozen copy of setup's rule set as it stood before the move. A change
# here is a change to every new workspace's permissions — review it.
_FROZEN_WORKSPACE_ONLY_RULES = [
    {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/list_files", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/write_file", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/bash", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/fetch_url", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/browse_url", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/run_workflow", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/workflow_status", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/cancel_workflow", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/workflow_inspect", "pattern": "*", "effect": "allow"},
    {"what": "*", "pattern": "*", "effect": "deny"},
]

_SRC = Path(__file__).resolve().parents[3] / "src"


def test_helpers_return_expected_rules() -> None:
    assert default_deny_tail_rule() == _TAIL
    assert agent_execute_preflight_rule() == _PREFLIGHT
    assert fail_closed_default_rules() == [_PREFLIGHT, _TAIL]


def test_helpers_return_fresh_objects() -> None:
    for factory in (
        default_deny_tail_rule,
        agent_execute_preflight_rule,
    ):
        first = factory()
        first["effect"] = "mutated"
        assert factory()["effect"] != "mutated"

    for list_factory in (fail_closed_default_rules, workspace_only_default_rules):
        rules = list_factory()
        rules[0]["effect"] = "mutated"
        rules.append({"what": "extra"})
        fresh = list_factory()
        assert fresh[0]["effect"] == "allow"
        assert {"what": "extra"} not in fresh


def test_workspace_only_rules_unchanged_by_move() -> None:
    assert workspace_only_default_rules() == _FROZEN_WORKSPACE_ONLY_RULES
    assert _workspace_only_default_rules() == _FROZEN_WORKSPACE_ONLY_RULES
    rules = workspace_only_default_rules()
    assert rules[0] == agent_execute_preflight_rule()
    assert rules[-1] == default_deny_tail_rule()


def test_setup_payload_uses_workspace_only_rules() -> None:
    payload = _setup_platform_payload()
    assert payload["authorization"] == {
        "variant": "rule_based",
        "config": {"rules": _FROZEN_WORKSPACE_ONLY_RULES},
    }
    assert payload["defaults"] == {}


def test_default_payload_authorization_is_fail_closed() -> None:
    authz = default_platform_payload()["authorization"]
    assert authz == {
        "variant": "rule_based",
        "config": {"rules": fail_closed_default_rules()},
    }
    rules = authz["config"]["rules"]
    status = inspect_default_deny_tail(rules)
    assert status.state == "last"
    assert status.catch_all_allow_index is None
    assert rules[-1] == _TAIL
    for rule in rules:
        legacy_catch_all = rule.get("who") == ".*" or rule.get("what") == ".*"
        new_catch_all = rule.get("what") == "*" and rule.get("pattern", "*") == "*"
        if rule["effect"] == "allow":
            assert not legacy_catch_all
            assert not new_catch_all


def test_default_rules_module_imports_no_cli_or_providers() -> None:
    tree = ast.parse((_SRC / "assets" / "default_rules.py").read_text("utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [m for m in imported if m.split(".")[0] in {"cli", "providers"}]


def test_rule_literal_no_longer_in_cli() -> None:
    setup_src = (_SRC / "cli" / "_setup.py").read_text("utf-8")
    assert '"kolsch/tools/read_file"' not in setup_src

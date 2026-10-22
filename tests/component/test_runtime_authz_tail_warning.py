"""bug-152 — `bootstrap_runtime` warns once when the default-deny tail is off.

A rule set with no default-deny tail, or with the tail somewhere other
than last, still loads and enforces exactly as before; bootstrap logs
one `platform_runtime` WARNING per process per workspace root naming
the problem and the fix. Direct `RuleBasedAuthorizationProvider(...)`
construction (chat `/reload`, `kolsch permissions`, the workflow
validator) stays silent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from cli._setup import _copy_kolsch_bundle, _setup_platform_payload
from platform_runtime import bootstrap_runtime
from platform_runtime import runtime as runtime_mod
from providers import RuleBasedAuthorizationProvider

if TYPE_CHECKING:
    from pathlib import Path

_FIX = "kolsch permissions add --what '*' --pattern '*' --effect deny"


@pytest.fixture(autouse=True)
def _fresh_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_mod, "_TAIL_WARNED_ROOTS", set())


def _tail_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "platform_runtime"
        and r.levelno == logging.WARNING
        and "default-deny tail" in r.getMessage()
    ]


def _write_workspace(
    root: Path, rules: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Install the kolsch bundle + a setup-shaped platform.yaml.

    `rules=None` keeps setup's default rule set. Returns the rules
    written.
    """
    _copy_kolsch_bundle(root)
    payload = _setup_platform_payload()
    if rules is not None:
        payload["authorization"]["config"]["rules"] = rules
    (root / "platform.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    written: list[dict[str, Any]] = payload["authorization"]["config"]["rules"]
    return written


async def _assert_enforces_yaml(root: Path, rules: list[dict[str, Any]]) -> None:
    runtime = await bootstrap_runtime(root=root)
    loaded = await runtime.authz.list_rules()
    assert [(r.what, r.pattern, r.effect, r.who) for r in loaded] == [
        (r["what"], r.get("pattern", "*"), r["effect"], r.get("who")) for r in rules
    ]


async def test_setup_default_rules_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="platform_runtime")
    rules = _write_workspace(tmp_path, None)
    assert rules[-1] == {"what": "*", "pattern": "*", "effect": "deny"}
    await _assert_enforces_yaml(tmp_path, rules)
    assert _tail_records(caplog) == []


async def test_legacy_tail_last_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="platform_runtime")
    default = _setup_platform_payload()["authorization"]["config"]["rules"]
    rules = [*default[:-1], {"who": ".*", "what": ".*", "effect": "deny"}]
    _write_workspace(tmp_path, rules)
    await bootstrap_runtime(root=tmp_path)
    assert _tail_records(caplog) == []


async def test_tail_removed_warns_once_with_path_and_fix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="platform_runtime")
    rules = _setup_platform_payload()["authorization"]["config"]["rules"][:-1]
    _write_workspace(tmp_path, rules)
    await _assert_enforces_yaml(tmp_path, rules)
    records = _tail_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert message.startswith(f"{tmp_path.resolve() / 'platform.yaml'}: ")
    assert _FIX in message
    assert "ask for approval in `kolsch chat`" in message


async def test_second_bootstrap_same_root_is_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="platform_runtime")
    rules = _setup_platform_payload()["authorization"]["config"]["rules"][:-1]
    _write_workspace(tmp_path, rules)
    await bootstrap_runtime(root=tmp_path)
    await bootstrap_runtime(root=tmp_path)
    assert len(_tail_records(caplog)) == 1


async def test_distinct_roots_each_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="platform_runtime")
    rules = _setup_platform_payload()["authorization"]["config"]["rules"][:-1]
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write_workspace(a, rules)
    _write_workspace(b, rules)
    await bootstrap_runtime(root=a)
    await bootstrap_runtime(root=b)
    assert len(_tail_records(caplog)) == 2


async def test_tail_not_last_names_position(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="platform_runtime")
    default = _setup_platform_payload()["authorization"]["config"]["rules"]
    rules = [default[-1], *default[:-1]]
    _write_workspace(tmp_path, rules)
    await _assert_enforces_yaml(tmp_path, rules)
    records = _tail_records(caplog)
    assert len(records) == 1
    assert f"position 1 of {len(rules)}" in records[0].getMessage()
    assert f"the {len(rules) - 1} rules after it never match" in records[0].getMessage()


async def test_scoped_deny_last_is_not_a_tail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="platform_runtime")
    default = _setup_platform_payload()["authorization"]["config"]["rules"]
    rules = [
        *default[:-1],
        {"what": "*", "pattern": "*", "effect": "deny", "from": "kolsch/agents/helper"},
    ]
    _write_workspace(tmp_path, rules)
    await bootstrap_runtime(root=tmp_path)
    assert len(_tail_records(caplog)) == 1


async def test_bare_default_payload_mentions_catch_all_allow(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="platform_runtime")
    # No platform.yaml: `load_or_create` writes the first-run
    # allow-all payload (F1 — no tail).
    runtime = await bootstrap_runtime(root=tmp_path)
    loaded = await runtime.authz.list_rules()
    assert [(r.who, r.what, r.effect) for r in loaded] == [(".*", ".*", "allow")]
    records = _tail_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "catch-all allow rule at position 1" in message
    assert "ask for approval" not in message


async def test_direct_provider_construction_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    RuleBasedAuthorizationProvider(
        config={
            "rules": [{"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"}]
        }
    )
    assert [r for r in caplog.records if "default-deny tail" in r.getMessage()] == []

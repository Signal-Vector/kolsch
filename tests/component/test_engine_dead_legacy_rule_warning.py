"""bug-159 D4 — the engine warns once per agent layer about dead legacy rules.

A legacy `what: "tool\\.bash"` rule in an agent's or bundle's
`permissions:` can never match a registered tool (actions are
`<bundle>/tools/<name>`). The engine's tool registration
(`_Exchange._register_tools_with_subsystem`) logs one
`config.deprecated_regex_rule` WARNING per such rule — and, because it
runs once per exchange, only the first time per `(agent_id, layer)` in
the process. This drives two real exchanges for the same agent and pins
both halves.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
import yaml

import engine.engine as engine_module
from engine import ExecutionContext, LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path

_DEAD_RULE = {"who": ".*", "what": r"tool\.bash", "effect": "prompt"}


@pytest.fixture(autouse=True)
def _fresh_dead_rule_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the process-wide dedup set so order / xdist can't matter."""
    monkeypatch.setattr(engine_module, "_DEAD_LEGACY_RULES_WARNED", set())


def _setup_workspace(root: Path) -> None:
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _append_permission(path: Path, rule: dict[str, str]) -> None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    payload["permissions"] = [*(payload.get("permissions") or []), rule]
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


async def _llm(request: LLMRequest) -> LLMResponse:
    _ = request
    return LLMResponse(content="ok")


def _dead_rule_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "never matches" in r.getMessage()
    ]


async def test_engine_warns_once_per_layer_then_stays_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _setup_workspace(root)
    bundle_dir = root / "bundles" / "kolsch"
    _append_permission(bundle_dir / "agents" / "helper.yaml", _DEAD_RULE)
    _append_permission(bundle_dir / "bundle.yaml", _DEAD_RULE)

    runtime = await bootstrap_runtime(root=root, llm=_llm)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    agent_id = str(config.agent_id)

    async def _exchange(name: str) -> None:
        session = await runtime.store.create_session(agent_id, name=name)
        threads = await runtime.store.list_threads(session.session_id)
        ctx = ExecutionContext(
            thread_id=threads[0].thread_id,
            trigger={"kind": "user_message", "content": "hi"},
        )
        _ = [e async for e in runtime.engine.execute_stream(config, ctx)]

    with caplog.at_level(logging.WARNING, logger="providers.authz"):
        await _exchange("dead-rule-1")
    first = _dead_rule_warnings(caplog)
    assert len([m for m in first if f"agent {agent_id} agent permissions" in m]) == 1
    assert len([m for m in first if f"agent {agent_id} bundle permissions" in m]) == 1
    assert len(first) == 2

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="providers.authz"):
        await _exchange("dead-rule-2")
    assert _dead_rule_warnings(caplog) == []

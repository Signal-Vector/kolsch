"""spec-136 M2 — the starter ``helper`` agent and ``jev_decide``.

Test 4 (AC 10) plus the feat-139 behaviour the helper now depends on:

- A real ``kolsch setup --non-interactive`` workspace (mock provider,
  no ``OPENROUTER_API_KEY``) runs the helper cleanly: no
  ``tool.setup_failed``, one ``tool.unavailable`` for ``jev_decide``,
  and the helper's Jev prompt paragraph is omitted.
- With the key present the tool is registered, the paragraph renders,
  and a MockLLM ``ToolCallResponse`` for ``jev_decide`` (pointed at a
  local fake Jev server) produces ``tool.called`` → ``tool.result``.

No network: the workspace copy of ``jev_decide.py`` is pointed at a
``127.0.0.1`` fake server, and the key is a dummy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import yaml

from engine import LLMResponse
from engine.context import ExecutionContext
from platform_runtime import bootstrap_runtime
from tests.component.test_jev_decide import _FakeJevServer, _ok
from tests.fixtures.assertions import assert_event_content, assert_event_sequence
from tests.fixtures.mock_llm import MockLLM, TextResponse, ToolCallResponse

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from engine.framework import LLMRequest

pytestmark = pytest.mark.slow

_JEV_PARAGRAPH = "For a bounded judgement"
_DUMMY_KEY = "sk-or-m2-test-dummy"


def _kolsch_setup(root: Path) -> None:
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env.pop("KOLSCH_PROVIDER", None)
    env.pop("KOLSCH_PROVIDER_KEY", None)
    env.pop("OPENROUTER_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"setup failed rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    )


@pytest.fixture(scope="module")
def _setup_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("helper_jev_template")
    _kolsch_setup(root)
    return root


@pytest.fixture
def workspace(_setup_template: Path, tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(_setup_template, root)
    return root


@pytest.fixture
def fake_jev() -> Iterator[_FakeJevServer]:
    server = _FakeJevServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _connect_fake_openrouter(root: Path, server: _FakeJevServer) -> None:
    """Add the dummy key and point the workspace tool at the fake server."""
    with (root / ".env").open("a", encoding="utf-8") as fh:
        fh.write(f"OPENROUTER_API_KEY={_DUMMY_KEY}\n")
    tool_file = root / "bundles" / "kolsch" / "tools" / "jev_decide.py"
    src = tool_file.read_text(encoding="utf-8")
    assert 'base_url="https://openrouter.ai/api"' in src
    tool_file.write_text(
        src.replace(
            'base_url="https://openrouter.ai/api"', f'base_url="{server.base_url}"'
        ),
        encoding="utf-8",
    )


def _allow_jev(root: Path) -> None:
    """Swap the default `prompt` rule to `allow` (no approval UI in tests)."""
    path = root / "platform.yaml"
    plat = yaml.safe_load(path.read_text(encoding="utf-8"))
    for rule in plat["authorization"]["config"]["rules"]:
        if rule.get("what") == "kolsch/tools/jev_decide":
            rule["effect"] = "allow"
    path.write_text(yaml.safe_dump(plat, sort_keys=False), encoding="utf-8")


class _MockLLMDriver:
    """Adapts the scripted MockLLM to the engine's ``LLMRequest`` seam."""

    def __init__(self, mock: MockLLM) -> None:
        self.mock = mock
        self.requests: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        response = await self.mock.agenerate({"request": request})
        if isinstance(response, ToolCallResponse):
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": response.tool_call_id or f"tc_{uuid4()}",
                        "name": response.tool,
                        "arguments": dict(response.args),
                    }
                ],
            )
        assert isinstance(response, TextResponse)
        return LLMResponse(content=response.text)


async def _run_helper(root: Path, driver: _MockLLMDriver) -> list[dict[str, Any]]:
    runtime = await bootstrap_runtime(root=root, llm=driver)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(str(config.agent_id), name="m2")
    (thread,) = await runtime.store.list_threads(session.session_id)
    await runtime.engine.execute(
        config,
        ExecutionContext(
            thread_id=thread.thread_id,
            trigger={"kind": "user_message", "content": "classify my ticket"},
        ),
    )
    path = next((root / "sessions").rglob(f"{session.session_id}*.jsonl"))
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _system_prompt(driver: _MockLLMDriver) -> str:
    return next(m.content for m in driver.requests[0].messages if m.role == "system")


async def test_helper_jev_hidden_without_openrouter_key(workspace: Path) -> None:
    """Mock-provider workspace, no key: helper works, Jev hidden + recorded."""
    driver = _MockLLMDriver(MockLLM(script=[TextResponse(text="hello")]))
    events = await _run_helper(workspace, driver)
    types = [e["type"] for e in events]

    assert "thread.exchange_failed" not in types
    assert "thread.exchange_completed" in types
    assert "tool.setup_failed" not in json.dumps(events)
    (unavailable,) = [e for e in events if e["type"] == "tool.unavailable"]
    assert_event_content(
        unavailable,
        tool_name="jev_decide",
        missing_secrets=["OPENROUTER_API_KEY"],
        recovery_hint="Run `kolsch connect openrouter`.",
    )
    assert "jev_decide" not in {t["name"] for t in driver.requests[0].tools}
    prompt = _system_prompt(driver)
    assert _JEV_PARAGRAPH not in prompt
    # The guard leaves no stray blank lines behind.
    assert "don't need.\n\n## Your documentation" in prompt


async def test_helper_jev_declared_and_called_with_key(
    workspace: Path, fake_jev: _FakeJevServer
) -> None:
    """Test 4: key present → tool + paragraph present; a scripted call
    produces ``tool.called`` → ``tool.result`` through the real dispatch."""
    _connect_fake_openrouter(workspace, fake_jev)
    _allow_jev(workspace)
    fake_jev.replies = [
        _ok(
            {
                "team": {
                    "type": "choice",
                    "choice": "outage",
                    "confidence": 0.97,
                    "probabilities": {"billing": 0.01, "outage": 0.97, "feature": 0.02},
                }
            }
        )
    ]
    args = {
        "state": "the site is returning 502s",
        "questions": {
            "team": {
                "type": "choice",
                "instructions": "Which team should handle this ticket?",
                "criteria": {"billing": None, "outage": None, "feature": None},
            }
        },
    }
    driver = _MockLLMDriver(
        MockLLM(
            script=[
                ToolCallResponse(tool="jev_decide", args=args, tool_call_id="tc_jev_1"),
                TextResponse(text="Outage (confidence 0.97)."),
            ]
        )
    )
    events = await _run_helper(workspace, driver)

    assert "jev_decide" in {t["name"] for t in driver.requests[0].tools}
    assert _JEV_PARAGRAPH in _system_prompt(driver)
    assert not [e for e in events if e["type"] == "tool.unavailable"]
    tool_events = [
        e for e in events if e["type"] in ("tool.called", "tool.result", "tool.failed")
    ]
    assert_event_sequence(tool_events, ["tool.called", "tool.result"])
    assert_event_content(tool_events[0], tool_name="jev_decide", arguments=args)
    assert_event_content(tool_events[1], tool_name="jev_decide")
    assert tool_events[1]["data"]["result"]["answers"]["team"]["choice"] == "outage"
    assert fake_jev.seen[0].headers["authorization"] == f"Bearer {_DUMMY_KEY}"
    assert _DUMMY_KEY not in json.dumps(events)


def test_helper_jev_default_rule_prompts_before_deny_tail(workspace: Path) -> None:
    """AC 8 as shipped to a real workspace: `prompt`, above the deny tail."""
    plat = yaml.safe_load((workspace / "platform.yaml").read_text(encoding="utf-8"))
    rules = plat["authorization"]["config"]["rules"]
    jev = [r for r in rules if r.get("what") == "kolsch/tools/jev_decide"]
    assert jev == [
        {"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": "prompt"}
    ]
    assert rules.index(jev[0]) < len(rules) - 1
    assert rules[-1]["effect"] == "deny"

"""Integration tests — ``from:``-scoped tool rules see the caller chain.

bug-158: a rule scoped with ``from: <bundle>/agents/<name>`` must only
fire for tool calls made by that agent. The engine has to build the
exchange's caller chain (the agent's own catalog ref first) and pass it
into every tool authorization call; an empty chain never matches a
scoped rule, so without it a scoped deny is ignored and a scoped allow
never grants.

Both scenarios go through the real path: a ``kolsch setup`` workspace,
rules written by ``kolsch permissions add --from`` in a subprocess,
``bootstrap_runtime``, the real engine, and a Mock LLM that asks for
one ``bash`` call. Two agents share the workspace — ``helper`` (A, the
rule's target) and ``helper_b`` (B, a copy under another name) — and
the assertions are on the emitted ``tool.result`` / ``tool.failed``
events.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml

from engine import ExecutionContext, LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
from schemas import Event, EventType
from tests.fixtures.mock_llm import (
    MockLLM,
    TextResponse,
    ToolCallResponse,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


_AGENT_A = "kolsch/agents/helper"
_AGENT_B = "kolsch/agents/helper_b"


# ---------------------------------------------------------------------------
# Workspace fixture — initialised via ``kolsch setup``
# ---------------------------------------------------------------------------


def _cli_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env["PATH"] = "/usr/bin:/bin"
    return env


def _run_cli(root: Path, *argv: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "cli", *argv],
        env=_cli_env(root),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"kolsch {' '.join(argv)} failed: rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)
    return result.stdout


def _setup_workspace(root: Path) -> None:
    """``kolsch setup``, then add agent B and strip both agents'
    agent-level ``bash → prompt`` rule so the platform layer (where
    ``kolsch permissions add`` writes) drives the decision."""
    _run_cli(root, "setup", "--non-interactive")
    agents_dir = root / "bundles" / "kolsch" / "agents"
    helper = yaml.safe_load((agents_dir / "helper.yaml").read_text(encoding="utf-8"))
    helper["permissions"] = [
        r
        for r in (helper.get("permissions") or [])
        if not (r.get("what") == "kolsch/tools/bash" and r.get("effect") == "prompt")
    ]
    (agents_dir / "helper.yaml").write_text(
        yaml.safe_dump(helper, sort_keys=False), encoding="utf-8"
    )
    helper_b = dict(helper, agent_id="ag_helper_b", name="helper_b")
    (agents_dir / "helper_b.yaml").write_text(
        yaml.safe_dump(helper_b, sort_keys=False), encoding="utf-8"
    )


def _add_rule(root: Path, *, effect: str, from_: str | None = None) -> None:
    """Insert a ``kolsch/tools/bash`` rule at the top of the platform
    rules. Later calls land above earlier ones."""
    argv = [
        "permissions",
        "add",
        "--what",
        "kolsch/tools/bash",
        "--pattern",
        "*",
        "--effect",
        effect,
        "--first",
    ]
    if from_ is not None:
        argv += ["--from", from_]
    _run_cli(root, *argv)


def _make_mock_llm_callable(
    mock: MockLLM,
) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    """Adapter matching the one in tests/integration/test_bundle_permissions.py."""

    async def _llm(request: LLMRequest) -> LLMResponse:
        context: dict[str, Any] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
            "system_prompt": next(
                (m.content for m in request.messages if m.role == "system"),
                "",
            ),
        }
        response = await mock.agenerate(context)
        if isinstance(response, TextResponse):
            return LLMResponse(content=response.text)
        if isinstance(response, ToolCallResponse):
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": response.tool_call_id or f"tc_{uuid4()}",
                        "name": response.tool,
                        "arguments": dict(response.args),
                    },
                ],
            )
        msg = f"unexpected MockLLM response shape: {response!r}"
        raise RuntimeError(msg)

    return _llm


async def _run_bash_turn(root: Path, agent_ref: str, marker: str) -> list[Event]:
    """One exchange for ``agent_ref`` whose model asks for a single
    ``bash`` call, through a freshly bootstrapped runtime."""
    mock = MockLLM(
        script=[
            ToolCallResponse(
                tool="bash",
                args={"command": f"echo {marker}"},
                tool_call_id=f"tc_{marker}",
            ),
            TextResponse(text=f"Done — {marker}"),
        ],
    )
    runtime = await bootstrap_runtime(root=root, llm=_make_mock_llm_callable(mock))
    config = await runtime.build_resolved_config(agent_ref)
    session = await runtime.store.create_session(str(config.agent_id), name=marker)
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": f"run echo {marker}"},
    )
    return [e async for e in runtime.engine.execute_stream(config, ctx)]


def _tool_outcome(events: list[Event]) -> Event:
    """The single bash terminal event (``tool.result`` or ``tool.failed``)."""
    terminals = [
        e for e in events if e.type in (EventType.TOOL_RESULT, EventType.TOOL_FAILED)
    ]
    assert len(terminals) == 1, (
        f"expected exactly one tool terminal; got types {[e.type for e in events]}"
    )
    return terminals[0]


def _describe(event: Event) -> str:
    if event.type == EventType.TOOL_FAILED:
        error = event.data["error"]
        return f"tool.failed({error['type']}, detail={error.get('detail')})"
    return f"{event.type}"


def _matched_rule_from(detail: dict[str, Any]) -> str | None:
    """The matched rule's ``from:`` scope (dumped as ``from_``)."""
    matched = detail.get("matched_rule") or {}
    value = matched.get("from_")
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# AC1 — a scoped deny blocks only the agent it names
# ---------------------------------------------------------------------------


async def test_scoped_deny_blocks_only_that_agent(tmp_path: Path) -> None:
    """``{bash, from: A, deny}`` above a broad bash allow: A's call is
    denied with the chain recorded, B's call runs."""
    root = tmp_path / "ws"
    root.mkdir()
    _setup_workspace(root)
    _add_rule(root, effect="allow")
    _add_rule(root, effect="deny", from_=_AGENT_A)

    a_outcome = _tool_outcome(await _run_bash_turn(root, _AGENT_A, "scoped-deny-a"))
    b_outcome = _tool_outcome(await _run_bash_turn(root, _AGENT_B, "scoped-deny-b"))

    assert a_outcome.type == EventType.TOOL_FAILED, (
        f"agent A must be blocked by the A-scoped deny; got {_describe(a_outcome)}"
    )
    error = a_outcome.data["error"]
    assert error["type"] == "tool.unauthorized"
    assert _matched_rule_from(error["detail"]) == _AGENT_A
    assert error["detail"]["caller_chain"] == [_AGENT_A]

    assert b_outcome.type == EventType.TOOL_RESULT, (
        f"agent B is outside the scoped deny; got {_describe(b_outcome)}"
    )


# ---------------------------------------------------------------------------
# AC2 — a scoped allow grants only the agent it names
# ---------------------------------------------------------------------------


async def test_scoped_allow_allows_only_that_agent(tmp_path: Path) -> None:
    """``{bash, from: A, allow}`` is the only rule that grants bash
    (a broad bash deny sits right below it): A's call runs, B's call
    is denied."""
    root = tmp_path / "ws"
    root.mkdir()
    _setup_workspace(root)
    _add_rule(root, effect="deny")
    _add_rule(root, effect="allow", from_=_AGENT_A)

    a_outcome = _tool_outcome(await _run_bash_turn(root, _AGENT_A, "scoped-allow-a"))
    b_outcome = _tool_outcome(await _run_bash_turn(root, _AGENT_B, "scoped-allow-b"))

    assert a_outcome.type == EventType.TOOL_RESULT, (
        f"agent A must be granted by the A-scoped allow; got {_describe(a_outcome)}"
    )

    assert b_outcome.type == EventType.TOOL_FAILED, (
        f"agent B is outside the scoped allow; got {_describe(b_outcome)}"
    )
    error = b_outcome.data["error"]
    assert error["type"] == "tool.unauthorized"
    assert _matched_rule_from(error["detail"]) is None
    assert error["detail"]["caller_chain"] == [_AGENT_B]

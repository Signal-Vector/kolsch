"""bug-142 Test 3 (chat surface) — resume / ``/fork`` on an invalid workflow.

The chat's pending-workflow resume and ``/fork`` render the validator's
message as a one-line notice instead of crashing the turn. A rejected
resume keeps the reply expectation armed (the run is still paused); a
rejected ``/fork`` restores ``pending_workflow_run_id`` (the forked run
was discarded).
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
from typing import TYPE_CHECKING, Any

from cli._slash import ChatSessionState, SlashContext, dispatch_slash
from cli.chat.session import _resume_pending_workflow
from cli.main import main
from kolsch._yamlio import dump
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path

_PAUSING: dict[str, Any] = {
    "nodes": {
        "ask": {"type": "kolsch/nodes/user_input", "prompt": "go?"},
        "after": {"type": "kolsch/nodes/noop"},
    },
    "graph": {"edges": [{"from": "ask", "to": "after"}]},
}

_LINEAR: dict[str, Any] = {
    "nodes": {"a": {"type": "kolsch/nodes/noop"}, "b": {"type": "kolsch/nodes/noop"}},
    "graph": {"edges": [{"from": "a", "to": "b"}]},
}


def _broken(workflow: dict[str, Any], source: str) -> dict[str, Any]:
    broken = copy.deepcopy(workflow)
    broken["graph"]["edges"].append({"from": source, "to": "ghost"})
    return broken


def _write(root: Path, name: str, workflow: dict[str, Any]) -> str:
    bundle = root / "bundles" / "b142"
    (bundle / "workflows").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        'name: b142\nversion: "0.1"\ndescription: bug-142\n', encoding="utf-8"
    )
    (bundle / "workflows" / f"{name}.yaml").write_text(dump(workflow), encoding="utf-8")
    return f"b142/workflows/{name}"


def _cli_run(root: Path, ref: str) -> str:
    out = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(out):
        rc = main(["--root", str(root), "workflows", "run", ref])
    assert rc == 0, out.getvalue()
    first = out.getvalue().strip().splitlines()[0].split()
    return next(tok for tok in first if tok.startswith("wrk_"))


async def _state(root: Path, pending: str) -> ChatSessionState:
    runtime = await bootstrap_runtime(root=root)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(str(config.agent_id), name="b142")
    threads = await runtime.store.list_threads(session.session_id)
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    state.pending_workflow_run_id = pending
    state.expecting_user_input_response = True
    return state


def test_chat_resume_with_invalid_workflow_validation_shows_notice(
    chat_workspace: Path,
) -> None:
    ref = _write(chat_workspace, "pausing_resume", _PAUSING)
    run_id = _cli_run(chat_workspace, ref)
    _write(chat_workspace, "pausing_resume", _broken(_PAUSING, "ask"))

    async def _go() -> tuple[str, ChatSessionState]:
        state = await _state(chat_workspace, run_id)
        out = io.StringIO()
        await _resume_pending_workflow(state, stdout=out, response_text="yes")
        return out.getvalue(), state

    rendered, state = asyncio.run(_go())
    assert "resume failed:" in rendered
    assert "refers to another step that does not exist" in rendered
    assert f"kolsch workflows validate {ref}" in rendered
    assert "fix the workflow and reply again" in rendered
    assert "Traceback" not in rendered
    assert state.expecting_user_input_response is True
    meta = json.loads((chat_workspace / "runs" / run_id / "meta.json").read_text())
    assert meta["status"] == "waiting_for_input"


def test_chat_fork_with_invalid_workflow_validation_shows_notice(
    chat_workspace: Path,
) -> None:
    ref = _write(chat_workspace, "linear_fork", _LINEAR)
    parent_id = _cli_run(chat_workspace, ref)
    events = [
        json.loads(line)
        for line in (chat_workspace / "runs" / parent_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    fork_at = next(e["event_id"] for e in events if e["kind"] == "workflow.run.started")
    _write(chat_workspace, "linear_fork", _broken(_LINEAR, "b"))
    runs_before = {p.name for p in (chat_workspace / "runs").iterdir()}

    async def _go() -> tuple[str, ChatSessionState]:
        state = await _state(chat_workspace, parent_id)
        out = io.StringIO()
        ctx = SlashContext(
            stdout=out,
            agent_config=state.config,
            authz=state.runtime.authz,
            agent_name=state.config.name,
            state=state,
        )
        await dispatch_slash(f"/fork {fork_at}", ctx)
        return out.getvalue(), state

    rendered, state = asyncio.run(_go())
    assert "/fork:" in rendered
    # The notice is the validator error's `caller_message` (the
    # existing chat convention), so it names the error kind, not `ghost`.
    assert "refers to another step that does not exist" in rendered
    assert f"kolsch workflows validate {ref}" in rendered
    assert "Traceback" not in rendered
    assert state.pending_workflow_run_id == parent_id
    assert {p.name for p in (chat_workspace / "runs").iterdir()} == runs_before

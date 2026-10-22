"""Component tests — workflow producers of the caller chain (bug-158).

Inside a workflow, every tool authorization is judged against the
workflow frames, proximate first:

| Call site                                   | Chain          |
|---------------------------------------------|----------------|
| builtin tool node in W                      | `(W,)`         |
| builtin tool node in child C of parent P    | `(C, P)`       |
| agent node A in W — pre-flight + tool call  | `(A, W)`       |
| agent node A in C (P → C)                   | `(A, C, P)`    |

These run real workflows through the production
`build_workflow_executor` against a `kolsch setup` workspace (so the
builtins are stamped `kolsch/tools/<name>` exactly as in production),
with a stub LLM whose agent asks for one `bash` call. A recorder
around the shared provider's `allow` pins the exact chains, and a
workflow-scoped deny proves the frames are enforced, not just logged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import yaml

from cli._workflow_runtime import build_workflow_executor
from engine import ExecutionContext, LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
from schemas import EventType
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from platform_runtime import PlatformRuntime

_AGENT = "wf/agents/worker"
_SOLO = "wf/workflows/solo"
_PARENT = "wf/workflows/parent"
_CHILD = "wf/workflows/child"
_GUARDED = "wf/workflows/guarded"
_BASH = "kolsch/tools/bash"
_READ_FILE = "kolsch/tools/read_file"

_WORKFLOWS: dict[str, dict[str, Any]] = {
    "solo": {
        "nodes": {
            "run": {"type": "kolsch/nodes/bash", "command": "echo solo"},
            "read": {"type": "kolsch/nodes/read_file", "path": "data/note.txt"},
            "ask": {"type": "kolsch/nodes/agent", "agent": _AGENT},
        },
        "graph": {
            "edges": [{"from": "run", "to": "read"}, {"from": "read", "to": "ask"}]
        },
    },
    "parent": {
        "nodes": {"sub": {"type": "kolsch/nodes/workflow", "ref": _CHILD}},
    },
    "child": {
        "nodes": {
            "run": {"type": "kolsch/nodes/bash", "command": "echo child"},
            "ask": {"type": "kolsch/nodes/agent", "agent": _AGENT},
        },
        "graph": {"edges": [{"from": "run", "to": "ask"}]},
    },
    # The agent node runs first so a W-scoped bash deny is observed on
    # the agent's tool call before the builtin bash node fails the run.
    "guarded": {
        "nodes": {
            "ask": {"type": "kolsch/nodes/agent", "agent": _AGENT},
            "run": {"type": "kolsch/nodes/bash", "command": "echo guarded"},
        },
        "graph": {"edges": [{"from": "ask", "to": "run"}]},
    },
}


def _setup_workspace(root: Path, *, rules: list[dict[str, Any]]) -> None:
    """`kolsch setup`, add the `wf` bundle, and put `rules` on top of the
    platform rules (the protected default-deny tail stays last)."""
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
    assert result.returncode == 0, result.stderr
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "note.txt").write_text("hello\n", encoding="utf-8")

    plat_path = root / "platform.yaml"
    plat = yaml.safe_load(plat_path.read_text(encoding="utf-8"))
    existing = plat["authorization"]["config"]["rules"]
    plat["authorization"]["config"]["rules"] = [*rules, existing[-1]]
    plat_path.write_text(yaml.safe_dump(plat, sort_keys=False), encoding="utf-8")

    bundle = root / "bundles" / "wf"
    (bundle / "agents").mkdir(parents=True)
    (bundle / "workflows").mkdir()
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "wf", "version": "0.1", "description": "wf"}),
        encoding="utf-8",
    )
    (bundle / "agents" / "worker.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_worker",
                "name": "worker",
                "description": "workflow-invoked agent with bash",
                "model": {"provider": "mock", "model": "mock-model"},
                "io": {"mode": "single_shot", "outputs": {"text": {"type": "str"}}},
                "prompt": "Run one command, then answer.",
                "subsystems": {
                    "tools": {"variant": "standard", "config": {"builtin": ["bash"]}}
                },
            }
        ),
        encoding="utf-8",
    )
    for name, doc in _WORKFLOWS.items():
        (bundle / "workflows" / f"{name}.yaml").write_text(
            yaml.safe_dump(doc), encoding="utf-8"
        )


_ALLOW_ALL = {"what": "*", "pattern": "*", "effect": "allow"}


async def _bash_then_answer(request: LLMRequest) -> LLMResponse:
    """Each exchange: ask for one `bash` call, then answer after its result."""
    if any(m.role == "tool" for m in request.messages):
        return LLMResponse(content="done")
    return LLMResponse(
        content=None,
        tool_calls=[
            {"id": "tc_wf", "name": "bash", "arguments": {"command": "echo a"}}
        ],
    )


async def _boot(root: Path) -> PlatformRuntime:
    return await bootstrap_runtime(root=root, llm=_bash_then_answer)


def _record_allow(
    runtime: PlatformRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, tuple[str, ...]]]:
    calls: list[tuple[str, tuple[str, ...]]] = []
    original = runtime.authz.allow

    async def _spy(principal: str, action: str, **kwargs: Any) -> Any:
        calls.append((action, tuple(kwargs["caller_chain"])))
        return await original(principal, action, **kwargs)

    monkeypatch.setattr(runtime.authz, "allow", _spy)
    return calls


async def _run_workflow(
    runtime: PlatformRuntime, root: Path, ref: str
) -> tuple[str, list[dict[str, Any]]]:
    """Run workflow `ref`; return its status and `policy.decision.made`
    payloads from every run JSONL (parent and children)."""
    name = ref.rsplit("/", 1)[-1]
    workflow = Workflow.model_validate(_WORKFLOWS[name])
    store = WorkflowRunStore(root)
    run = store.create(workflow_ref=ref, inputs={})
    executor = build_workflow_executor(
        root=root, runtime=runtime, workflow=workflow, run=run, store=store
    )
    status = await executor.run()
    decisions: list[dict[str, Any]] = []
    for events_path in sorted(root.rglob("events.jsonl")):
        for line in events_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line) if line.strip() else {}
            if event.get("kind") == "policy.decision.made":
                decisions.append(event["payload"])
    return status.value, decisions


async def test_workflow_nodes_and_agent_node_chains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Builtin nodes in W get `(W,)`; agent node A in W gets `(A, W)`
    for both its pre-flight and its tool call."""
    _setup_workspace(tmp_path, rules=[_ALLOW_ALL])
    runtime = await _boot(tmp_path)
    calls = _record_allow(runtime, monkeypatch)

    status, _ = await _run_workflow(runtime, tmp_path, _SOLO)

    assert status == "succeeded"
    assert calls == [
        (_BASH, (_SOLO,)),
        (_READ_FILE, (_SOLO,)),
        ("agent.execute.ag_worker", (_AGENT, _SOLO)),
        (_BASH, (_AGENT, _SOLO)),
    ]


async def test_sub_workflow_chains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P → C: a builtin node in C gets `(C, P)`; agent node A in C gets
    `(A, C, P)`."""
    _setup_workspace(tmp_path, rules=[_ALLOW_ALL])
    runtime = await _boot(tmp_path)
    calls = _record_allow(runtime, monkeypatch)

    status, _ = await _run_workflow(runtime, tmp_path, _PARENT)

    assert status == "succeeded"
    assert calls == [
        (_BASH, (_CHILD, _PARENT)),
        ("agent.execute.ag_worker", (_AGENT, _CHILD, _PARENT)),
        (_BASH, (_AGENT, _CHILD, _PARENT)),
    ]


async def test_workflow_scoped_deny_blocks_agent_tool_call_and_node_in_w(
    tmp_path: Path,
) -> None:
    """AC4 — a `from: W` bash deny blocks the embedded agent's bash call
    (chain `(A, W)`) and W's own bash node (chain `(W,)`); the decisions
    are recorded as `deny` with W in the chain."""
    _setup_workspace(
        tmp_path,
        rules=[
            {"what": _BASH, "pattern": "*", "from": _GUARDED, "effect": "deny"},
            _ALLOW_ALL,
        ],
    )
    runtime = await _boot(tmp_path)

    status, decisions = await _run_workflow(runtime, tmp_path, _GUARDED)

    bash = [(d["effect"], d["caller_chain"]) for d in decisions if d["action"] == _BASH]
    assert bash == [
        ("deny", [_AGENT, _GUARDED]),
        ("deny", [_GUARDED]),
    ]
    assert status == "failed"


async def test_workflow_scoped_deny_leaves_bash_alone_outside_w(
    tmp_path: Path,
) -> None:
    """The same W-scoped rule does not touch bash in another workflow,
    nor the same agent's bash outside any workflow."""
    _setup_workspace(
        tmp_path,
        rules=[
            {"what": _BASH, "pattern": "*", "from": _GUARDED, "effect": "deny"},
            _ALLOW_ALL,
        ],
    )
    runtime = await _boot(tmp_path)

    status, decisions = await _run_workflow(runtime, tmp_path, _SOLO)
    assert status == "succeeded"
    assert {d["effect"] for d in decisions if d["action"] == _BASH} == {"allow"}

    config = await runtime.build_resolved_config(_AGENT)
    session = await runtime.store.create_session(str(config.agent_id), name="direct")
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "invocation", "inputs": {}},
    )
    events = [e async for e in runtime.engine.execute_stream(config, ctx)]
    assert any(e.type == EventType.TOOL_RESULT for e in events)
    assert not any(e.type == EventType.TOOL_FAILED for e in events)

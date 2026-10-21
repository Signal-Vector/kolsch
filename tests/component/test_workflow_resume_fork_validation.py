"""bug-142 — resume and fork validate the reloaded workflow first.

`IterativeExecutor.run()` gates on `validate_workflow_or_raise`;
`resume()` and `start_from_fork()` used to reload the YAML and re-enter
the worklist unchecked. Now both gate before any side effect:

- a rejected resume leaves the run `waiting_for_input`, with no new
  JSONL line, and succeeds on retry once the workflow is fixed;
- a rejected fork leaves no run directory / record behind.

Executor-level tests (Tests 1-2) plus the CLI surface (Test 3).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from cli.main import main
from schemas import EventId
from schemas.entities import ForkedFrom
from workflows.builtins.noop import NoopRunner
from workflows.builtins.user_input import UserInputRunner
from workflows.errors import WorkflowNodeNotDefined, WorkflowValidationError
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

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
    "nodes": {
        "a": {"type": "kolsch/nodes/noop"},
        "b": {"type": "kolsch/nodes/noop"},
    },
    "graph": {"edges": [{"from": "a", "to": "b"}]},
}


def _invalid(workflow: dict[str, Any]) -> dict[str, Any]:
    """The same workflow with an edge to an undeclared node (one error)."""
    broken = copy.deepcopy(workflow)
    broken["graph"]["edges"].append(
        {"from": "a" if "a" in broken["nodes"] else "ask", "to": "ghost"}
    )
    return broken


def _registry() -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", NoopRunner())
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    return registry


def _executor(
    store: WorkflowRunStore, workflow: dict[str, Any], run: Any
) -> IterativeExecutor:
    return IterativeExecutor(
        workflow=Workflow.model_validate(workflow),
        run=run,
        store=store,
        registry=_registry(),
        catalog=AssetCatalog(),
    )


def _lines(store: WorkflowRunStore, run_id: Any) -> int:
    return len(store.directory(run_id).events_path.read_text().splitlines())


# ---------------------------------------------------------------------------
# Test 1 — invalid resume is rejected cleanly, then retryable
# ---------------------------------------------------------------------------


async def test_resume_rejects_invalid_reloaded_workflow_validation(
    tmp_path: Path,
) -> None:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x")
    status = await _executor(store, _PAUSING, run).run()
    assert status == WorkflowRunStatus.WAITING_FOR_INPUT
    before = _lines(store, run.run_id)
    meta_before = (store.directory(run.run_id).root / "meta.json").read_text()
    waiting_before = store.directory(run.run_id).waiting_at_path.read_bytes()

    paused = store.read(run.run_id)
    with pytest.raises(WorkflowNodeNotDefined):
        await _executor(store, _invalid(_PAUSING), paused).resume(
            response={"response": "yes"}
        )

    # Nothing happened: still paused, no new event, meta untouched.
    assert store.read(run.run_id).status == WorkflowRunStatus.WAITING_FOR_INPUT
    assert _lines(store, run.run_id) == before
    assert (store.directory(run.run_id).root / "meta.json").read_text() == meta_before
    assert store.directory(run.run_id).waiting_at_path.read_bytes() == waiting_before

    # Fixed YAML → the same run resumes to completion.
    status = await _executor(store, _PAUSING, store.read(run.run_id)).resume(
        response={"response": "yes"}
    )
    assert status == WorkflowRunStatus.SUCCEEDED
    kinds = [e["kind"] for e in store.read_events(run.run_id)]
    assert kinds.count("workflow.run.resumed") == 1


# ---------------------------------------------------------------------------
# Test 2 — invalid fork is rejected cleanly
# ---------------------------------------------------------------------------


async def test_fork_rejects_invalid_reloaded_workflow_validation(
    tmp_path: Path,
) -> None:
    store = WorkflowRunStore(tmp_path)
    parent = store.create(workflow_ref="demo/workflows/x")
    assert await _executor(store, _LINEAR, parent).run() == WorkflowRunStatus.SUCCEEDED
    fork_at = next(
        e["event_id"]
        for e in store.read_events(parent.run_id)
        if e["kind"] == "workflow.node.completed" and e.get("node_name") == "a"
    )
    forked = store.create(
        workflow_ref=parent.workflow_ref,
        trace_id=parent.trace_id,
        forked_from=ForkedFrom(run_id=parent.run_id, fork_event_id=EventId(fork_at)),
    )
    assert store.directory(forked.run_id).root.exists()

    with pytest.raises(WorkflowValidationError):
        await _executor(store, _invalid(_LINEAR), forked).start_from_fork(
            fork_event_id=fork_at
        )

    # No phantom run: directory gone, not listed; parent untouched.
    assert not store.directory(forked.run_id).root.exists()
    assert [r.run_id for r in store.list()] == [parent.run_id]
    assert [r.run_id for r in store.list_all_meta()] == [parent.run_id]


async def test_fork_validation_failure_with_several_errors_also_discards(
    tmp_path: Path,
) -> None:
    store = WorkflowRunStore(tmp_path)
    parent = store.create(workflow_ref="demo/workflows/x")
    assert await _executor(store, _LINEAR, parent).run() == WorkflowRunStatus.SUCCEEDED
    fork_at = next(
        e["event_id"]
        for e in store.read_events(parent.run_id)
        if e["kind"] == "workflow.run.started"
    )
    forked = store.create(
        workflow_ref=parent.workflow_ref,
        trace_id=parent.trace_id,
        forked_from=ForkedFrom(run_id=parent.run_id, fork_event_id=EventId(fork_at)),
    )
    broken = _invalid(_LINEAR)
    broken["graph"]["edges"].append({"from": "b", "to": "phantom"})
    with pytest.raises(ExceptionGroup):
        await _executor(store, broken, forked).start_from_fork(fork_event_id=fork_at)
    assert not store.directory(forked.run_id).root.exists()


def test_discard_unstarted_refuses_a_run_with_events(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x")
    store.directory(run.run_id).events_path.write_text('{"kind": "x"}\n')
    with pytest.raises(ValueError, match="has events"):
        store.discard_unstarted(run.run_id)
    assert store.directory(run.run_id).root.exists()


# ---------------------------------------------------------------------------
# Test 3 — CLI surface: error envelope, non-zero exit, no traceback
# ---------------------------------------------------------------------------

_PLATFORM = """\
secrets: {variant: dotenv, config: {path: .env}}
sandbox: {variant: native, config: {}}
authorization:
  variant: rule_based
  config:
    rules:
      - {what: "*", pattern: "*", effect: "allow"}
storage: {entity_backend: sqlite, entity_path: .platform/entities.db}
event_bus: {backend: in_process}
file_access: {allowed_paths: []}
defaults: {}
"""


def _workspace(root: Path, workflow: dict[str, Any]) -> Path:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(textwrap.dedent(_PLATFORM), encoding="utf-8")
    bundle = root / "bundles" / "demo"
    (bundle / "workflows").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        'name: demo\nversion: "0.1"\ndescription: t\n', encoding="utf-8"
    )
    _write_wf(root, workflow)
    return root


def _write_wf(root: Path, workflow: dict[str, Any]) -> None:
    from kolsch._yamlio import dump

    path = root / "bundles" / "demo" / "workflows" / "wf.yaml"
    path.write_text(dump(workflow), encoding="utf-8")


def _cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def _run_id(out: str) -> str:
    first = out.strip().splitlines()[0].split()
    return next(tok for tok in first if tok.startswith("wrk_"))


def test_cli_resume_with_invalid_workflow_validation_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path, _PAUSING)
    rc, out = _cli(
        ["--root", str(root), "workflows", "run", "demo/workflows/wf"], capsys
    )
    assert rc == 0
    run_id = _run_id(out)

    _write_wf(root, _invalid(_PAUSING))
    rc, out = _cli(
        ["--root", str(root), "workflows", "resume", run_id, "--input", "response=y"],
        capsys,
    )
    assert rc == 1
    assert "WorkflowNodeNotDefined" in out
    assert "Traceback" not in out
    meta = json.loads((root / "runs" / run_id / "meta.json").read_text())
    assert meta["status"] == "waiting_for_input"

    _write_wf(root, _PAUSING)
    rc, out = _cli(
        ["--root", str(root), "workflows", "resume", run_id, "--input", "response=y"],
        capsys,
    )
    assert rc == 0, out


@pytest.mark.parametrize("several", [False, True], ids=["one-error", "error-group"])
def test_cli_fork_with_invalid_workflow_validation_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, several: bool
) -> None:
    root = _workspace(tmp_path, _LINEAR)
    rc, out = _cli(
        ["--root", str(root), "workflows", "run", "demo/workflows/wf"], capsys
    )
    assert rc == 0, out
    parent_id = _run_id(out)
    events = [
        json.loads(line)
        for line in (root / "runs" / parent_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    fork_at = next(e["event_id"] for e in events if e["kind"] == "workflow.run.started")

    broken = _invalid(_LINEAR)
    if several:
        broken["graph"]["edges"].append({"from": "b", "to": "phantom"})
    _write_wf(root, broken)
    rc, out = _cli(
        ["--root", str(root), "workflows", "fork", parent_id, "--at", fork_at], capsys
    )
    assert rc == 1
    assert "WorkflowNodeNotDefined" in out
    assert "Traceback" not in out
    assert [p.name for p in (root / "runs").iterdir()] == [parent_id]


# ---------------------------------------------------------------------------
# `kolsch workflows run` with an invalid workflow — same gate, same rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("several", [False, True], ids=["one-error", "error-group"])
def test_cli_run_with_invalid_workflow_validation_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, several: bool
) -> None:
    broken = _invalid(_LINEAR)
    if several:
        broken["graph"]["edges"].append({"from": "b", "to": "phantom"})
    root = _workspace(tmp_path, broken)
    rc, out = _cli(
        ["--root", str(root), "workflows", "run", "demo/workflows/wf"], capsys
    )
    assert rc == 1
    assert "WorkflowNodeNotDefined" in out
    assert "Traceback" not in out


@pytest.mark.parametrize("several", [False, True], ids=["one-error", "error-group"])
def test_cli_run_follow_with_invalid_workflow_validation_exits_promptly(
    tmp_path: Path, *, several: bool
) -> None:
    """A real subprocess with a HARD timeout: a regression to the old
    behaviour (the JSONL tail waiting for a terminal event the rejected
    run never writes) fails here instead of hanging the suite."""
    broken = _invalid(_LINEAR)
    if several:
        broken["graph"]["edges"].append({"from": "b", "to": "phantom"})
    root = _workspace(tmp_path, broken)
    env = dict(os.environ)
    env.pop("KOLSCH_WORKSPACE", None)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli",
                "--root",
                str(root),
                "workflows",
                "run",
                "demo/workflows/wf",
                "--follow",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=_FOLLOW_HARD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"`workflows run --follow` on an invalid workflow did not exit "
            f"within {_FOLLOW_HARD_TIMEOUT}s (hang)"
        )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "WorkflowNodeNotDefined" in combined
    assert "Traceback" not in combined


_FOLLOW_HARD_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Review pass — rejected `run` leaves nothing; discard failures never
# mask the validation error; the shared group filter
# ---------------------------------------------------------------------------


async def test_rejected_run_leaves_no_run_validation(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x")
    with pytest.raises(WorkflowNodeNotDefined):
        await _executor(store, _invalid(_LINEAR), run).run()
    assert not store.directory(run.run_id).root.exists()
    assert store.list() == []
    assert list(store.list_all_meta()) == []


async def test_rejected_child_run_is_kept_validation(tmp_path: Path) -> None:
    """A sub-workflow child the parent already points at is not discarded.

    Driven through a real parent `kolsch/nodes/workflow` node: the
    child's run-start gate rejects it, the child dir is kept (the
    parent's `called_workflow` marker points at it), and
    `WorkflowSubRunner` records it FAILED rather than leaving it
    `pending` in `workflows list`.
    """
    from assets.catalog import AssetEntry
    from kolsch._yamlio import dump
    from workflows.builtins import WorkflowSubRunner

    child_path = tmp_path / "bundles" / "demo" / "workflows" / "x.yaml"
    child_path.parent.mkdir(parents=True)
    child_path.write_text(dump(_invalid(_LINEAR)), encoding="utf-8")
    catalog = AssetCatalog(
        assets={
            "demo/workflows/x": AssetEntry(
                ref="demo/workflows/x",
                asset_type="workflow",
                bundle="demo",
                path=str(child_path),
            )
        }
    )
    store = WorkflowRunStore(tmp_path)
    registry = _registry()
    registry.register(
        "kolsch/nodes/workflow",
        WorkflowSubRunner(
            catalog=catalog,
            store=store,
            executor_factory=IterativeExecutor,
            registry=registry,
        ),
    )
    parent = store.create(workflow_ref="demo/workflows/parent")
    executor = IterativeExecutor(
        workflow=Workflow.model_validate(
            {
                "nodes": {
                    "call": {"type": "kolsch/nodes/workflow", "ref": "demo/workflows/x"}
                },
                "graph": {"edges": []},
            }
        ),
        run=parent,
        store=store,
        registry=registry,
        catalog=catalog,
    )
    assert await executor.run() == WorkflowRunStatus.FAILED

    (child,) = [run for run in store.list() if run.parent is not None]
    assert child.parent is not None
    assert child.parent.workflow_run_id == parent.run_id
    assert store.directory(child.run_id).root.exists()
    assert list(store.read_events(child.run_id)) == []
    assert child.status == WorkflowRunStatus.FAILED


@pytest.mark.parametrize("entry", ["run", "fork"])
async def test_discard_failure_never_masks_the_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    store = WorkflowRunStore(tmp_path)

    def _boom(_run_id: Any) -> None:
        msg = "refusing to discard run: it has events"
        raise ValueError(msg)

    if entry == "run":
        run = store.create(workflow_ref="demo/workflows/x")
        monkeypatch.setattr(store, "discard_unstarted", _boom)
        with pytest.raises(WorkflowNodeNotDefined):
            await _executor(store, _invalid(_LINEAR), run).run()
        return

    parent = store.create(workflow_ref="demo/workflows/x")
    assert await _executor(store, _LINEAR, parent).run() == WorkflowRunStatus.SUCCEEDED
    fork_at = next(
        e["event_id"]
        for e in store.read_events(parent.run_id)
        if e["kind"] == "workflow.run.started"
    )
    forked = store.create(
        workflow_ref=parent.workflow_ref,
        trace_id=parent.trace_id,
        forked_from=ForkedFrom(run_id=parent.run_id, fork_event_id=EventId(fork_at)),
    )
    monkeypatch.setattr(store, "discard_unstarted", _boom)
    with pytest.raises(WorkflowNodeNotDefined):
        await _executor(store, _invalid(_LINEAR), forked).start_from_fork(
            fork_event_id=fork_at
        )


def test_workflow_errors_in_reraises_foreign_groups_validation() -> None:
    from workflows.validator import workflow_errors_in

    ok = ExceptionGroup("v", [_not_defined("a"), _not_defined("b")])
    assert [e.ref for e in workflow_errors_in(ok)] == ["a", "b"]
    mixed = ExceptionGroup("v", [_not_defined("a"), RuntimeError("boom")])
    with pytest.raises(ExceptionGroup) as exc_info:
        workflow_errors_in(mixed)
    assert exc_info.value is mixed


def _not_defined(ref: str) -> WorkflowNodeNotDefined:
    return WorkflowNodeNotDefined(
        file="demo/workflows/x", line=0, ref=ref, location=f"edges[0].to={ref}"
    )


def test_cli_rejected_run_leaves_nothing_listed_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path, _invalid(_LINEAR))
    rc, out = _cli(
        ["--root", str(root), "workflows", "run", "demo/workflows/wf"], capsys
    )
    assert rc == 1, out
    assert not (root / "runs").exists() or list((root / "runs").iterdir()) == []
    rc, out = _cli(
        ["--root", str(root), "workflows", "list", "--format", "json"], capsys
    )
    assert rc == 0, out
    assert "wrk_" not in out

"""bug-020 Test 5 — `build_workflow_executor` accepts observer + cancel kwargs.

Exercises AC #8 (`event_observer` + `cancel_token` kwargs thread
through to `IterativeExecutor`) and the backward-compat default
(`None` — the headless `kolsch workflows run` CLI stays byte-
identical).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from engine.context import CancelToken
from platform_runtime import bootstrap_runtime
from workflows.parser import load_workflow

if TYPE_CHECKING:
    from pathlib import Path

_HELLO_WORKFLOW_YAML = """
nodes:
  greet:
    type: kolsch/nodes/bash
    command: "echo Hello"
graph:
  edges: []
"""


async def _boot_runtime(tmp_path: Path) -> Any:
    """Return a `PlatformRuntime` rooted at ``tmp_path``."""
    return await bootstrap_runtime(root=tmp_path)


def _write_workflow(tmp_path: Path) -> Path:
    """Write a minimal workflow to tmp; return its path."""
    path = tmp_path / "hello.yaml"
    path.write_text(_HELLO_WORKFLOW_YAML, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_build_accepts_event_observer_kwarg(tmp_path: Path) -> None:
    """`event_observer` kwarg lands on the resulting `IterativeExecutor`."""
    from cli._workflow_runtime import build_workflow_executor
    from workflows.run_store import WorkflowRunStore

    runtime = await _boot_runtime(tmp_path)
    path = _write_workflow(tmp_path)
    workflow = load_workflow(path)
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="test/hello", inputs={})

    def _observer(_event: Any) -> None:
        pass

    executor = build_workflow_executor(
        root=tmp_path,
        runtime=runtime,
        workflow=workflow,
        run=run,
        store=store,
        event_observer=_observer,
    )

    assert executor._event_observer is _observer


@pytest.mark.asyncio
async def test_build_accepts_cancel_token_kwarg(tmp_path: Path) -> None:
    """`cancel_token` kwarg lands on the resulting `IterativeExecutor`."""
    from cli._workflow_runtime import build_workflow_executor
    from workflows.run_store import WorkflowRunStore

    runtime = await _boot_runtime(tmp_path)
    path = _write_workflow(tmp_path)
    workflow = load_workflow(path)
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="test/hello", inputs={})

    token = CancelToken()

    executor = build_workflow_executor(
        root=tmp_path,
        runtime=runtime,
        workflow=workflow,
        run=run,
        store=store,
        cancel_token=token,
    )

    assert executor._cancel_token is token


@pytest.mark.asyncio
async def test_build_default_kwargs_are_none(tmp_path: Path) -> None:
    """Both kwargs default to `None` — backward-compat for the CLI call site."""
    from cli._workflow_runtime import build_workflow_executor
    from workflows.run_store import WorkflowRunStore

    runtime = await _boot_runtime(tmp_path)
    path = _write_workflow(tmp_path)
    workflow = load_workflow(path)
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="test/hello", inputs={})

    executor = build_workflow_executor(
        root=tmp_path,
        runtime=runtime,
        workflow=workflow,
        run=run,
        store=store,
    )

    assert executor._event_observer is None
    assert executor._cancel_token is None

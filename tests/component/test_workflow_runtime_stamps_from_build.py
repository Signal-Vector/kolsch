"""Integration test — `_stamp_kolsch` stamps every one of the 5 workflow builtins.

qol-051 B1 — closes the coverage gap Reviewer flagged in bug-050 AC #2:
the unit tests at `tests/unit/test_workflow_runtime_home_bundle.py`
exercise the ``home_bundle`` stamp on each ``make_*`` factory in
isolation, but never call ``build_workflow_executor`` — so a future
refactor that dropped a line like ``_stamp_kolsch(bash_defn)`` would
slip past the unit suite. This end-to-end test builds the same
``IterativeExecutor`` production ships, pulls each of the 5 file-IO /
shell / browse runners off the executor's ``_registry``, and asserts
its ``ToolDefinition.action_name`` is the canonical 3-seg
``kolsch/tools/<name>`` — the string users' ``platform.yaml`` rules
reference at authz-check time.

Comment out any one of the five ``_stamp_kolsch(...)`` calls at
``src/cli/_workflow_runtime.py:129-141`` and this test fails on the
affected tool (``action_name`` drops back to the legacy 1-seg fallback
``tool.<name>``) — the proof captured in the commit body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cli._workflow_runtime import build_workflow_executor
from platform_runtime import bootstrap_runtime
from workflows.parser import load_workflow
from workflows.run_store import WorkflowRunStore

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


@pytest.mark.asyncio
async def test_build_workflow_executor_stamps_all_5_builtin_action_names(
    tmp_path: Path,
) -> None:
    """End-to-end guard for bug-050 AC #2: each of the 5 workflow-runtime
    builtins carries the canonical 3-seg ``action_name`` after
    ``build_workflow_executor`` finishes wiring the registry.

    The mapping tool_name -> registered runner slot is fixed at
    ``kolsch/nodes/<tool_name>`` for all 5 tools; each runner stores
    its ``ToolDefinition`` in ``self._defn`` (see
    ``src/workflows/builtins/{bash,read_file,write_file,list_files,browse_url}.py``).
    We reach in via the executor's ``_registry._runners`` map — the
    same map ``RunnerRegistry.get`` reads at dispatch time — so if a
    stamp gets dropped in the runtime wiring, the assertion below
    fires exactly where the miss-in-production would.
    """
    runtime = await bootstrap_runtime(root=tmp_path)
    wf_path = tmp_path / "hello.yaml"
    wf_path.write_text(_HELLO_WORKFLOW_YAML, encoding="utf-8")
    workflow = load_workflow(wf_path)
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="test/hello", inputs={})

    executor = build_workflow_executor(
        root=tmp_path,
        runtime=runtime,
        workflow=workflow,
        run=run,
        store=store,
    )

    for tool_name in ("bash", "read_file", "write_file", "list_files", "browse_url"):
        runner = executor._registry._runners[f"kolsch/nodes/{tool_name}"]
        defn = runner._defn
        assert defn.home_bundle == "kolsch", (
            f"{tool_name!r} runner's tool defn missing home_bundle stamp — "
            f"see src/cli/_workflow_runtime.py::_stamp_kolsch and bug-050 AC #2."
        )
        assert defn.action_name == f"kolsch/tools/{tool_name}", (
            f"{tool_name!r} runner's tool defn action_name resolved to "
            f"{defn.action_name!r}; expected canonical 3-seg "
            f"'kolsch/tools/{tool_name}' so users' `what: "
            f"kolsch/tools/{tool_name}` rules match at dispatch. "
            "See bug-050 AC #2 / bug-workflow-runtime-tool-home-bundle-not-stamped."
        )

"""bug-024 Test 4 (AC #11) — startup guardrail WARN log.

`ChatApp.on_mount` fires a `_LOGGER.warning(...)` when the service
registry has `WorkflowsImpl` live BUT the mounted agent's registered
tool set contains no tool declaring `requires=["workflows"]`. That
mismatch is exactly the composition failure bug-024 exists to catch:
`WorkflowsImpl` is running (bug-020's plumbing) but the model can't
see any workflow tool, so it falls back to `bash` when prose asks to
run a workflow.

The unit tests here drive `_warn_if_workflow_tools_missing` directly
with hand-built subsystem/workflows stubs — the ChatApp.on_mount
call site is exercised end-to-end by the flagship
`test_model_picks_run_workflow_for_prose.py` (AC #10).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from cli.chat.app import (
    _WORKFLOW_TOOLS_MISSING_WARNING,
    _warn_if_workflow_tools_missing,
)
from tools.decorator import ToolDefinition


class _StubArgs(BaseModel):
    """No-arg schema for the stub tools below."""


async def _noop_fn(*_: Any, **__: Any) -> Any:  # pragma: no cover — unused
    return None


class _StubRegistry:
    """Mimics `ToolsRegistry.get_tool(agent_id, name)` shape."""

    def __init__(self, by_name: dict[str, ToolDefinition]) -> None:
        self._by_name = by_name

    def get_tool(self, _agent_id: Any, name: str) -> ToolDefinition | None:
        return self._by_name.get(name)


class _StubToolsSubsystem:
    """Enough surface for `_warn_if_workflow_tools_missing`."""

    def __init__(self, defns: list[ToolDefinition]) -> None:
        self._by_name = {d.name: d for d in defns}
        self._registry = _StubRegistry(self._by_name)

    def list_registered_tools(self, _agent_id: Any) -> list[Any]:
        return [d.to_info() for d in self._by_name.values()]


def _defn(name: str, *, requires: list[str] | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Stub {name} tool.",
        args_schema=_StubArgs,
        func=_noop_fn,
        requires=list(requires or []),
    )


def test_warns_when_declared_refs_have_no_workflow_tool(caplog: Any) -> None:
    """AC #11: guardrail fires when the config's `declared:` list is missing.

    This is the primary signal used at `on_mount` (BEFORE the engine
    registers tools per-exchange). Bug-024's canonical repro: the
    shipped helper.yaml declared `{read_file, write_file, list_files,
    bash, fetch_website}` and the model then picked bash for prose
    like "run the greet workflow".
    """
    workflows_impl = object()  # non-None → guardrail engages
    logger = logging.getLogger("bug024.warns_declared")
    logger.setLevel(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        _warn_if_workflow_tools_missing(
            logger=logger,
            tools_subsystem=None,
            agent_id="ag_helper",
            workflows_impl=workflows_impl,
            declared_refs=[
                "kolsch/tools/read_file",
                "kolsch/tools/write_file",
                "kolsch/tools/list_files",
                "kolsch/tools/bash",
                "kolsch/tools/fetch_website",
            ],
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        f"expected exactly one WARN; got {len(warnings)}: {warnings!r}"
    )
    assert warnings[0].getMessage() == _WORKFLOW_TOOLS_MISSING_WARNING
    # AC #11: message is actionable — names the file + the ref.
    assert "helper.yaml" in warnings[0].getMessage()
    assert "kolsch/tools/run_workflow" in warnings[0].getMessage()


def test_no_warning_when_declared_refs_include_run_workflow(caplog: Any) -> None:
    """The guardrail stays silent when the config declares run_workflow."""
    workflows_impl = object()
    logger = logging.getLogger("bug024.silent_declared")
    logger.setLevel(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        _warn_if_workflow_tools_missing(
            logger=logger,
            tools_subsystem=None,
            agent_id="ag_helper",
            workflows_impl=workflows_impl,
            declared_refs=[
                "kolsch/tools/bash",
                "kolsch/tools/run_workflow",
            ],
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [], (
        f"guardrail fired even though run_workflow was declared: {warnings!r}"
    )


def test_warns_when_registered_tools_have_no_workflow_requirement(
    caplog: Any,
) -> None:
    """Fallback signal: runtime registry check when config isn't passed."""
    tools_sub = _StubToolsSubsystem(
        [
            _defn("bash"),
            _defn("read_file"),
        ]
    )
    workflows_impl = object()

    logger = logging.getLogger("bug024.warns_registry")
    logger.setLevel(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        _warn_if_workflow_tools_missing(
            logger=logger,
            tools_subsystem=tools_sub,
            agent_id="ag_helper",
            workflows_impl=workflows_impl,
            declared_refs=None,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == _WORKFLOW_TOOLS_MISSING_WARNING


def test_no_warning_when_run_workflow_registered_at_runtime(
    caplog: Any,
) -> None:
    """Fallback silent path: a real `requires=["workflows"]` tool exists."""
    tools_sub = _StubToolsSubsystem(
        [
            _defn("bash"),
            _defn("run_workflow", requires=["cards", "workflows"]),
        ]
    )
    workflows_impl = object()

    logger = logging.getLogger("bug024.silent_registry")
    logger.setLevel(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        _warn_if_workflow_tools_missing(
            logger=logger,
            tools_subsystem=tools_sub,
            agent_id="ag_helper",
            workflows_impl=workflows_impl,
            declared_refs=None,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_no_warning_when_workflows_impl_absent(caplog: Any) -> None:
    """Sanity: with `workflows_impl=None`, the guardrail is inert.

    Headless test fixtures often construct a ChatApp without a live
    `WorkflowsImpl` — the warning would be a false alarm there.
    """
    logger = logging.getLogger("bug024.no_impl")
    logger.setLevel(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        _warn_if_workflow_tools_missing(
            logger=logger,
            tools_subsystem=None,
            agent_id="ag_helper",
            workflows_impl=None,
            declared_refs=["kolsch/tools/bash"],
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []

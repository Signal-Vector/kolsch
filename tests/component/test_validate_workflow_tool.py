"""Phase 3.11 Step 0 — Phase Test 7: ``validate_workflow`` round-trip.

Confirms the bundle tool at
``src/cli/_starter_bundle/kolsch/tools/validate_workflow.py`` correctly
serialises every :class:`workflows.errors.WorkflowValidationError`
subclass surfaced via the test scenarios into the structured
:class:`ValidationFinding` shape (per spec §17.3): YAML parse errors,
validator errors with ``identifying_names``, missing catalog refs,
arg-shape errors, and warnings round-trip.

The tool runs UNMOCKED against the real engine surface — the
fixture sets up a workspace, copies the starter bundle, and writes
ad-hoc YAMLs into the workspace for each scenario.

Pattern mirrored from ``tests/component/tools/test_read_file_line_based.py``:
discover the tool from the starter-bundle path, build a
:class:`ToolContext` by hand, invoke as a coroutine.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from files import PathPolicy
from schemas import AgentId, SessionId, ThreadId
from tools import ToolContext
from tools.discovery import discover_tools_in_file

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_REPO_ROOT = Path(__file__).resolve().parents[2]
_STARTER_BUNDLE = (
    _REPO_ROOT / "src" / "cli" / "_starter_bundle" / "kolsch"
)
_TOOL_PATH = _STARTER_BUNDLE / "tools" / "validate_workflow.py"


def _tool() -> Callable[..., Awaitable[object]]:
    for fn in discover_tools_in_file(_TOOL_PATH):
        defn = fn._tool_definition  # type: ignore[attr-defined]
        if defn.name == "validate_workflow":
            return fn
    msg = f"could not find validate_workflow tool in {_TOOL_PATH}"
    raise RuntimeError(msg)


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.ag_test",
        secrets={},
        workspace_root=workspace,
        path_policy=PathPolicy(allowed_paths=["."], denied_paths=[]),
        tool_call_id="tc_validate_unit",
    )


def _make_workspace(tmp_path: Path) -> Path:
    """Lay down a workspace with the starter bundle copied in.

    Mirrors ``tests/component/test_workflow_branching_e2e.py``'s
    layout — the bundle ships with a ``bundle.yaml`` already, so a
    direct ``copytree`` is enough for catalog discovery to see it.
    """
    (tmp_path / "bundles").mkdir()
    shutil.copytree(_STARTER_BUNDLE, tmp_path / "bundles" / "kolsch")
    return tmp_path


# ---------------------------------------------------------------------------
# 1 — Valid workflow via catalog ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_valid_workflow_via_ref(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    result = await fn(Args(workflow_ref="kolsch/workflows/hello"), _ctx(workspace))

    assert result["is_valid"] is True
    assert result["errors"] == []
    assert result["warnings"] == []


# ---------------------------------------------------------------------------
# 2 — Valid workflow via direct yaml_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_valid_workflow_via_path(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    path = workspace / "bundles" / "kolsch" / "workflows" / "hello.yaml"
    result = await fn(Args(yaml_path=str(path)), _ctx(workspace))

    assert result["is_valid"] is True
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# 3 — YAML parse error surfaces as a single finding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_yaml_parse_error_surfaces_finding(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    broken = workspace / "bundles" / "kolsch" / "workflows" / "broken.yaml"
    # Unterminated mapping → ruamel.yaml ScannerError.
    broken.write_text("nodes:\n  ready:\n    type: kolsch/nodes/noop\n  bad: [\n", encoding="utf-8")

    result = await fn(Args(yaml_path=str(broken)), _ctx(workspace))

    assert result["is_valid"] is False
    assert len(result["errors"]) == 1
    finding = result["errors"][0]
    # The parser wraps YAML errors into WorkflowValidationError.
    assert finding["error_class"] == "WorkflowValidationError"
    assert finding["caller_message"]
    assert "broken.yaml" in finding["file"]


# ---------------------------------------------------------------------------
# 4 — Validator error: duplicate node names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_duplicate_node_finding(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    # Two outbound edges into the same target with the same bind key —
    # the classic WorkflowBindCollision pattern (master spec §7.1).
    dup = workspace / "bundles" / "kolsch" / "workflows" / "collide.yaml"
    dup.write_text(
        """\
description: bind collision scenario
nodes:
  alpha:
    type: kolsch/nodes/noop
  beta:
    type: kolsch/nodes/noop
  sink:
    type: kolsch/nodes/echo

graph:
  edges:
    - from: alpha
      to: sink
      binds:
        message: "from-alpha"
    - from: beta
      to: sink
      binds:
        message: "from-beta"
""",
        encoding="utf-8",
    )

    result = await fn(Args(yaml_path=str(dup)), _ctx(workspace))

    assert result["is_valid"] is False
    classes = {err["error_class"] for err in result["errors"]}
    assert "WorkflowBindCollision" in classes
    collision = next(
        e for e in result["errors"] if e["error_class"] == "WorkflowBindCollision"
    )
    # The exception's identifying_names_keys is ("node", "bind") —
    # both must round-trip.
    assert collision["identifying_names"].get("node") == "sink"
    assert collision["identifying_names"].get("bind") == "message"
    assert "collide.yaml" in collision["file"]


# ---------------------------------------------------------------------------
# 5 — Missing catalog ref → WorkflowNotFound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_missing_catalog_ref(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    result = await fn(
        Args(workflow_ref="kolsch/workflows/does-not-exist"),
        _ctx(workspace),
    )

    assert result["is_valid"] is False
    assert len(result["errors"]) == 1
    finding = result["errors"][0]
    assert finding["error_class"] == "WorkflowNotFound"
    assert finding["identifying_names"] == {
        "ref": "kolsch/workflows/does-not-exist"
    }
    assert finding["file"] == "<catalog>"


# ---------------------------------------------------------------------------
# 6 — Both args set → InvalidToolArgs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_both_args_set(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    result = await fn(
        Args(workflow_ref="kolsch/workflows/hello", yaml_path="/dev/null"),
        _ctx(workspace),
    )

    assert result["is_valid"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["error_class"] == "InvalidToolArgs"


# ---------------------------------------------------------------------------
# 7 — Neither arg set → InvalidToolArgs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_neither_arg_set(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    result = await fn(Args(), _ctx(workspace))

    assert result["is_valid"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["error_class"] == "InvalidToolArgs"


# ---------------------------------------------------------------------------
# 8 — Warnings round-trip (per-node timeout > workflow timeout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_warnings_roundtrip(tmp_path: Path) -> None:
    """A workflow whose per-node timeout exceeds the workflow timeout
    triggers :class:`WorkflowTimeoutShadowsNode` (a warning, not an
    error). The tool's response must place it under ``warnings`` and
    leave ``is_valid`` True."""
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    yaml_path = workspace / "bundles" / "kolsch" / "workflows" / "shadow.yaml"
    yaml_path.write_text(
        """\
description: timeout-shadow scenario
timeout_seconds: 5
nodes:
  slow:
    type: kolsch/nodes/noop
    timeout_seconds: 30

graph:
  edges: []
""",
        encoding="utf-8",
    )

    result = await fn(Args(yaml_path=str(yaml_path)), _ctx(workspace))

    assert result["is_valid"] is True
    classes = {w["error_class"] for w in result["warnings"]}
    assert "WorkflowTimeoutShadowsNode" in classes
    warn = next(
        w
        for w in result["warnings"]
        if w["error_class"] == "WorkflowTimeoutShadowsNode"
    )
    assert warn["identifying_names"].get("node") == "slow"

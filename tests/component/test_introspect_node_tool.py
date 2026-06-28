"""Phase 3.11 Step 0 — Phase Test 8: ``introspect_node`` round-trip.

Confirms the bundle tool at
``src/cli/_starter_bundle/kolsch/tools/introspect_node.py`` correctly
identifies valid + invalid authored ``@node`` modules per spec §17.4.

Six scenarios:

1. Valid ``@node`` (starter ``echo.py``) → ``signature_ok: True``,
   label ``"Echo"``, both schemas populated.
2. Missing ``@node`` decorator on ``run`` → ``signature_ok: False``,
   error names the violation.
3. ``run`` accepts no parameters → ``signature_ok: False``.
4. Param 1 not a ``BaseModel`` subclass → ``signature_ok: False``,
   error mentions the Inputs requirement.
5. Non-existent file → ``signature_ok: False``, error reads
   like a ``FileNotFoundError``.
6. Syntax error in the module → ``signature_ok: False``, error
   reads like a ``SyntaxError``.

The fixture writes ad-hoc ``@node`` modules under a tmp workspace
for each scenario; the starter ``echo.py`` is copied in for the
positive case.
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
_TOOL_PATH = _STARTER_BUNDLE / "tools" / "introspect_node.py"
_ECHO_PATH = _STARTER_BUNDLE / "nodes" / "echo.py"


def _tool() -> Callable[..., Awaitable[object]]:
    for fn in discover_tools_in_file(_TOOL_PATH):
        defn = fn._tool_definition  # type: ignore[attr-defined]
        if defn.name == "introspect_node":
            return fn
    msg = f"could not find introspect_node tool in {_TOOL_PATH}"
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
        tool_call_id="tc_introspect_unit",
    )


def _make_workspace(tmp_path: Path) -> Path:
    (tmp_path / "bundles" / "kolsch" / "nodes").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# 1 — Valid @node module (starter ``echo.py``)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_valid_echo_node(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    dest = workspace / "bundles" / "kolsch" / "nodes" / "echo.py"
    shutil.copy(_ECHO_PATH, dest)

    result = await fn(Args(py_path=str(dest)), _ctx(workspace))

    assert result["signature_ok"] is True
    assert result["label"] == "Echo"
    assert result["errors"] == []
    inputs = result["inputs_schema"]
    outputs = result["outputs_schema"]
    assert inputs is not None
    assert outputs is not None
    assert "message" in inputs.get("properties", {})
    assert "message" in outputs.get("properties", {})


# ---------------------------------------------------------------------------
# 2 — Missing @node decorator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_missing_decorator(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    target = workspace / "bundles" / "kolsch" / "nodes" / "bare.py"
    target.write_text(
        "from __future__ import annotations\n"
        "\n"
        "from pydantic import BaseModel\n"
        "\n"
        "from workflows import NodeContext\n"
        "\n"
        "\n"
        "class Inputs(BaseModel):\n"
        "    x: int\n"
        "\n"
        "\n"
        "class Outputs(BaseModel):\n"
        "    y: int\n"
        "\n"
        "\n"
        "async def run(inputs: Inputs, ctx: NodeContext) -> Outputs:\n"
        "    _ = ctx\n"
        "    return Outputs(y=inputs.x)\n",
        encoding="utf-8",
    )

    result = await fn(Args(py_path=str(target)), _ctx(workspace))

    assert result["signature_ok"] is False
    assert result["label"] is None
    assert any("MissingDecorator" in err for err in result["errors"])


# ---------------------------------------------------------------------------
# 3 — Wrong signature: ``run`` accepts no parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_wrong_signature_no_params(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    target = workspace / "bundles" / "kolsch" / "nodes" / "no_params.py"
    target.write_text(
        "from __future__ import annotations\n"
        "\n"
        "from pydantic import BaseModel\n"
        "\n"
        "from workflows import node\n"
        "\n"
        "\n"
        "class Outputs(BaseModel):\n"
        "    msg: str\n"
        "\n"
        "\n"
        '@node(label="NoParams")\n'
        "async def run() -> Outputs:\n"
        '    return Outputs(msg="hi")\n',
        encoding="utf-8",
    )

    result = await fn(Args(py_path=str(target)), _ctx(workspace))

    assert result["signature_ok"] is False
    assert result["label"] == "NoParams"
    assert any("InvalidSignature" in err for err in result["errors"])
    assert any("parameters, got 0" in err for err in result["errors"])


# ---------------------------------------------------------------------------
# 4 — Wrong param-1 annotation (not a BaseModel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_wrong_param1_annotation(tmp_path: Path) -> None:
    """Param 1 must be a ``BaseModel`` subclass. A plain ``dict``
    annotation fails the §5.3 check — the error message must name the
    Inputs requirement so the meta-agent can fix it."""
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    target = workspace / "bundles" / "kolsch" / "nodes" / "bad_inputs.py"
    target.write_text(
        "from __future__ import annotations\n"
        "\n"
        "from pydantic import BaseModel\n"
        "\n"
        "from workflows import NodeContext, node\n"
        "\n"
        "\n"
        "class Outputs(BaseModel):\n"
        "    msg: str\n"
        "\n"
        "\n"
        '@node(label="BadInputs")\n'
        "async def run(inputs: dict, ctx: NodeContext) -> Outputs:\n"
        "    _ = ctx\n"
        "    return Outputs(msg=str(inputs))\n",
        encoding="utf-8",
    )

    result = await fn(Args(py_path=str(target)), _ctx(workspace))

    assert result["signature_ok"] is False
    assert result["label"] == "BadInputs"
    joined = "\n".join(result["errors"])
    assert "InvalidSignature" in joined
    assert "BaseModel" in joined


# ---------------------------------------------------------------------------
# 5 — Non-existent file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_missing_file(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    # Path is inside workspace (so the path policy passes) but the file
    # itself doesn't exist.
    target = workspace / "bundles" / "kolsch" / "nodes" / "not_here.py"
    result = await fn(Args(py_path=str(target)), _ctx(workspace))

    assert result["signature_ok"] is False
    joined = "\n".join(result["errors"])
    assert "FileNotFoundError" in joined


# ---------------------------------------------------------------------------
# 6 — Syntax error in module
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_syntax_error(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    fn = _tool()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    target = workspace / "bundles" / "kolsch" / "nodes" / "syntax.py"
    target.write_text(
        "def run(inputs, ctx ->\n    return None\n", encoding="utf-8"
    )

    result = await fn(Args(py_path=str(target)), _ctx(workspace))

    assert result["signature_ok"] is False
    joined = "\n".join(result["errors"])
    assert "SyntaxError" in joined

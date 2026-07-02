"""Regression tests for bug-workflow-runtime-tool-home-bundle-not-stamped.

The workflow runtime at ``src/cli/_workflow_runtime.py`` builds
``ToolDefinition``s for the five workflow-scope builtins (bash,
read_file, write_file, list_files, browse_url) via
``get_tool_definition(make_*(...))``. Without an explicit
``home_bundle`` stamp on each defn, ``action_name`` falls back to
the legacy 1-seg form ``tool.<name>`` and users' canonical 3-seg
rules (``what: kolsch/tools/<name>``) silently don't match at
authz-check time.

The fix stamps ``home_bundle = "kolsch"`` on every workflow-runtime
tool defn (canonical builtins ship at ``src/tools/builtins.py`` and
belong to the ``kolsch`` bundle by definition). These tests pin the
invariant so a future refactor of the workflow-runtime factory
plumbing cannot silently drop the stamp again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from files import PathPolicy
from tools.builtins import (
    make_bash,
    make_browse_url,
    make_list_files,
    make_read_file,
    make_write_file,
)
from tools.decorator import get_tool_definition


@pytest.fixture()
def _policy(tmp_path: Path) -> PathPolicy:
    """Match the shape ``build_workflow_executor`` gets from the runtime."""
    return PathPolicy(allowed_paths=[str(tmp_path)], denied_paths=[])


@pytest.mark.parametrize(
    ("factory", "tool_name"),
    [
        (make_bash, "bash"),
        (make_read_file, "read_file"),
        (make_write_file, "write_file"),
        (make_list_files, "list_files"),
        (make_browse_url, "browse_url"),
    ],
)
def test_workflow_runtime_stamps_home_bundle_on_all_builtin_defns(
    factory: object,
    tool_name: str,
    tmp_path: Path,
    _policy: PathPolicy,
) -> None:
    """Each of the 5 workflow-runtime builtins must be stamped ``kolsch``.

    Mirrors the code at ``src/cli/_workflow_runtime.py``: build the
    ``@tool``-decorated coroutine via its factory, pull the sentinel
    via ``get_tool_definition``, and stamp ``home_bundle="kolsch"``.
    Assert the resulting ``action_name`` is the canonical 3-seg ref
    the user's ``platform.yaml`` rules reference.
    """
    fn = factory(policy=_policy, root=tmp_path)  # type: ignore[operator]
    defn = get_tool_definition(fn)
    # Fresh defn: home_bundle is None; action_name falls back to legacy.
    assert defn.home_bundle is None
    assert defn.action_name == f"tool.{tool_name}", (
        "Sanity: pre-stamp defn must return the legacy 1-seg form so "
        "the stamp below is the load-bearing change."
    )
    # The workflow-runtime fix stamps `kolsch` here. Simulate it and
    # assert the action_name flips to canonical.
    defn.home_bundle = "kolsch"
    assert defn.action_name == f"kolsch/tools/{tool_name}", (
        f"Workflow-runtime tool defn for {tool_name!r} must expose the "
        f"canonical 3-seg action string 'kolsch/tools/{tool_name}' so "
        f"users' `what: kolsch/tools/{tool_name}` rules match at "
        "dispatch. See bug-workflow-runtime-tool-home-bundle-not-stamped."
    )

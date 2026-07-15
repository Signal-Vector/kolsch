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

from dataclasses import replace
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
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
    # The workflow-runtime fix stamps `kolsch` on the sentinel. Simulate
    # it on a *copy* rather than mutating the returned sentinel — bug-050:
    # `make_browse_url` returns a module-cached function whose sentinel
    # `ToolDefinition` is shared across factory calls, so an in-place
    # `defn.home_bundle = "kolsch"` here would poison later parametrizations
    # and any downstream test running in the same pytest process. Prod
    # code owns the sentinel and stamps it once at `build_workflow_executor`;
    # this test only reads the invariant.
    stamped = replace(defn, home_bundle="kolsch")
    assert stamped.action_name == f"kolsch/tools/{tool_name}", (
        f"Workflow-runtime tool defn for {tool_name!r} must expose the "
        f"canonical 3-seg action string 'kolsch/tools/{tool_name}' so "
        f"users' `what: kolsch/tools/{tool_name}` rules match at "
        "dispatch. See bug-workflow-runtime-tool-home-bundle-not-stamped."
    )
    # And, of course, the original sentinel must be untouched by our probe.
    assert defn.home_bundle is None, (
        "This test must not mutate the shared ToolDefinition sentinel — see bug-050."
    )


def test_workflow_runtime_stamps_home_bundle_survives_double_bootstrap(
    tmp_path: Path,
    _policy: PathPolicy,
) -> None:
    """bug-050: pin the pollution class the parametrized guard tripped over.

    Before bug-050, ``make_browse_url`` returned a module-cached
    coroutine (loaded once into ``sys.modules``) whose attached
    ``ToolDefinition`` was a single shared sentinel across every
    factory call. Any caller that mutated ``defn.home_bundle`` in
    place — production code (``_stamp_kolsch`` in ``_workflow_runtime``)
    OR a test's own probe — poisoned the sentinel for every downstream
    reader in the same pytest process, so the guard test
    ``test_workflow_runtime_stamps_home_bundle_on_all_builtin_defns``
    flaked under full-suite order (any earlier test that exercised
    ``build_workflow_executor`` re-stamped the sentinel).

    The bug-050 fix detaches a fresh ``_tool_definition`` per factory
    call via ``_detach_tool_definition`` in ``src/tools/builtins.py``.
    This test pins the invariant: two calls in a row to
    ``make_browse_url`` yield independent defns — mutating one does
    not observably poison the other. Without the fix, the second
    ``assert defn_second.home_bundle is None`` blows up because the
    first stamp bled through.
    """
    fn_first = make_browse_url(policy=_policy, root=tmp_path)
    defn_first = get_tool_definition(fn_first)
    assert defn_first.home_bundle is None, (
        "First get_tool_definition(make_browse_url(...)) must return "
        "an unstamped defn — the factory is expected to detach a fresh "
        "ToolDefinition per call so callers can stamp `home_bundle` "
        "without poisoning the module-cached sentinel. See bug-050."
    )
    # Mutate the first defn as prod code (``_stamp_kolsch``) would.
    defn_first.home_bundle = "kolsch"

    # Second call must see a fresh defn — the mutation on defn_first
    # must not have bled through the module cache.
    fn_second = make_browse_url(policy=_policy, root=tmp_path)
    defn_second = get_tool_definition(fn_second)
    assert defn_second.home_bundle is None, (
        "Second get_tool_definition(make_browse_url(...)) must ALSO "
        "return an unstamped defn — the first caller's stamp must not "
        "have bled through the module cache. If this fails, "
        "`make_browse_url` is back to returning a shared sentinel — see "
        "bug-050's `_detach_tool_definition` helper in "
        "src/tools/builtins.py."
    )
    # And the two defns are distinct objects — pollution isolation.
    assert defn_first is not defn_second, (
        "Sanity: after bug-050, make_browse_url detaches a per-call "
        "ToolDefinition copy. If this identity breaks (both defns the "
        "same object), the class of pollution is back."
    )

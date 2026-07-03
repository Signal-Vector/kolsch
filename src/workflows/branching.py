"""Branch value extraction for the executor's edge dispatcher.

Maps a completed node's captured output to the boolean the executor
uses to filter outgoing edges by their `branch:` field. Single
function — `compute_branch_value` — typed so the worklist dispatcher
reads it once and gates on the return value.

Returns `None` for non-branchable nodes; the executor treats `None` as
"fire every outgoing edge unconditionally" (the validator already
guarantees these nodes have no `branch:` edges, so the runtime check
is defensive).
"""

from __future__ import annotations

from typing import Any

__all__ = ["compute_branch_value"]


_BASH_TYPE_REF = "kolsch/nodes/bash"
_BRANCH_TYPE_REF = "kolsch/nodes/branch"


def compute_branch_value(
    node_def: Any,
    captured: dict[str, Any],
) -> bool | None:
    """Extract the branchable boolean for edge dispatch.

    Mapping by source node type:

    - `kolsch/nodes/bash`: `True` iff `captured["exit_code"] == 0`.
    - `kolsch/nodes/branch`: `captured["value"]` — the
      [`BranchRunner`][workflows.builtins.branch.BranchRunner] coerces
      its `condition:` to `bool` before storing, so the guard below is
      for completeness rather than defence.
    - Authored Python `@node` returning `bool` or
      `tuple[bool, BaseModel]`: the bool lives at `captured["value"]`
      (the
      [`CatalogNodeRunner`][workflows.builtins.catalog_node_runner.CatalogNodeRunner]
      stores it under the `value` key for both return shapes).
    - Everything else (`noop`, `agent`, future leaves): `None`.

    The `isinstance(value, bool)` check is intentional — Python's
    `bool` is a subclass of `int`, but the executor's edge dispatch
    must NOT route on truthy/falsy ints. The validator has already
    rejected `branch:` edges from non-branchable sources at parse
    time; this runtime check defends against drift in the
    captured-output shape (e.g. a future authored node that returns an
    `int` rather than a `bool`).

    Args:
        node_def: The completed node's definition.
        captured: The captured output dict for that node.

    Returns:
        The branchable boolean, or `None` for non-branchable nodes.
    """
    type_ref = getattr(node_def, "type", None)
    if type_ref == _BASH_TYPE_REF:
        return bool(captured.get("exit_code") == 0)
    if type_ref == _BRANCH_TYPE_REF:
        # Symmetric with the authored-Python path below: both
        # branchable source types capture their bool under `value`.
        # Keeping the case explicit makes the mapping legible and
        # leaves room for future per-type capture-key drift without
        # touching the catch-all arm.
        value = captured.get("value")
        if isinstance(value, bool):
            return value
        return None
    value = captured.get("value")
    if isinstance(value, bool):
        return value
    return None

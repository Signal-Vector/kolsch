"""Branch value extraction — Phase 3.3 Step 1.

Maps a completed node's captured output to the boolean the executor
uses to filter outgoing edges by their ``branch:`` field (master
spec §2.5 / §6.5). Single function — :func:`compute_branch_value`
— typed so the worklist dispatcher reads it once and gates on the
return value.

Returns ``None`` for non-branchable nodes; the executor treats
``None`` as "fire every outgoing edge unconditionally" (the
validator already guarantees these nodes have no ``branch:`` edges,
so the runtime check is defensive).
"""

from __future__ import annotations

from typing import Any

__all__ = ["compute_branch_value"]


_BASH_TYPE_REF = "kolsch/nodes/bash"
_BRANCH_TYPE_REF = "kolsch/nodes/branch"


def compute_branch_value(
    node_def: Any,  # noqa: ANN401 — NodeDefUnion is a Pydantic alias
    captured: dict[str, Any],
) -> bool | None:
    """Extract the branchable boolean for edge dispatch.

    Master spec §6.5:

    - ``kolsch/nodes/bash``: ``True`` iff ``captured["exit_code"] == 0``.
    - ``kolsch/nodes/branch`` (Phase 3.6): ``captured["value"]`` —
      the :class:`workflows.builtins.branch.BranchRunner` coerces its
      ``condition:`` to ``bool`` before storing, so the guard below
      is for completeness rather than defence.
    - Authored Python ``@node`` returning ``bool`` or
      ``tuple[bool, BaseModel]``: the bool lives at ``captured["value"]``
      per master spec §4.0 line 217 (the
      :class:`workflows.builtins.catalog_node_runner.CatalogNodeRunner`
      stores it under the ``value`` key for both return shapes).
    - Everything else (``noop``, ``agent``, future leaves): ``None``.

    The ``isinstance(value, bool)`` check is intentional — Python's
    ``bool`` is a subclass of ``int``, but the executor's edge
    dispatch must NOT route on truthy/falsy ints. The validator
    has already rejected ``branch:`` edges from non-branchable
    sources at parse time; this runtime check defends against drift
    in the captured-output shape (e.g. a future authored node that
    returns an ``int`` rather than a ``bool``).
    """
    type_ref = getattr(node_def, "type", None)
    if type_ref == _BASH_TYPE_REF:
        return bool(captured.get("exit_code") == 0)
    if type_ref == _BRANCH_TYPE_REF:
        # Symmetric with the authored-Python path below: both branchable
        # source types capture their bool under ``value``. Keeping the
        # case explicit makes the master-spec §6.5 mapping legible and
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

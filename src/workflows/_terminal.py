"""Child-workflow terminal-output projection.

The [`WorkflowSubRunner`][workflows.builtins.workflow_sub.WorkflowSubRunner]
returns this helper's output as the value the parent's downstream
Jinja resolves via `nodes.<workflow_node>.<terminal_name>.<key>`.

A workflow's "output" is the dict keyed by its TERMINAL node names
(nodes with no outgoing graph edges). Each value is that terminal's
captured output dict — or `None` if the terminal didn't fire (the
branch wasn't taken; a `when:` gate fired false; the cascade pruned
it).

A child workflow's
[`IterativeExecutor`][workflows.executor.IterativeExecutor] internally
holds `_captured` (a per-name output dict) and `_pruned` (a set of
names suppressed by the branch-not-taken cascade). This helper builds
the projection against those two structures without re-walking the
graph at every parent consumer site.

Node-library workflows (`graph: None`) have no terminals — every node
is treated as terminal for the harvest. The harvest still works the
same way: `captured.get(name)` for each node name in the nodes dict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflows.parser import Workflow

__all__ = ["compute_terminal_output_dict"]


def compute_terminal_output_dict(
    workflow: Workflow,
    captured: dict[str, dict[str, Any]],
    pruned: set[str],
) -> dict[str, dict[str, Any] | None]:
    """Project a child workflow's run state into its terminal output dict.

    Walks `workflow.graph.edges` to compute the set of terminal nodes
    (nodes with no outgoing edges). For each terminal:

    - `None` if the node was pruned (branch-not-taken cascade) OR was
      structurally suppressed (the cascade adds pruned names to both
      `_pruned` and `_completed_or_skipped` so the membership check is
      on `pruned`);
    - the captured output dict otherwise (`captured.get(name)` returns
      `None` when the node was skipped via a `when:` gate with no
      completion event — the "skip is silent" convention preserves the
      same `None` shape).

    A workflow with `graph is None` (node library) has no edges and so
    every node is "terminal" for harvest purposes — but node libraries
    aren't runnable via
    [`IterativeExecutor`][workflows.executor.IterativeExecutor], so
    this branch is defensive only.

    Args:
        workflow: The child workflow whose terminals to project.
        captured: The executor's per-node output map.
        pruned: The set of node names suppressed by the
            branch-not-taken cascade.

    Returns:
        The projected dict keyed by terminal node name.
    """
    outgoing: dict[str, set[str]] = {n: set() for n in workflow.nodes}
    if workflow.graph is not None:
        for edge in workflow.graph.edges:
            outgoing.setdefault(edge.from_, set()).add(edge.to)
    terminals = [name for name in workflow.nodes if not outgoing.get(name)]
    return {
        name: (None if name in pruned else captured.get(name)) for name in terminals
    }

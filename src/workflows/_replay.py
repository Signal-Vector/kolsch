"""JSONL event replay — Phase 3.4 Step 1.

The resume entry point (:meth:`workflows.executor.IterativeExecutor.resume`)
loads a paused run's JSONL and walks it to rebuild the in-memory
state that the original (now-exited) executor process held: captured
node outputs, the pruned-nodes set, and the completed-or-skipped
bookkeeping. The replay then identifies the open ``user_input``
invocation (the node whose ``workflow.node.started`` event has no
matching completion in the JSONL).

The replay deliberately MUTATES the executor's private fields
directly. The alternative (a typed snapshot model that the executor
copies in via a public setter) adds two layers of indirection for
zero correctness benefit at this scope — the replay helper and the
executor are co-evolving and live in the same package. Document the
coupling in the function's docstring; widen the seam if a future
phase needs cross-process snapshots that aren't pure executor state.

Phase 3.4 replays:

- ``workflow.node.started`` — track the open-invocations map.
- ``workflow.node.completed`` — restore ``_captured`` + mark complete
  + close the open-invocation slot.
- ``workflow.node.pruned`` (Phase 3.3) — add to ``_pruned`` +
  ``_completed_or_skipped`` so the cascade survives resume; close the
  open-invocation slot.
- ``workflow.node.failed`` — defensive: a run that failed should
  never have reached resume (the status check at
  :meth:`IterativeExecutor.resume` entry rejects ``failed`` runs
  first). If the JSONL nevertheless carries a failed event, raise
  :class:`workflows.errors.WorkflowResumeRejected` rather than
  silently restore inconsistent state.

Future phases will extend the replay table as new event kinds land
(``set_state`` / ``delete_state`` in Phase 3.5 mutate ``_state``;
nested ``workflow.run.resumed`` in Phase 3.7 sub-workflows). The
contract is forward-compatible: unknown event kinds are ignored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from collections.abc import Iterable

from workflows.errors import WorkflowResumeRejected

if TYPE_CHECKING:
    from workflows.executor import IterativeExecutor

__all__ = ["replay_into"]


def replay_into(
    executor: IterativeExecutor,
    events: Iterable[dict[str, Any]],
) -> dict[str, str | None]:
    """Walk historical ``events`` and rebuild the executor's state.

    Mutates these private fields on ``executor``:

    - ``_captured`` — every ``workflow.node.completed`` payload's
      ``output`` lands at ``_captured[<node_name>]``.
    - ``_completed_or_skipped`` — every completed AND every pruned
      node is added.
    - ``_pruned`` — every ``workflow.node.pruned`` event's target.

    Returns a mapping ``{node_name: invocation_id | None}`` where a
    non-``None`` value identifies a node whose ``workflow.node.started``
    event has no matching closing event (completed / pruned). The
    paused ``user_input`` node is the canonical open invocation;
    Phase 3.7 sub-workflows may introduce nested opens.

    Raises :class:`WorkflowResumeRejected` defensively if a
    ``workflow.node.failed`` event is present (a failed run should
    not reach resume; the status-check gate at
    :meth:`IterativeExecutor.resume` entry is the primary boundary).
    """
    started: dict[str, str | None] = {}
    for event in events:
        kind = event.get("kind")
        if kind == "workflow.node.started":
            node_name = event.get("node_name") or event.get(
                "payload", {}
            ).get("node_name")
            invocation_id = event.get("invocation_id") or event.get(
                "payload", {}
            ).get("invocation_id")
            if node_name is None or invocation_id is None:
                continue
            started[str(node_name)] = str(invocation_id)
        elif kind == "workflow.node.completed":
            node_name = event.get("node_name") or event.get(
                "payload", {}
            ).get("node_name")
            if node_name is None:
                continue
            output = event.get("payload", {}).get("output") or {}
            executor._captured[str(node_name)] = dict(output)
            executor._completed_or_skipped.add(str(node_name))
            started[str(node_name)] = None
        elif kind == "workflow.node.pruned":
            node_name = event.get("node_name") or event.get(
                "payload", {}
            ).get("node_name")
            if node_name is None:
                continue
            executor._pruned.add(str(node_name))
            executor._completed_or_skipped.add(str(node_name))
            started[str(node_name)] = None
        elif kind == "workflow.node.failed":
            # Defensive — the resume entry's status check rejects
            # failed runs first; this guards against a future code
            # path that calls replay_into outside that gate.
            raise WorkflowResumeRejected(
                run_id=str(executor._run.run_id),
                status="failed",
            )
    return started

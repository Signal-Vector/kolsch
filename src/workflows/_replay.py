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

Phase 3.5 adds:

- ``workflow.state.mutated`` — apply ``set`` / ``delete`` ops to
  ``executor._state`` in JSONL order. The last event wins for any
  key with multiple writes (master spec §4.3.1 "evolving variable"
  pattern). ``delete`` on an absent key is silent — matches
  ``DeleteStateRunner`` semantics so replay is faithful to the
  original run.

Phase 3.7 adds:

- ``workflow.node.called_workflow`` — records the parent-side
  ``{invocation_id: child_run_id}`` marker on the executor so the
  resume cascade can identify which workflow-node invocation spawned
  which child without re-walking the JSONL. The event itself doesn't
  mutate any worklist state (the workflow node is OPEN until its
  matching ``workflow.node.completed`` lands).
- ``workflow.node.called_workflow_completed`` — pure forensics
  marker; replay ignores it (the open-invocation slot is closed by
  the matching ``workflow.node.completed``, not by this).

Unknown event kinds are silently ignored — forward-compat with future
event-log extensions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from collections.abc import Iterable

from workflows.errors import WorkflowResumeRejected

if TYPE_CHECKING:
    from workflows.executor import IterativeExecutor

__all__ = ["find_event_by_id", "replay_into"]


def find_event_by_id(
    events: Iterable[dict[str, Any]],
    target_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Walk ``events`` and return the prefix up to (and including)
    ``target_id`` plus the matching event itself.

    Used by Phase 3.8's fork (:meth:`workflows.executor.IterativeExecutor.start_from_fork`)
    to truncate the parent run's JSONL at the chosen fork point — the
    returned prefix is then replayed verbatim through
    :func:`replay_into`, restoring ``_captured`` / ``_state`` /
    ``_pruned`` / ``_completed_or_skipped`` to the parent's state at
    that moment.

    Returns ``None`` when ``target_id`` is absent from the iterable.
    The caller raises :class:`workflows.errors.WorkflowRuntimeError`
    with a fork-point-not-found message; the helper itself stays
    side-effect-free so it's reusable from non-fork callers (e.g.,
    forensics tools that need an event prefix).
    """
    collected: list[dict[str, Any]] = []
    for event in events:
        collected.append(event)
        if event.get("event_id") == target_id:
            return collected, event
    return None


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
        elif kind == "workflow.state.mutated":
            payload = event.get("payload") or {}
            key = payload.get("key")
            op = payload.get("op")
            if key is None or op not in ("set", "delete"):
                continue
            if op == "set":
                executor._state[str(key)] = payload.get("value")
            else:
                executor._state.pop(str(key), None)
        elif kind == "workflow.node.called_workflow":
            # Phase 3.7 — record the parent-side spawn marker so the
            # resume cascade can map an open workflow-node invocation
            # to its child run without re-walking the JSONL. The
            # open-invocations bookkeeping is NOT touched here — the
            # workflow node remains open until its matching
            # ``workflow.node.completed`` event lands; that pre-existing
            # path (above) is what closes the slot.
            payload = event.get("payload") or {}
            invocation_id = (
                event.get("invocation_id") or payload.get("invocation_id")
            )
            child_run_id = payload.get("child_workflow_run_id")
            if invocation_id is None or child_run_id is None:
                continue
            executor._workflow_node_child_runs[str(invocation_id)] = str(
                child_run_id
            )
        elif kind == "workflow.node.called_workflow_completed":
            # Phase 3.7 — forensics marker only. The slot is closed by
            # the matching ``workflow.node.completed`` (when the parent
            # eventually harvested), not by this event. For paused
            # workflow nodes this event always carries
            # ``child_status="waiting_for_input"`` and the slot stays
            # open across resume.
            pass
        elif kind == "workflow.node.failed":
            # Defensive — the resume entry's status check rejects
            # failed runs first; this guards against a future code
            # path that calls replay_into outside that gate.
            raise WorkflowResumeRejected(
                run_id=str(executor._run.run_id),
                status="failed",
            )
    return started

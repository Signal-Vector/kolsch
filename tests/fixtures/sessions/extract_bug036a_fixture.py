"""Extract the bug-036a JSONL fixture from a real user session.

The fixture backs ``tests/cli/chat/test_bug036a_fixture_replay.py``
(the AC #3 flagship test) + the AC #5 snapshot tests. It is a slice
of a real session, not synthetic — the whole point of AC #3 is
"replay the events a real user's chat produced and prove the fix
holds against that shape".

Refresh the fixture:

    uv run python tests/fixtures/sessions/extract_bug036a_fixture.py \\
        ~/Documents/kolsch/sessions/ses_<any-recent-successful-jd-run>.jsonl

The canonical source used at spec-write time is
``ses_d3029e63-7157-46c2-8834-070d0210f073.jsonl``. Any session that
invokes ``run_workflow_*`` on the ``job_description_review`` workflow
and completes at least one agent node with a ``sub_agent_event`` of
kind ``agent.message`` will produce an equivalent fixture.

The extracted excerpt keeps:

- The session bootstrap (``session.opened`` + ``config.set`` +
  ``thread.exchange_started`` + the ``user.message`` that triggered
  the workflow).
- The ``tool.called`` for ``run_workflow*``.
- ``ui.event`` envelopes: ``kolsch.card.opened`` (WorkflowCard
  mount), ``kolsch.workflow.progress`` for at least
  ``run.started`` + one ``node.started`` (agent type) +
  one ``node.completed`` for that node.
- ``ui.event`` envelopes: ``kolsch.workflow.node.sub_agent_event``
  containing at least one inner ``agent.message`` scoped to the
  agent node.
- The ``tool.result`` for the workflow call (must carry ``card_id``).

Everything else (secondary nodes, extra sub-agent chatter, model
request/response chatter that isn't inside the workflow scope) is
dropped so the fixture stays small.

bug-036d fixture extension — hand-edited additions
====================================================

The shipped fixture is **not** just this extractor's output. After
extraction, four synthetic events for a ``deduper`` agent-type
node (bug-036d Mockup 4 — multi-input fan-in) are spliced in
between the bash ``fetch_jd`` completion and the workflow's
``tool.result``:

1. ``kolsch.workflow.progress`` — ``workflow.node.started`` for
   ``deduper`` (``type: kolsch/nodes/agent``).
2. ``kolsch.workflow.node.sub_agent_event`` — inner
   ``user.message`` whose ``content`` is a **dict** with three
   labeled input keys (``headhunter_review`` /
   ``hiring_manager_review`` / ``cto_review``) — the shape a
   fan-in node's bound inputs take.
3. ``kolsch.workflow.node.sub_agent_event`` — inner
   ``agent.message`` with the synthesized reply.
4. ``kolsch.workflow.progress`` — ``workflow.node.completed`` for
   the same invocation.

If you refresh the fixture from a new source session, either:

* pick a source session that already contains a fan-in
  agent-type node whose bound-inputs dict has ≥ 2 keys (rare
  today — the ``deduper`` node in ``job_description_review``
  fits, but only if the sub-agent USER_MESSAGE happens to land
  in the JSONL), OR
* re-splice the four hand-edited events after running the
  extractor by running
  ``extend_bug036d_deduper_slice.py`` in this same directory.
  The ``deduper`` node is what makes Mockup 4's golden
  non-degenerate — without it the golden collapses to Mockup
  3's shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_FIXTURE_OUT = Path(__file__).resolve().parent / "ses_bug036a_jd_review_excerpt.jsonl"


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _extract(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a small representative excerpt covering the fixture shape."""
    out: list[dict[str, Any]] = []

    def keep(evt: dict[str, Any]) -> None:
        out.append(evt)

    # Bootstrap: first session.opened + config.set + thread.exchange_started
    # + user.message.
    bootstrap_kinds = {
        "session.opened",
        "config.set",
        "thread.exchange_started",
        "user.message",
    }
    seen_bootstrap: set[str] = set()
    for evt in events:
        t = evt.get("type", "")
        if t in bootstrap_kinds and t not in seen_bootstrap:
            keep(evt)
            seen_bootstrap.add(t)
            if bootstrap_kinds <= seen_bootstrap:
                break

    # Workflow call: first tool.called for run_workflow* (transcript-root).
    workflow_call: dict[str, Any] | None = None
    for evt in events:
        if evt.get("type") != "tool.called":
            continue
        name = evt.get("data", {}).get("tool_name", "")
        if isinstance(name, str) and (
            name == "run_workflow" or name.startswith("run_workflow_")
        ):
            workflow_call = evt
            break
    if workflow_call is None:
        raise SystemExit("no run_workflow tool.called in source session")
    keep(workflow_call)
    tool_call_id = workflow_call.get("data", {}).get("tool_call_id")

    # ui.event: card.opened (workflow card mount).
    for evt in events:
        if evt.get("type") != "ui.event":
            continue
        data = evt.get("data", {})
        if data.get("kind") == "kolsch.card.opened":
            keep(evt)
            break

    # ui.event: workflow.run.started.
    for evt in events:
        if evt.get("type") != "ui.event":
            continue
        data = evt.get("data", {})
        if data.get("kind") != "kolsch.workflow.progress":
            continue
        payload = data.get("payload", {})
        if payload.get("kind") == "workflow.run.started":
            keep(evt)
            break

    # Find the first agent-type node's started/completed + its
    # sub_agent_event(agent.message).
    target_node_name: str | None = None
    target_invocation_id: str | None = None
    node_started_evt: dict[str, Any] | None = None
    for evt in events:
        if evt.get("type") != "ui.event":
            continue
        data = evt.get("data", {})
        if data.get("kind") != "kolsch.workflow.progress":
            continue
        payload = data.get("payload", {})
        if payload.get("kind") != "workflow.node.started":
            continue
        inner = payload.get("payload") or {}
        if inner.get("type") == "kolsch/nodes/agent":
            target_node_name = payload.get("node_name")
            target_invocation_id = payload.get("invocation_id")
            node_started_evt = evt
            break

    if node_started_evt is None or target_node_name is None:
        raise SystemExit("no agent-type node in source session")

    keep(node_started_evt)

    # sub_agent_event(agent.message) scoped to this node.
    for evt in events:
        if evt.get("type") != "ui.event":
            continue
        data = evt.get("data", {})
        if data.get("kind") != "kolsch.workflow.node.sub_agent_event":
            continue
        payload = data.get("payload", {})
        if payload.get("invocation_id") != target_invocation_id:
            continue
        if payload.get("event_type") == "agent.message":
            keep(evt)
            break

    # workflow.node.completed for this node.
    for evt in events:
        if evt.get("type") != "ui.event":
            continue
        data = evt.get("data", {})
        if data.get("kind") != "kolsch.workflow.progress":
            continue
        payload = data.get("payload", {})
        if payload.get("kind") != "workflow.node.completed":
            continue
        if payload.get("invocation_id") == target_invocation_id:
            keep(evt)
            break

    # bug-036c: also try to include a bash-type node so the
    # ``test_bug036c_fixture_replay.py`` assertion has a subject.
    # Fall through silently if no bash node exists in the source
    # session — bug-036c fixture-replay is optional at that point.
    _include_first_bash_slice(
        events, out, target_invocation_id, thread_id=None, session_id=None
    )

    # tool.result for the workflow call (must carry card_id).
    for evt in events:
        if evt.get("type") != "tool.result":
            continue
        data = evt.get("data", {})
        if data.get("tool_call_id") != tool_call_id:
            continue
        keep(evt)
        break

    return out


def _include_first_bash_slice(
    events: list[dict[str, Any]],
    out: list[dict[str, Any]],
    _target_invocation_id: str | None,
    *,
    thread_id: str | None,
    session_id: str | None,
) -> None:
    """bug-036c: include the first bash-node started + completed pair.

    Optional slice — the bug-036c fixture-replay test only fires
    if a bash node is present. Missing (older sessions or a JD flow
    that skipped the fetch step) is fine; the test will fall back
    to a source-code-injected synthetic slice.
    """
    _ = thread_id, session_id
    bash_invocation: str | None = None
    for evt in events:
        if evt.get("type") != "ui.event":
            continue
        data = evt.get("data", {})
        if data.get("kind") != "kolsch.workflow.progress":
            continue
        payload = data.get("payload", {})
        if payload.get("kind") != "workflow.node.started":
            continue
        inner = payload.get("payload") or {}
        if inner.get("type") == "kolsch/nodes/bash":
            bash_invocation = payload.get("invocation_id")
            out.append(evt)
            break
    if bash_invocation is None:
        return
    for evt in events:
        if evt.get("type") != "ui.event":
            continue
        data = evt.get("data", {})
        if data.get("kind") != "kolsch.workflow.progress":
            continue
        payload = data.get("payload", {})
        if payload.get("kind") != "workflow.node.completed":
            continue
        if payload.get("invocation_id") == bash_invocation:
            out.append(evt)
            break


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract bug-036a JSONL fixture from a real session.",
    )
    ap.add_argument("source", type=Path, help="Path to a source session JSONL.")
    ap.add_argument(
        "--out",
        type=Path,
        default=_FIXTURE_OUT,
        help=f"Output path (default: {_FIXTURE_OUT}).",
    )
    args = ap.parse_args()

    events = _load(args.source)
    excerpt = _extract(events)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        for evt in excerpt:
            f.write(json.dumps(evt))
            f.write("\n")
    print(f"wrote {len(excerpt)} events -> {args.out}", file=sys.stderr)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

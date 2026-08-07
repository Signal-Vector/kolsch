"""Extend the bug-036a fixture with a deduper (fan-in) agent node.

bug-036d Reviewer feedback: mockup4 golden was byte-identical to
mockup3 because the fixture only contained one agent-type node
(``headhunter_review``). This adds a second agent node
(``deduper``) whose USER_MESSAGE content is a dict with three
labeled input keys — the shape the ``synthesize``-style fan-in
node produces. Regenerating mockup4 against this extended fixture
gives it something to actually assert.

The events are spliced in AFTER the ``fetch_jd`` bash node's
completion (line 14) and BEFORE the workflow ``tool.result``
(line 15). That keeps the workflow lifecycle intact — every node
that started also completed — so ``_update_header`` reports 3/3
rather than a partial-progress bar.

Run after ``extract_bug036a_fixture.py`` refreshes the base slice:

    uv run python tests/fixtures/sessions/extend_bug036d_deduper_slice.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SESSION_ID = "ses_d3029e63-7157-46c2-8834-070d0210f073"
THREAD_ID = "th_5f757b2c-a32e-4dbc-83d6-3f9408f1608a"
TRACE_ID = "tr_f7fdf208-af4a-4bc3-bb92-6af3d5af314c"
RUN_ID = "wrk_a11f4371-8c49-4b5f-9617-5e84b22b6867"
WORKFLOW_REF = "acme/workflows/job_description_review"

DEDUPER_INVOCATION = "inv_e3d0221c-804b-41a1-809d-89066173cb85"

# Stable UUIDs for the four new events (bug-036d fixture extension).
_EV_STARTED_OUTER = "ev_01c9bf1f-257c-4a87-a8c9-436637705ad1"
_EV_STARTED_INNER = "ev_aa6c5142-92d3-416d-a992-b7efb88e5fb9"
_EV_USER_OUTER = "ev_bd96e96c-1f9c-4826-a134-aa1c286a8fdd"
_EV_USER_INNER = "ev_b4d45e35-5cad-4292-91d3-9779ef2eba77"
_EV_AGENT_OUTER = "ev_6b8a569a-d201-493a-aa5d-7e1d82e1962b"
_EV_AGENT_INNER = "ev_72cddac2-cf58-4727-b630-43306197e500"
_EV_EXCHANGE = "ev_d62695b1-67a6-4129-9b3b-28fe692c4646"
_EV_COMPLETED_OUTER = "ev_0a19e5d0-d859-40f7-85a3-e4977c91545e"
# One more for the completed-inner event_id.
_EV_COMPLETED_INNER = "ev_e5b2d13f-2c1a-4ba4-9b18-f3a4e1c25a17"

_SCORES_HEADHUNTER = (
    "[\n"
    '  {"requirement": "Deep blockchain expertise including L1/L2 protocol design",'
    ' "score": 10, "rationale": "Scarcest skill; core technical differentiator."},\n'
    '  {"requirement": "12+ years engineering experience with architecture ownership",'
    ' "score": 9, "rationale": "Executive credibility for equity-partner CTO."},\n'
    '  {"requirement": "Hands-on Rust systems programming",'
    ' "score": 9, "rationale": "Explicitly mandatory; eliminates most candidates."}\n'
    "]"
)
_SCORES_HIRING_MANAGER = (
    "[\n"
    '  {"requirement": "Distributed team leadership in remote/async environments",'
    ' "score": 9, "rationale": "20 hrs/week distributed model demands strong async leaders."},\n'
    '  {"requirement": "Proven track record shipping to production",'
    ' "score": 8, "rationale": "Founders need tangible outcomes, not designs."},\n'
    '  {"requirement": "Product-focused mindset aligned to user impact",'
    ' "score": 7, "rationale": "Prevents blockchain theater; ties tech to revenue."}\n'
    "]"
)
_SCORES_CTO = (
    "[\n"
    '  {"requirement": "Production security leadership: threat modeling, secure SDLC",'
    ' "score": 10, "rationale": "Blockchain platform is a high-value attack target."},\n'
    '  {"requirement": "GCP mastery for infrastructure and observability",'
    ' "score": 8, "rationale": "Cloud foundation is inflexibly anchored to GCP."},\n'
    '  {"requirement": "Executive technical communication and investor readiness",'
    ' "score": 7, "rationale": "Founding CTO is the primary technical credibility lever."}\n'
    "]"
)

DEDUPER_INPUTS = {
    "headhunter_review": _SCORES_HEADHUNTER,
    "hiring_manager_review": _SCORES_HIRING_MANAGER,
    "cto_review": _SCORES_CTO,
}

DEDUPER_OUTPUT = (
    "[\n"
    '  {"rank": 1, "requirement": "Deep L1/L2 blockchain protocol design",'
    ' "recommended_by": ["cto","headhunter","hiring_manager"], "avg": 9.7},\n'
    '  {"rank": 2, "requirement": "Hands-on Rust systems programming",'
    ' "recommended_by": ["headhunter","hiring_manager"], "avg": 8.5},\n'
    '  {"rank": 3, "requirement": "Production security leadership",'
    ' "recommended_by": ["cto"], "avg": 10.0}\n'
    "]"
)


def _node_started_evt() -> dict[str, Any]:
    return {
        "actor": {"id": None, "kind": "system"},
        "content_ref": None,
        "created_at": "2026-07-09T22:11:00.000000Z",
        "data": {
            "kind": "kolsch.workflow.progress",
            "logical_seq": 40,
            "payload": {
                "event_id": _EV_STARTED_INNER,
                "invocation_id": DEDUPER_INVOCATION,
                "kind": "workflow.node.started",
                "node_name": "deduper",
                "payload": {
                    "caller_chain": [],
                    "inputs": DEDUPER_INPUTS,
                    "invocation_id": DEDUPER_INVOCATION,
                    "node_name": "deduper",
                    "type": "kolsch/nodes/agent",
                },
                "run_id": RUN_ID,
                "trace_id": TRACE_ID,
                "ts": "2026-07-09T22:11:00.000000+00:00",
                "workflow_ref": WORKFLOW_REF,
            },
            "schema_version": 1,
        },
        "event_id": _EV_STARTED_OUTER,
        "parent_event_id": "ev_c7aa19c3-27a9-5236-8a56-5a541ad79714",
        "session_id": SESSION_ID,
        "thread_id": THREAD_ID,
        "trace_id": TRACE_ID,
        "type": "ui.event",
    }


def _sub_agent_user_message_evt() -> dict[str, Any]:
    """USER_MESSAGE inner event: content is a multi-input dict.

    The workflow scheduler's bound-inputs to a fan-in agent node —
    ``deduper`` receives ``{"headhunter_review": "...",
    "hiring_manager_review": "...", "cto_review": "..."}`` and its
    first turn is a synthetic USER_MESSAGE carrying that dict as
    ``data.content``. There is no ``text`` key, so the
    ``_content_dict_text`` fallback in the adapter renders each
    labeled input line by line (via ``_format_content_dict``).
    """
    return {
        "actor": {"id": None, "kind": "system"},
        "content_ref": None,
        "created_at": "2026-07-09T22:11:05.000000Z",
        "data": {
            "kind": "kolsch.workflow.node.sub_agent_event",
            "logical_seq": 41,
            "payload": {
                "event_id": _EV_USER_INNER,
                "event_type": "user.message",
                "invocation_id": DEDUPER_INVOCATION,
                "node_name": "deduper",
                "payload": {
                    "content": DEDUPER_INPUTS,
                },
                "run_id": RUN_ID,
                "trace_id": TRACE_ID,
            },
            "schema_version": 1,
        },
        "event_id": _EV_USER_OUTER,
        "parent_event_id": _EV_STARTED_OUTER,
        "session_id": SESSION_ID,
        "thread_id": THREAD_ID,
        "trace_id": TRACE_ID,
        "type": "ui.event",
    }


def _sub_agent_agent_message_evt() -> dict[str, Any]:
    """AGENT_MESSAGE inner event: the fan-in agent's synthesized reply."""
    return {
        "actor": {"id": None, "kind": "system"},
        "content_ref": None,
        "created_at": "2026-07-09T22:11:20.000000Z",
        "data": {
            "kind": "kolsch.workflow.node.sub_agent_event",
            "logical_seq": 42,
            "payload": {
                "event_id": _EV_AGENT_INNER,
                "event_type": "agent.message",
                "invocation_id": DEDUPER_INVOCATION,
                "node_name": "deduper",
                "payload": {
                    "content": {"text": DEDUPER_OUTPUT},
                    "exchange_id": _EV_EXCHANGE,
                    "logical_seq": 6,
                },
                "run_id": RUN_ID,
                "trace_id": TRACE_ID,
            },
            "schema_version": 1,
        },
        "event_id": _EV_AGENT_OUTER,
        "parent_event_id": _EV_USER_OUTER,
        "session_id": SESSION_ID,
        "thread_id": THREAD_ID,
        "trace_id": TRACE_ID,
        "type": "ui.event",
    }


def _node_completed_evt() -> dict[str, Any]:
    return {
        "actor": {"id": None, "kind": "system"},
        "content_ref": None,
        "created_at": "2026-07-09T22:11:25.000000Z",
        "data": {
            "kind": "kolsch.workflow.progress",
            "logical_seq": 43,
            "payload": {
                "event_id": _EV_COMPLETED_INNER,
                "invocation_id": DEDUPER_INVOCATION,
                "kind": "workflow.node.completed",
                "node_name": "deduper",
                "payload": {
                    "invocation_id": DEDUPER_INVOCATION,
                    "node_name": "deduper",
                    "output": {"text": DEDUPER_OUTPUT},
                },
                "run_id": RUN_ID,
                "trace_id": TRACE_ID,
                "ts": "2026-07-09T22:11:25.000000+00:00",
                "workflow_ref": WORKFLOW_REF,
            },
            "schema_version": 1,
        },
        "event_id": _EV_COMPLETED_OUTER,
        "parent_event_id": _EV_AGENT_OUTER,
        "session_id": SESSION_ID,
        "thread_id": THREAD_ID,
        "trace_id": TRACE_ID,
        "type": "ui.event",
    }


def main() -> None:
    fixture_path = (
        Path(__file__).resolve().parent / "ses_bug036a_jd_review_excerpt.jsonl"
    )
    lines = [ln for ln in fixture_path.read_text().splitlines() if ln]
    # Idempotent: strip any prior deduper events (identified by the
    # stable EV_STARTED_OUTER id) before re-splicing.
    lines = [ln for ln in lines if DEDUPER_INVOCATION not in ln]
    # Splice deduper events immediately before the workflow
    # ``tool.result`` (the last event in the base slice).
    new_events = [
        _node_started_evt(),
        _sub_agent_user_message_evt(),
        _sub_agent_agent_message_evt(),
        _node_completed_evt(),
    ]
    new_lines = [json.dumps(e) for e in new_events]
    output = lines[:-1] + new_lines + [lines[-1]]
    fixture_path.write_text("\n".join(output) + "\n")
    print(f"wrote {len(output)} events to {fixture_path}")  # noqa: T201


if __name__ == "__main__":
    main()

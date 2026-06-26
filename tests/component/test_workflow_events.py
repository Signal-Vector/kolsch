"""Phase 3.1 Step 5 — event-envelope + ordering + caller_chain tests.

Covers ACs 5.1-5.4:

- 5.1: every executor-emitted JSONL line is a valid Pydantic envelope
  (common fields + node-scoped + caller_chain shape).
- 5.2: master spec §11.2 ordering — ``workflow.run.started`` first;
  per-node ``started`` / ``completed`` / ``failed`` pairs nested
  between; terminal ``workflow.run.*`` last.
- 5.3: ``caller_chain`` persists in ``workflow.node.started`` payload.
  (``workflow.tool.called`` carrier ships in Phase 3.2 + dispatcher
  wire-up.)
- 5.4: ``policy.decision.made`` fires when ``audit_level == "all"``;
  defaults (``"rule_matched"``) emit only when a rule matched.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog
from providers.authz import (
    RuleBasedAuthorizationProvider,
)
from workflows.events import WorkflowEventKind
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


class _StaticRunner:
    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, state, ctx)
        return dict(self._output)


class _RaisingRunner:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, **_: Any) -> dict[str, Any]:
        raise self._exc


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
    inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs=inputs or {})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# AC 5.1 — envelope shape
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    async def test_run_started_envelope_has_common_fields(
        self, tmp_path: Path
    ) -> None:
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        events = list(store.read_events(run.run_id))
        started = next(
            e for e in events if e["kind"] == "workflow.run.started"
        )
        # Common-field hoist — top-level on every envelope.
        assert started["event_id"].startswith("ev_")
        assert started["parent_event_id"] is None
        assert started["workflow_run_id"] == str(run.run_id)
        assert started["trace_id"] == str(run.trace_id)
        assert "ts" in started
        assert "payload" in started

    async def test_node_envelope_hoists_invocation_id_and_node_name(
        self, tmp_path: Path
    ) -> None:
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        events = list(store.read_events(run.run_id))
        node_started = next(
            e for e in events if e["kind"] == "workflow.node.started"
        )
        # Top-level fields per master spec §11.3.
        assert node_started["node_name"] == "a"
        assert node_started["invocation_id"].startswith("inv_")
        # Payload back-compat — Step 1-4 readers still work.
        assert node_started["payload"]["node_name"] == "a"

    def test_event_kind_enum_lists_13_kinds(self) -> None:
        """AC 5.1 — the StrEnum vocabulary is exactly the 13 executor-
        emitted kinds documented in master spec §11.1."""
        assert len(list(WorkflowEventKind)) == 13


# ---------------------------------------------------------------------------
# AC 5.2 — event ordering
# ---------------------------------------------------------------------------


class TestEventOrdering:
    async def test_linear_pipeline_event_order(self, tmp_path: Path) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                    "c": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "a", "to": "b"},
                        {"from": "b", "to": "c"},
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        # Master spec §11.2 — exact ordering.
        assert kinds == [
            "workflow.run.started",
            "workflow.node.started",
            "workflow.node.completed",
            "workflow.node.started",
            "workflow.node.completed",
            "workflow.node.started",
            "workflow.node.completed",
            "workflow.run.completed",
        ]

    async def test_node_failed_pairs_with_node_started(
        self, tmp_path: Path
    ) -> None:
        """The ``workflow.node.failed`` event's ``parent_event_id``
        points at its matching ``workflow.node.started`` — not the
        run-level start."""
        wf = _wf(
            {
                "nodes": {
                    "first": {"type": "kolsch/nodes/noop"},
                    "second": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "first", "to": "second"}]},
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _StaticRunner({}))
        registry.register(
            "kolsch/nodes/noop",  # second registration overwrites
            _RaisingRunner(RuntimeError("boom")),
        )
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        started = next(
            e for e in events if e["kind"] == "workflow.node.started"
        )
        failed = next(
            e for e in events if e["kind"] == "workflow.node.failed"
        )
        # Parent of node.failed is the matching node.started.
        assert failed["parent_event_id"] == started["event_id"]
        # And run.failed's parent is node.failed.
        run_failed = next(
            e for e in events if e["kind"] == "workflow.run.failed"
        )
        assert run_failed["parent_event_id"] == failed["event_id"]

    async def test_terminal_event_is_last(self, tmp_path: Path) -> None:
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        events = list(store.read_events(run.run_id))
        terminal_kinds = {
            "workflow.run.completed",
            "workflow.run.failed",
            "workflow.run.timed_out",
            "workflow.run.aborted",
        }
        assert events[-1]["kind"] in terminal_kinds


# ---------------------------------------------------------------------------
# AC 5.3 — caller_chain persistence in workflow.node.started
# ---------------------------------------------------------------------------


class TestCallerChain:
    async def test_node_started_payload_has_caller_chain_field(
        self, tmp_path: Path
    ) -> None:
        """v1 ships the empty chain (root-entry runs have none); the
        FIELD existence + shape is the contract Phase 3.7 fills."""
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        events = list(store.read_events(run.run_id))
        node_started = next(
            e for e in events if e["kind"] == "workflow.node.started"
        )
        assert "caller_chain" in node_started["payload"]
        # Root-entry run carries an empty chain; Phase 3.7 sub-workflow
        # wiring is what populates this with the parent frames.
        assert node_started["payload"]["caller_chain"] == []


# ---------------------------------------------------------------------------
# AC 5.4 — policy.decision.made emission
# ---------------------------------------------------------------------------


class TestPolicyDecisionMade:
    async def test_audit_level_all_emits_on_default_deny(
        self, tmp_path: Path
    ) -> None:
        """With ``audit_level: "all"`` AND no matching rule (default
        deny), the policy event still lands in the JSONL."""
        from workflows.run_context import (
            WorkflowRunContext,
            reset_workflow_run_context,
            set_workflow_run_context,
        )

        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/x", inputs={})
        provider = RuleBasedAuthorizationProvider(
            config={"rules": [], "audit_level": "all"}
        )
        # Seed a ``workflow.run.started`` so the JSONL exists; the
        # policy emit is the next line.
        store.append_event(
            run.run_id,
            {"kind": "workflow.run.started", "payload": {}},
        )
        token = set_workflow_run_context(
            WorkflowRunContext(
                run_id=run.run_id,
                trace_id=run.trace_id,
                store=store,
            )
        )
        try:
            await provider.allow(
                "user.local",
                "tool.unmapped_action_X",
                target="some_target",
                layers=(),
            )
        finally:
            reset_workflow_run_context(token)
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        assert "policy.decision.made" in kinds
        policy = next(
            e for e in events if e["kind"] == "policy.decision.made"
        )
        assert policy["payload"]["effect"] == "deny"
        assert policy["payload"]["action"] == "tool.unmapped_action_X"

    async def test_audit_level_rule_matched_skips_default_deny(
        self, tmp_path: Path
    ) -> None:
        """With ``audit_level: "rule_matched"`` (the default), a
        default-deny outcome does NOT emit — only matched rules do."""
        from workflows.run_context import (
            WorkflowRunContext,
            reset_workflow_run_context,
            set_workflow_run_context,
        )

        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/x", inputs={})
        provider = RuleBasedAuthorizationProvider(
            config={"rules": []}  # audit_level defaults to "rule_matched"
        )
        store.append_event(
            run.run_id,
            {"kind": "workflow.run.started", "payload": {}},
        )
        token = set_workflow_run_context(
            WorkflowRunContext(
                run_id=run.run_id,
                trace_id=run.trace_id,
                store=store,
            )
        )
        try:
            await provider.allow(
                "user.local",
                "tool.unmapped_action_X",
                target="some_target",
                layers=(),
            )
        finally:
            reset_workflow_run_context(token)
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        assert "policy.decision.made" not in kinds

    async def test_audit_level_rule_matched_emits_when_rule_fires(
        self, tmp_path: Path
    ) -> None:
        """A matching rule with ``audit_level: "rule_matched"`` DOES
        emit. The payload carries the rule id."""
        from workflows.run_context import (
            WorkflowRunContext,
            reset_workflow_run_context,
            set_workflow_run_context,
        )

        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/x", inputs={})
        provider = RuleBasedAuthorizationProvider(
            config={
                "rules": [
                    {"what": "*", "pattern": "*", "effect": "allow"}
                ]
            }
        )
        store.append_event(
            run.run_id,
            {"kind": "workflow.run.started", "payload": {}},
        )
        token = set_workflow_run_context(
            WorkflowRunContext(
                run_id=run.run_id,
                trace_id=run.trace_id,
                store=store,
            )
        )
        try:
            result = await provider.allow(
                "user.local",
                "tool.bash",
                target="some_target",
                layers=(),
            )
        finally:
            reset_workflow_run_context(token)
        assert result.allowed is True
        events = list(store.read_events(run.run_id))
        policy = next(
            e for e in events if e["kind"] == "policy.decision.made"
        )
        assert policy["payload"]["effect"] == "allow"
        assert policy["payload"]["rule_id"] is not None

    async def test_no_run_context_means_no_emission(
        self, tmp_path: Path
    ) -> None:
        """Non-workflow callers (chat, ``kolsch run``, tests that
        don't construct a run) observe zero overhead — no contextvar
        set, no JSONL emit."""
        from workflows.run_context import (
            current_workflow_run_context,
        )

        provider = RuleBasedAuthorizationProvider(
            config={"rules": [], "audit_level": "all"}
        )
        # Sanity: no context set at the start of the test.
        assert current_workflow_run_context() is None
        # No store hooked, so any emission attempt would crash. The
        # fact that this call completes proves the gate works.
        result = await provider.allow(
            "user.local",
            "tool.bash",
            target="x",
            layers=(),
        )
        assert result.effect.value == "deny"
        # Sanity check the tmp_path isn't littered with a runs/ dir.
        assert not (tmp_path / "runs").exists()


# ---------------------------------------------------------------------------
# JSONL is valid line-delimited JSON.
# ---------------------------------------------------------------------------


def test_jsonl_lines_each_parse_as_json(tmp_path: Path) -> None:
    """Every JSONL line is exactly one valid JSON object — no embedded
    newlines, no half-flushed records."""
    wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", _StaticRunner({}))
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    ex = IterativeExecutor(
        workflow=wf,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    import asyncio

    asyncio.run(ex.run())
    raw = store.directory(run.run_id).events_path.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert lines
    for line in lines:
        json.loads(line)  # raises on malformed line

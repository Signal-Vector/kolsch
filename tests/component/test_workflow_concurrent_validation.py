"""Phase 3.10 Step 1 — run-start gate concurrency invariants.

Covers ACs 1.1 — 1.10 from the build doc:

- ``_collect_parallel_branch_groups`` correctly decomposes fan-out
  points into branch node sets.
- ``_branch_state_write_set`` collects keys from ``set_state`` /
  ``delete_state`` nodes and recursively from sequential map workers.
- ``_branch_state_read_set`` collects ``state.<key>`` references from
  Jinja config strings.
- ``_check_concurrency_invariants`` raises
  :class:`WorkflowParallelStateConflict` on write-write, write-read,
  or read-write conflicts.
- ``parallel: true`` maps whose worker writes state are rejected.
- ``concurrency: <= 0`` rejected with
  :class:`WorkflowInvalidConcurrency`.
- ``sequential``-mode workflows skip the cross-branch conflict
  analysis (sub-passes 1 + 2 still fire because they're structural).
- Sub-workflow nodes inside a branch are OPAQUE (their internal
  writes do not leak into the parent's write set).
"""

from __future__ import annotations

from typing import Any

import pytest

from assets.catalog import AssetCatalog
from workflows.errors import (
    WorkflowInvalidConcurrency,
    WorkflowParallelStateConflict,
)
from workflows.parser import Workflow
from workflows.validator import (
    _branch_state_read_set,
    _branch_state_write_set,
    _check_concurrency_invariants,
    _collect_parallel_branch_groups,
    validate_workflow,
    validate_workflow_or_raise,
)


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


# ---------------------------------------------------------------------------
# AC 1.1 — _collect_parallel_branch_groups decomposition
# ---------------------------------------------------------------------------


class TestBranchDecomposition:
    def test_simple_two_branch_fan_out(self) -> None:
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "start": {"type": "kolsch/nodes/noop"},
                    "left": {"type": "kolsch/nodes/noop"},
                    "right": {"type": "kolsch/nodes/noop"},
                    "join": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "start", "to": "left"},
                        {"from": "start", "to": "right"},
                        {"from": "left", "to": "join"},
                        {"from": "right", "to": "join"},
                    ],
                },
            }
        )
        groups = _collect_parallel_branch_groups(wf)
        assert len(groups) == 1
        fan_out, branches = groups[0]
        assert fan_out == "start"
        assert {frozenset(b) for b in branches} == {
            frozenset({"left"}),
            frozenset({"right"}),
        }

    def test_three_way_fan_out(self) -> None:
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "start": {"type": "kolsch/nodes/noop"},
                    "alpha": {"type": "kolsch/nodes/noop"},
                    "beta": {"type": "kolsch/nodes/noop"},
                    "gamma": {"type": "kolsch/nodes/noop"},
                    "end": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "start", "to": "alpha"},
                        {"from": "start", "to": "beta"},
                        {"from": "start", "to": "gamma"},
                        {"from": "alpha", "to": "end"},
                        {"from": "beta", "to": "end"},
                        {"from": "gamma", "to": "end"},
                    ],
                },
            }
        )
        groups = _collect_parallel_branch_groups(wf)
        assert len(groups) == 1
        _fan_out, branches = groups[0]
        assert len(branches) == 3

    def test_branch_edge_not_a_fan_out(self) -> None:
        """Edges carrying ``branch:`` are exclusive routes — not a
        parallel fan-out surface."""
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "decide": {
                        "type": "kolsch/nodes/branch",
                        "condition": "{{ true }}",
                    },
                    "yes_path": {"type": "kolsch/nodes/noop"},
                    "no_path": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "decide", "to": "yes_path", "branch": True},
                        {"from": "decide", "to": "no_path", "branch": False},
                    ],
                },
            }
        )
        groups = _collect_parallel_branch_groups(wf)
        assert groups == []

    def test_branch_walks_until_confluence(self) -> None:
        """A multi-node branch is walked all the way to the next
        confluence (exclusive). Confluence node is NOT part of any
        branch."""
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "s": {"type": "kolsch/nodes/noop"},
                    "a1": {"type": "kolsch/nodes/noop"},
                    "a2": {"type": "kolsch/nodes/noop"},
                    "b1": {"type": "kolsch/nodes/noop"},
                    "join": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "s", "to": "a1"},
                        {"from": "a1", "to": "a2"},
                        {"from": "s", "to": "b1"},
                        {"from": "a2", "to": "join"},
                        {"from": "b1", "to": "join"},
                    ],
                },
            }
        )
        groups = _collect_parallel_branch_groups(wf)
        assert len(groups) == 1
        _, branches = groups[0]
        branch_sets = {frozenset(b) for b in branches}
        assert branch_sets == {frozenset({"a1", "a2"}), frozenset({"b1"})}
        # The confluence node 'join' belongs to NO branch.
        assert all("join" not in b for b in branches)

    def test_nested_fan_out_surfaces_both_levels(self) -> None:
        """Inner + outer fan-outs each produce their own group."""
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "s": {"type": "kolsch/nodes/noop"},
                    "outer_left": {"type": "kolsch/nodes/noop"},
                    "inner_top": {"type": "kolsch/nodes/noop"},
                    "inner_bot": {"type": "kolsch/nodes/noop"},
                    "outer_right": {"type": "kolsch/nodes/noop"},
                    "join_inner": {"type": "kolsch/nodes/noop"},
                    "join_outer": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "s", "to": "outer_left"},
                        {"from": "s", "to": "outer_right"},
                        {"from": "outer_left", "to": "inner_top"},
                        {"from": "outer_left", "to": "inner_bot"},
                        {"from": "inner_top", "to": "join_inner"},
                        {"from": "inner_bot", "to": "join_inner"},
                        {"from": "join_inner", "to": "join_outer"},
                        {"from": "outer_right", "to": "join_outer"},
                    ],
                },
            }
        )
        groups = _collect_parallel_branch_groups(wf)
        # Two fan-out points: 's' and 'outer_left'.
        fan_outs = {g[0] for g in groups}
        assert fan_outs == {"s", "outer_left"}


# ---------------------------------------------------------------------------
# AC 1.2 — _branch_state_write_set
# ---------------------------------------------------------------------------


class TestStateWriteSet:
    def test_collects_set_state_keys(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ss": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"alpha": 1, "beta": 2},
                    },
                },
            }
        )
        writes = _branch_state_write_set(wf, {"ss"})
        assert set(writes) == {"alpha", "beta"}
        assert writes["alpha"] == "ss"

    def test_collects_delete_state_keys(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ds": {
                        "type": "kolsch/nodes/delete_state",
                        "keys": ["x", "y"],
                    },
                },
            }
        )
        writes = _branch_state_write_set(wf, {"ds"})
        assert set(writes) == {"x", "y"}

    def test_sequential_map_recurses_into_worker(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "{{ items }}",
                        "item_as": "row",
                    },
                    "worker": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"k": "{{ row }}"},
                    },
                },
            }
        )
        writes = _branch_state_write_set(wf, {"fan"})
        assert set(writes) == {"k"}

    def test_parallel_map_contributes_no_writes(self) -> None:
        """Parallel maps that survive validation cannot mutate state
        (the gate rejects them otherwise); ``_branch_state_write_set``
        therefore skips them."""
        wf = _wf(
            {
                "nodes": {
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "{{ items }}",
                        "item_as": "row",
                        "parallel": True,
                    },
                    "worker": {"type": "kolsch/nodes/noop"},
                },
            }
        )
        writes = _branch_state_write_set(wf, {"fan"})
        assert writes == {}

    def test_sub_workflow_is_opaque(self) -> None:
        """A ``kolsch/nodes/workflow`` node does NOT contribute its
        child's writes — child state is isolated (Phase 3.7)."""
        wf = _wf(
            {
                "nodes": {
                    "sub": {
                        "type": "kolsch/nodes/workflow",
                        "ref": "acme/workflows/inner",
                    },
                },
            }
        )
        writes = _branch_state_write_set(wf, {"sub"})
        assert writes == {}


# ---------------------------------------------------------------------------
# AC 1.3 — _branch_state_read_set
# ---------------------------------------------------------------------------


class TestStateReadSet:
    def test_jinja_state_ref_is_detected(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "n": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo {{ state.greeting }}",
                    },
                },
            }
        )
        reads = _branch_state_read_set(wf, {"n"})
        assert set(reads) == {"greeting"}
        assert reads["greeting"] == "n"

    def test_multiple_keys_in_one_string(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "n": {
                        "type": "kolsch/nodes/bash",
                        "command": (
                            "echo {{ state.a }} and {{ state.b }}"
                        ),
                    },
                },
            }
        )
        reads = _branch_state_read_set(wf, {"n"})
        assert set(reads) == {"a", "b"}

    def test_read_set_descends_into_set_state_keys(self) -> None:
        """``SetStateNodeDef.keys:`` values are Jinja-evaluable and
        get scanned for ``state.<key>`` reads."""
        wf = _wf(
            {
                "nodes": {
                    "n": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"out": "{{ state.config }}"},
                    },
                },
            }
        )
        reads = _branch_state_read_set(wf, {"n"})
        assert "config" in reads


# ---------------------------------------------------------------------------
# AC 1.4 — conflict detection between sibling branches
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def test_write_write_conflict_rejected(self) -> None:
        """Phase Test 4 — two branches both writing ``state.counter``."""
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "s": {"type": "kolsch/nodes/noop"},
                    "bump_a": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"counter": 1},
                    },
                    "bump_b": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"counter": 2},
                    },
                    "join": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "s", "to": "bump_a"},
                        {"from": "s", "to": "bump_b"},
                        {"from": "bump_a", "to": "join"},
                        {"from": "bump_b", "to": "join"},
                    ],
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        conflicts = [
            e for e in result.errors
            if isinstance(e, WorkflowParallelStateConflict)
        ]
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert conflict.state_key == "counter"
        assert {conflict.node_a, conflict.node_b} == {"bump_a", "bump_b"}

    def test_write_read_conflict_rejected(self) -> None:
        """Read-after-write rejection — branch A writes ``state.config``;
        branch B reads via ``{{ state.config }}``."""
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "s": {"type": "kolsch/nodes/noop"},
                    "writer": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"config": "hello"},
                    },
                    "reader": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo {{ state.config }}",
                    },
                    "join": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "s", "to": "writer"},
                        {"from": "s", "to": "reader"},
                        {"from": "writer", "to": "join"},
                        {"from": "reader", "to": "join"},
                    ],
                },
            }
        )
        with pytest.raises(WorkflowParallelStateConflict) as exc_info:
            validate_workflow_or_raise(wf, AssetCatalog())
        assert exc_info.value.state_key == "config"

    def test_disjoint_writes_allowed(self) -> None:
        """Two branches writing DIFFERENT state keys — pass the gate."""
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "s": {"type": "kolsch/nodes/noop"},
                    "write_a": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"a": 1},
                    },
                    "write_b": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"b": 2},
                    },
                    "join": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "s", "to": "write_a"},
                        {"from": "s", "to": "write_b"},
                        {"from": "write_a", "to": "join"},
                        {"from": "write_b", "to": "join"},
                    ],
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        conflicts = [
            e for e in result.errors
            if isinstance(e, WorkflowParallelStateConflict)
        ]
        assert conflicts == []

    def test_three_way_conflict_surfaces_pairs(self) -> None:
        """Three branches all writing the same key: every conflicting
        pair surfaces a finding."""
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "s": {"type": "kolsch/nodes/noop"},
                    "a": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"k": 1},
                    },
                    "b": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"k": 2},
                    },
                    "c": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"k": 3},
                    },
                    "end": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "s", "to": "a"},
                        {"from": "s", "to": "b"},
                        {"from": "s", "to": "c"},
                        {"from": "a", "to": "end"},
                        {"from": "b", "to": "end"},
                        {"from": "c", "to": "end"},
                    ],
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        conflicts = [
            e for e in result.errors
            if isinstance(e, WorkflowParallelStateConflict)
        ]
        # 3 sibling branches, all-pairs combinations = C(3, 2) = 3.
        assert len(conflicts) == 3


# ---------------------------------------------------------------------------
# AC 1.5 — parallel: true map worker writes state → rejected
# ---------------------------------------------------------------------------


class TestParallelMapStateRejection:
    def test_parallel_map_set_state_worker_rejected(self) -> None:
        """Phase Test 5 — a parallel map's worker writes state."""
        wf = _wf(
            {
                "nodes": {
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "{{ items }}",
                        "item_as": "row",
                        "parallel": True,
                    },
                    "worker": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"k": "{{ row }}"},
                    },
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        conflicts = [
            e for e in result.errors
            if isinstance(e, WorkflowParallelStateConflict)
        ]
        assert any(c.node_a == "fan" for c in conflicts)

    def test_parallel_map_delete_state_worker_rejected(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "{{ items }}",
                        "item_as": "row",
                        "parallel": True,
                    },
                    "worker": {
                        "type": "kolsch/nodes/delete_state",
                        "keys": ["k"],
                    },
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowParallelStateConflict)
            for e in result.errors
        )

    def test_parallel_map_nested_map_worker_rejected(self) -> None:
        """A parallel map whose worker is itself a (sequential) map
        whose worker mutates state is REJECTED — concurrent iterations
        of the outer map each invoke the inner chain, racing on the
        state mutation."""
        wf = _wf(
            {
                "nodes": {
                    "outer": {
                        "type": "kolsch/nodes/map",
                        "apply": "inner",
                        "over": "{{ outers }}",
                        "item_as": "o",
                        "parallel": True,
                    },
                    "inner": {
                        "type": "kolsch/nodes/map",
                        "apply": "writer",
                        "over": "{{ inners }}",
                        "item_as": "i",
                    },
                    "writer": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"k": "{{ i }}"},
                    },
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowParallelStateConflict)
            for e in result.errors
        )

    def test_parallel_map_pure_worker_allowed(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "{{ items }}",
                        "item_as": "row",
                        "parallel": True,
                        "concurrency": 5,
                    },
                    "worker": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo {{ row }}",
                    },
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert not any(
            isinstance(e, WorkflowParallelStateConflict)
            for e in result.errors
        )


# ---------------------------------------------------------------------------
# AC 1.6 — concurrency: <= 0 rejected
# ---------------------------------------------------------------------------


class TestInvalidConcurrency:
    @pytest.mark.parametrize("bad", [0, -1, -10])
    def test_non_positive_concurrency_rejected(self, bad: int) -> None:
        wf = _wf(
            {
                "nodes": {
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "{{ items }}",
                        "item_as": "row",
                        "parallel": True,
                        "concurrency": bad,
                    },
                    "worker": {"type": "kolsch/nodes/noop"},
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        matches = [
            e for e in result.errors
            if isinstance(e, WorkflowInvalidConcurrency)
        ]
        assert len(matches) == 1
        assert matches[0].node == "fan"
        assert matches[0].value == bad

    def test_positive_concurrency_passes(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "{{ items }}",
                        "item_as": "row",
                        "parallel": True,
                        "concurrency": 4,
                    },
                    "worker": {"type": "kolsch/nodes/noop"},
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert not any(
            isinstance(e, WorkflowInvalidConcurrency)
            for e in result.errors
        )


# ---------------------------------------------------------------------------
# AC 1.7 — sequential mode skips cross-branch analysis
# ---------------------------------------------------------------------------


class TestSequentialSkipsCrossBranchAnalysis:
    def test_sequential_mode_allows_conflicting_writes(self) -> None:
        """A workflow with two branches writing the same state key
        passes the gate under ``concurrency: sequential`` because the
        executor runs them serially — no concurrency, no conflict."""
        wf = _wf(
            {
                "concurrency": "sequential",
                "nodes": {
                    "s": {"type": "kolsch/nodes/noop"},
                    "bump_a": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"counter": 1},
                    },
                    "bump_b": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"counter": 2},
                    },
                    "join": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "s", "to": "bump_a"},
                        {"from": "s", "to": "bump_b"},
                        {"from": "bump_a", "to": "join"},
                        {"from": "bump_b", "to": "join"},
                    ],
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert not any(
            isinstance(e, WorkflowParallelStateConflict)
            for e in result.errors
        )

    def test_sequential_mode_still_rejects_parallel_map_state_writes(
        self,
    ) -> None:
        """``parallel: true`` on a map is a per-map opt-in independent
        of the workflow-level mode — a parallel map that writes state
        is still rejected even when the workflow is sequential."""
        wf = _wf(
            {
                "concurrency": "sequential",
                "nodes": {
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "{{ items }}",
                        "item_as": "row",
                        "parallel": True,
                    },
                    "worker": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"k": "{{ row }}"},
                    },
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowParallelStateConflict)
            for e in result.errors
        )


# ---------------------------------------------------------------------------
# AC 1.8 — sub-workflow opacity
# ---------------------------------------------------------------------------


class TestSubWorkflowOpacity:
    def test_sub_workflow_in_branch_does_not_contribute_writes(self) -> None:
        """A branch containing a ``kolsch/nodes/workflow`` sub-workflow
        node contributes NO writes from that sub-workflow — child
        state is isolated per Phase 3.7. Two such branches do not
        conflict on the same key even when both invoke the same child."""
        wf = _wf(
            {
                "concurrency": "auto",
                "nodes": {
                    "s": {"type": "kolsch/nodes/noop"},
                    "sub_a": {
                        "type": "kolsch/nodes/workflow",
                        "ref": "acme/workflows/inner",
                    },
                    "sub_b": {
                        "type": "kolsch/nodes/workflow",
                        "ref": "acme/workflows/inner",
                    },
                    "join": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "s", "to": "sub_a"},
                        {"from": "s", "to": "sub_b"},
                        {"from": "sub_a", "to": "join"},
                        {"from": "sub_b", "to": "join"},
                    ],
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert not any(
            isinstance(e, WorkflowParallelStateConflict)
            for e in result.errors
        )


# Step 1 + Step 4 — runtime-change guard removed; the parallel map
# runner now legitimately references both fields.


# ---------------------------------------------------------------------------
# Direct entry point — _check_concurrency_invariants smoke
# ---------------------------------------------------------------------------


def test_check_concurrency_invariants_returns_errors_and_warnings() -> None:
    """The layer signature returns ``(errors, warnings)``; warnings is
    always empty at this layer (no non-blocking concurrency findings
    at Step 1)."""
    wf = _wf({"nodes": {"n": {"type": "kolsch/nodes/noop"}}})
    errors, warnings = _check_concurrency_invariants(
        workflow=wf, catalog=AssetCatalog(), file="<wf>"
    )
    assert errors == []
    assert warnings == []

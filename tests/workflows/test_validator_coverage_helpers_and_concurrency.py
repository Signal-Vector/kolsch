"""Coverage tests for validator helpers, catalog-node signature modes,
and the concurrency-invariant sub-passes.

Groups:

- ``_related_issues_tail`` single-error note + multi-layer tail.
- ``_check_extends_policy`` — extended workflow carrying ``policy:``
  is rejected; unresolved / policy-free refs pass.
- Structural: ``copy_of:`` source unreachable; a self-referential
  edge shape that forces the zero-outgoing ``real_starts`` fallback.
- ``_check_jinja_refs`` / ``_check_value_for_refs`` — node-not-defined
  inside a node config Jinja ref, and the bare ``nodes.<pred>`` access
  pass-through.
- ``_agent_output_field_set`` degradation (no catalog, catalog miss,
  malformed YAML, non-mapping ``io``, non-mapping ``outputs``).
- Catalog-node signature modes NOT covered elsewhere: keyword-only
  param, param-1 not a ``BaseModel``, missing return annotation, bad
  optional param-3, unreadable-signature fallback.
- Concurrency invariants: sequential map worker recursion (write +
  read sets), ``_walk_worker_descendants`` map recursion, cross-branch
  write-read conflict, and the ``_record_conflict`` dedup path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from workflows.errors import (
    WorkflowExtendsPolicyForbidden,
    WorkflowNodeNotDefined,
    WorkflowNodeSignatureInvalid,
    WorkflowParallelStateConflict,
    WorkflowUnreachableNode,
)
from workflows.parser import Workflow
from workflows.validator import (
    _agent_output_field_set,
    _branch_state_read_set,
    _branch_state_write_set,
    _check_concurrency_invariants,
    _collect_parallel_branch_groups,
    _extract_nodes_refs,
    _related_issues_tail,
    _walk_worker_descendants,
    validate_workflow,
    validate_workflow_or_raise,
)

if TYPE_CHECKING:
    from pathlib import Path


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


# ---------------------------------------------------------------------------
# _related_issues_tail — single-error note attach + multi-layer tail.
# ---------------------------------------------------------------------------


def test_related_issues_tail_none_for_single_layer() -> None:
    """When all findings carry the same ``layer:`` note (or none), no
    cross-layer tail is produced."""
    err = WorkflowUnreachableNode(file="wf.yaml", line=0, node="ghost")
    err.add_note("layer: structural")
    assert _related_issues_tail((err,)) is None


def test_related_issues_tail_summarizes_multiple_layers() -> None:
    """Two findings from different layers produce a ``Related issues``
    summary line naming both layers with counts."""
    a = WorkflowUnreachableNode(file="wf.yaml", line=0, node="x")
    a.add_note("layer: structural")
    b = WorkflowNodeNotDefined(file="wf.yaml", line=0, ref="y", location="nodes.z")
    b.add_note("layer: jinja_refs")
    tail = _related_issues_tail((a, b))
    assert tail is not None
    assert "structural (1)" in tail
    assert "jinja_refs (1)" in tail


def test_or_raise_single_error_attaches_tail_note() -> None:
    """``validate_workflow_or_raise`` raises the single error directly;
    a cross-layer tail (if any) is attached as a note. Here a single
    error means the tail is ``None`` and the error is raised bare."""
    wf = _wf(
        {
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "island": {"type": "kolsch/nodes/noop"},
            },
            "graph": {"edges": [{"from": "start", "to": "start"}]},
        }
    )
    with pytest.raises(WorkflowUnreachableNode):
        validate_workflow_or_raise(wf, AssetCatalog(), file="wf.yaml")


# ---------------------------------------------------------------------------
# _check_extends_policy — a policy-carrying extended workflow is rejected.
# ---------------------------------------------------------------------------


def test_extends_policy_forbidden_when_parent_has_policy(tmp_path: Path) -> None:
    """An extended workflow whose YAML declares non-empty ``policy:``
    rules is rejected with :class:`WorkflowExtendsPolicyForbidden`."""
    parent_path = tmp_path / "parent.yaml"
    parent_path.write_text(
        "nodes:\n"
        "  start:\n"
        "    type: kolsch/nodes/noop\n"
        "graph:\n"
        "  edges: []\n"
        "policy:\n"
        "  - allow: '*'\n",
        encoding="utf-8",
    )
    cat = AssetCatalog()
    ref = "demo/workflows/parent"
    cat.assets[ref] = AssetEntry(
        ref=ref,
        asset_type="workflow",
        bundle="demo",
        path=str(parent_path),
        metadata={},
    )
    wf = _wf(
        {
            "extends": [ref],
            "nodes": {"start": {"type": "kolsch/nodes/noop"}},
            "graph": {"edges": []},
        }
    )
    result = validate_workflow(wf, cat, file="child.yaml")
    assert any(isinstance(e, WorkflowExtendsPolicyForbidden) for e in result.errors)


def test_extends_unresolved_ref_is_skipped(tmp_path: Path) -> None:
    """An ``extends:`` ref not in the catalog is skipped by the policy
    layer (the resolver catches it) — no policy error is raised here."""
    _ = tmp_path
    wf = _wf(
        {
            "extends": ["demo/workflows/ghost"],
            "nodes": {"start": {"type": "kolsch/nodes/noop"}},
            "graph": {"edges": []},
        }
    )
    result = validate_workflow(wf, AssetCatalog(), file="child.yaml")
    assert not any(isinstance(e, WorkflowExtendsPolicyForbidden) for e in result.errors)


def test_extends_unreadable_parent_file_is_skipped(tmp_path: Path) -> None:
    """An ``extends:`` entry whose on-disk path can't be read (missing
    file) is defensively skipped by the policy layer — the resolver
    re-raises the real error later."""
    cat = AssetCatalog()
    ref = "demo/workflows/gone"
    cat.assets[ref] = AssetEntry(
        ref=ref,
        asset_type="workflow",
        bundle="demo",
        path=str(tmp_path / "does-not-exist.yaml"),
        metadata={},
    )
    wf = _wf(
        {
            "extends": [ref],
            "nodes": {"start": {"type": "kolsch/nodes/noop"}},
            "graph": {"edges": []},
        }
    )
    result = validate_workflow(wf, cat, file="child.yaml")
    assert not any(isinstance(e, WorkflowExtendsPolicyForbidden) for e in result.errors)


# ---------------------------------------------------------------------------
# _extract_nodes_refs — malformed template contributes no refs.
# ---------------------------------------------------------------------------


def test_extract_nodes_refs_malformed_yields_empty() -> None:
    """A template that fails to parse contributes no ``nodes.<X>.<Y>``
    pairs (the best-effort parse guard)."""
    assert _extract_nodes_refs("{{ nodes.x.y (unterminated") == []


def test_extract_nodes_refs_x_y_and_bare_forms() -> None:
    """Both ``nodes.<X>.<Y>`` and the bare ``nodes.<X>`` access forms
    are surfaced; the bare form is dropped when a fuller pair exists."""
    pairs = _extract_nodes_refs("{{ nodes.src.out }} {{ nodes.other }}")
    assert ("src", "out") in pairs
    assert ("other", None) in pairs


# ---------------------------------------------------------------------------
# _collect_parallel_branch_groups — node-library (graph=None) short-circuit.
# ---------------------------------------------------------------------------


def test_collect_parallel_branch_groups_node_library_is_empty() -> None:
    """A node library (``graph=None``) has no fan-out points — the
    decomposition returns the empty list."""
    wf = Workflow.model_validate(
        {"nodes": {"lonely": {"type": "kolsch/nodes/noop"}}, "graph": None}
    )
    assert _collect_parallel_branch_groups(wf) == []


def test_check_concurrency_invariants_node_library_auto_short_circuits() -> None:
    """``_check_concurrency_invariants`` on a node-library (``graph is
    None``) under ``auto`` mode returns before the cross-branch pass."""
    wf = Workflow.model_validate(
        {
            "concurrency": "auto",
            "nodes": {"lonely": {"type": "kolsch/nodes/noop"}},
            "graph": None,
        }
    )
    errors, warnings = _check_concurrency_invariants(
        workflow=wf, catalog=AssetCatalog(), file="wf.yaml"
    )
    assert errors == []
    assert warnings == []


def test_cross_branch_write_read_reverse_direction() -> None:
    """The write-read conflict is detected in BOTH scan directions: the
    alphabetically-earlier branch writes a key the later branch reads
    (the ``writes_a`` → ``reads_b`` arm)."""
    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                # 'aaa_writer' sorts before 'zzz_reader' so its branch
                # is scanned as the outer (writes_a) side.
                "aaa_writer": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"shared": "{{ 1 }}"},
                },
                "zzz_reader": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo {{ state.shared }}",
                },
                "join": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "start", "to": "aaa_writer"},
                    {"from": "start", "to": "zzz_reader"},
                    {"from": "aaa_writer", "to": "join"},
                    {"from": "zzz_reader", "to": "join"},
                ]
            },
        }
    )
    errors, _warnings = _check_concurrency_invariants(
        workflow=wf, catalog=AssetCatalog(), file="wf.yaml"
    )
    conflicts = [e for e in errors if isinstance(e, WorkflowParallelStateConflict)]
    assert len(conflicts) == 1
    assert conflicts[0].state_key == "shared"


def test_cross_branch_write_write_conflict_dedups() -> None:
    """Two sibling branches writing the SAME state key surface exactly
    one :class:`WorkflowParallelStateConflict` (the ``_record_conflict``
    dedup collapses the symmetric pair)."""
    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "left_w": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"k": "{{ 1 }}"},
                },
                "right_w": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"k": "{{ 2 }}"},
                },
                "join": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "start", "to": "left_w"},
                    {"from": "start", "to": "right_w"},
                    {"from": "left_w", "to": "join"},
                    {"from": "right_w", "to": "join"},
                ]
            },
        }
    )
    errors, _warnings = _check_concurrency_invariants(
        workflow=wf, catalog=AssetCatalog(), file="wf.yaml"
    )
    conflicts = [e for e in errors if isinstance(e, WorkflowParallelStateConflict)]
    assert len(conflicts) == 1
    assert conflicts[0].state_key == "k"


# ---------------------------------------------------------------------------
# Structural — copy_of source unreachable.
# ---------------------------------------------------------------------------


def test_copy_of_source_must_be_graph_reachable() -> None:
    """A ``copy_of:`` whose source node is not itself graph-reachable
    surfaces the source as :class:`WorkflowUnreachableNode`."""
    wf = _wf(
        {
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "detached": {"type": "kolsch/nodes/noop"},
                "clone": {"copy_of": "detached"},
            },
            "graph": {
                "edges": [
                    {"from": "start", "to": "clone"},
                ]
            },
        }
    )
    result = validate_workflow(wf, AssetCatalog(), file="wf.yaml")
    unreachable = [e for e in result.errors if isinstance(e, WorkflowUnreachableNode)]
    assert any(e.node == "detached" for e in unreachable), [e.node for e in unreachable]


# ---------------------------------------------------------------------------
# _check_value_for_refs — node-not-defined in config + bare pred access.
# ---------------------------------------------------------------------------


def test_node_config_ref_to_undefined_node_is_rejected() -> None:
    """A node config Jinja ref to a node that does not exist surfaces
    :class:`WorkflowNodeNotDefined` from the jinja-refs layer."""
    wf = _wf(
        {
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "user": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo {{ nodes.ghost.stdout }}",
                },
            },
            "graph": {"edges": [{"from": "start", "to": "user"}]},
        }
    )
    result = validate_workflow(wf, AssetCatalog(), file="wf.yaml")
    assert any(
        isinstance(e, WorkflowNodeNotDefined) and e.ref == "ghost"
        for e in result.errors
    ), [type(e).__name__ for e in result.errors]


def test_bare_nodes_pred_access_passes() -> None:
    """A bare ``nodes.<pred>`` access (no field traversal) on a valid
    predecessor is legal — the field check short-circuits (``y_field is
    None``) and contributes no error."""
    wf = _wf(
        {
            "nodes": {
                "src": {"type": "kolsch/nodes/noop"},
                "sink": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo {{ nodes.src }}",
                },
            },
            "graph": {"edges": [{"from": "src", "to": "sink"}]},
        }
    )
    result = validate_workflow(wf, AssetCatalog(), file="wf.yaml")
    assert result.is_valid, [type(e).__name__ for e in result.errors]


# ---------------------------------------------------------------------------
# _agent_output_field_set — graceful degradation paths.
# ---------------------------------------------------------------------------


def _agent_node(agent_ref: str) -> Any:
    wf = _wf(
        {
            "nodes": {"a": {"type": "kolsch/nodes/agent", "agent": agent_ref}},
            "graph": {"edges": []},
        }
    )
    return wf.nodes["a"]


def test_agent_output_field_set_no_catalog_returns_none() -> None:
    """With no catalog, an agent node's output schema is unknown."""
    node = _agent_node("demo/agents/x")
    assert _agent_output_field_set(node, catalog=None) is None


def test_agent_output_field_set_catalog_miss_returns_none() -> None:
    """A catalog miss on the agent ref degrades to open (``None``)."""
    node = _agent_node("demo/agents/missing")
    assert _agent_output_field_set(node, catalog=AssetCatalog()) is None


def test_agent_output_field_set_non_mapping_io_returns_none(tmp_path: Path) -> None:
    """An agent YAML whose ``io:`` block is not a mapping degrades to
    open (``None``) rather than crashing."""
    agent_path = tmp_path / "weird.yaml"
    agent_path.write_text("io: not-a-mapping\n", encoding="utf-8")
    cat = AssetCatalog()
    ref = "demo/agents/weird"
    cat.assets[ref] = AssetEntry(
        ref=ref,
        asset_type="agent",
        bundle="demo",
        path=str(agent_path),
        metadata={},
    )
    node = _agent_node(ref)
    assert _agent_output_field_set(node, catalog=cat) is None


def test_agent_output_field_set_non_mapping_outputs_returns_none(
    tmp_path: Path,
) -> None:
    """An agent YAML whose ``io.outputs`` is not a mapping degrades to
    open (``None``)."""
    agent_path = tmp_path / "listout.yaml"
    agent_path.write_text(
        "io:\n  outputs:\n    - a\n    - b\n",
        encoding="utf-8",
    )
    cat = AssetCatalog()
    ref = "demo/agents/listout"
    cat.assets[ref] = AssetEntry(
        ref=ref,
        asset_type="agent",
        bundle="demo",
        path=str(agent_path),
        metadata={},
    )
    node = _agent_node(ref)
    assert _agent_output_field_set(node, catalog=cat) is None


def test_agent_output_field_set_reads_declared_outputs(tmp_path: Path) -> None:
    """A well-formed agent YAML surfaces its declared ``io.outputs``
    keys as the field set."""
    agent_path = tmp_path / "good.yaml"
    agent_path.write_text(
        "io:\n  outputs:\n    text:\n      type: str\n    score:\n      type: int\n",
        encoding="utf-8",
    )
    cat = AssetCatalog()
    ref = "demo/agents/good"
    cat.assets[ref] = AssetEntry(
        ref=ref,
        asset_type="agent",
        bundle="demo",
        path=str(agent_path),
        metadata={},
    )
    node = _agent_node(ref)
    assert _agent_output_field_set(node, catalog=cat) == frozenset({"text", "score"})


# ---------------------------------------------------------------------------
# Catalog-node signature modes not covered by the existing suite.
# ---------------------------------------------------------------------------


def _seed_node(
    *, tmp_path: Path, name: str, source: str
) -> tuple[AssetCatalog, Workflow]:
    node_file = tmp_path / f"{name}.py"
    node_file.write_text(source, encoding="utf-8")
    cat = AssetCatalog()
    ref = f"acme/nodes/{name}"
    cat.assets[ref] = AssetEntry(
        ref=ref,
        asset_type="node",
        bundle="acme",
        path=str(node_file),
        metadata={},
    )
    wf = _wf({"nodes": {"n": {"type": ref}}, "graph": {"edges": []}})
    return cat, wf


def test_catalog_node_keyword_only_param_rejected(tmp_path: Path) -> None:
    """A ``run`` with a keyword-only parameter (after ``*``) is not
    positional — rejected with :class:`WorkflowNodeSignatureInvalid`."""
    cat, wf = _seed_node(
        tmp_path=tmp_path,
        name="kwonly",
        source=(
            "from pydantic import BaseModel\n"
            "from workflows import node, NodeContext\n"
            "class I(BaseModel):\n    msg: str\n"
            "@node()\n"
            "def run(i: I, *, ctx: NodeContext) -> I:\n"
            "    return i\n"
        ),
    )
    result = validate_workflow(wf, cat, file="wf.yaml")
    assert any(isinstance(e, WorkflowNodeSignatureInvalid) for e in result.errors)


def test_catalog_node_param1_not_basemodel_rejected(tmp_path: Path) -> None:
    """``run``'s first param annotated with a non-``BaseModel`` type is
    rejected."""
    cat, wf = _seed_node(
        tmp_path=tmp_path,
        name="badp1",
        source=(
            "from workflows import node, NodeContext\n"
            "@node()\n"
            "def run(i: str, ctx: NodeContext) -> bool:\n"
            "    return True\n"
        ),
    )
    result = validate_workflow(wf, cat, file="wf.yaml")
    assert any(
        isinstance(e, WorkflowNodeSignatureInvalid) and "param 1" in (e.reason or "")
        for e in result.errors
    )


def test_catalog_node_bad_param3_rejected(tmp_path: Path) -> None:
    """A three-param ``run`` whose optional param-3 is not a
    ``BaseModel`` subclass is rejected."""
    cat, wf = _seed_node(
        tmp_path=tmp_path,
        name="badp3",
        source=(
            "from pydantic import BaseModel\n"
            "from workflows import node, NodeContext\n"
            "class I(BaseModel):\n    msg: str\n"
            "@node()\n"
            "def run(i: I, ctx: NodeContext, config: str) -> I:\n"
            "    return i\n"
        ),
    )
    result = validate_workflow(wf, cat, file="wf.yaml")
    assert any(
        isinstance(e, WorkflowNodeSignatureInvalid) and "param 3" in (e.reason or "")
        for e in result.errors
    )


def test_catalog_node_missing_return_annotation_rejected(tmp_path: Path) -> None:
    """A ``run`` with no return annotation is rejected with a
    return-type finding (``<missing>``)."""
    from workflows.errors import WorkflowNodeReturnTypeInvalid

    cat, wf = _seed_node(
        tmp_path=tmp_path,
        name="noret",
        source=(
            "from pydantic import BaseModel\n"
            "from workflows import node, NodeContext\n"
            "class I(BaseModel):\n    msg: str\n"
            "@node()\n"
            "def run(i: I, ctx: NodeContext):\n"
            "    return None\n"
        ),
    )
    result = validate_workflow(wf, cat, file="wf.yaml")
    assert any(isinstance(e, WorkflowNodeReturnTypeInvalid) for e in result.errors)


# ---------------------------------------------------------------------------
# Concurrency invariants — sequential map recursion + read-set + dedup.
# ---------------------------------------------------------------------------


def test_branch_write_set_recurses_sequential_map_worker() -> None:
    """A sequential map (``parallel: false``) contributes its worker's
    ``set_state`` writes transitively to the branch write set."""
    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "m": {
                    "type": "kolsch/nodes/map",
                    "over": "{{ items }}",
                    "apply": "worker",
                    "item_as": "it",
                    "parallel": False,
                },
                "worker": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"counter": "{{ 1 }}"},
                },
            },
            "graph": {"edges": []},
        }
    )
    writes = _branch_state_write_set(wf, {"m"})
    assert "counter" in writes
    assert writes["counter"] == "worker"


def test_branch_read_set_recurses_map_and_scans_list_dict() -> None:
    """The read-set scanner recurses sequential map workers and walks
    nested list/dict Jinja fields for ``state.<key>`` references."""
    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "m": {
                    "type": "kolsch/nodes/map",
                    "over": "{{ state.source_list }}",
                    "apply": "worker",
                    "item_as": "it",
                    "parallel": False,
                },
                "worker": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo {{ state.inner_key }}",
                },
            },
            "graph": {"edges": []},
        }
    )
    reads = _branch_state_read_set(wf, {"m"})
    # `over:` on the map references state.source_list; the worker's
    # command references state.inner_key.
    assert "source_list" in reads
    assert "inner_key" in reads


def test_walk_worker_descendants_recurses_nested_map() -> None:
    """``_walk_worker_descendants`` recurses through nested map workers
    but treats sub-workflow refs as opaque."""
    wf = _wf(
        {
            "nodes": {
                "outer": {
                    "type": "kolsch/nodes/map",
                    "over": "{{ items }}",
                    "apply": "inner",
                    "item_as": "it",
                },
                "inner": {
                    "type": "kolsch/nodes/map",
                    "over": "{{ sub }}",
                    "apply": "leaf",
                    "item_as": "jt",
                },
                "leaf": {"type": "kolsch/nodes/noop"},
            },
            "graph": {"edges": []},
        }
    )
    descendants = _walk_worker_descendants(wf, "outer")
    assert descendants == ["outer", "inner", "leaf"]


def test_cross_branch_write_read_conflict_detected() -> None:
    """Two sibling parallel branches where one writes ``state.k`` and
    the other reads it surface a :class:`WorkflowParallelStateConflict`
    under ``auto`` mode (the write-read pass)."""
    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "writer": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"shared": "{{ 1 }}"},
                },
                "reader": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo {{ state.shared }}",
                },
                "join": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "start", "to": "writer"},
                    {"from": "start", "to": "reader"},
                    {"from": "writer", "to": "join"},
                    {"from": "reader", "to": "join"},
                ]
            },
        }
    )
    errors, _warnings = _check_concurrency_invariants(
        workflow=wf, catalog=AssetCatalog(), file="wf.yaml"
    )
    conflicts = [e for e in errors if isinstance(e, WorkflowParallelStateConflict)]
    assert len(conflicts) >= 1
    assert conflicts[0].state_key == "shared"


def test_sequential_mode_skips_cross_branch_pass() -> None:
    """A ``sequential``-mode workflow skips the cross-branch conflict
    pass entirely — no conflict is raised even when siblings share a
    state key (they never run concurrently)."""
    wf = _wf(
        {
            "concurrency": "sequential",
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "writer": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"shared": "{{ 1 }}"},
                },
                "reader": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo {{ state.shared }}",
                },
                "join": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "start", "to": "writer"},
                    {"from": "start", "to": "reader"},
                    {"from": "writer", "to": "join"},
                    {"from": "reader", "to": "join"},
                ]
            },
        }
    )
    errors, _warnings = _check_concurrency_invariants(
        workflow=wf, catalog=AssetCatalog(), file="wf.yaml"
    )
    assert not any(isinstance(e, WorkflowParallelStateConflict) for e in errors)

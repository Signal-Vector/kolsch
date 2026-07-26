"""qol-097 — pure-function tests for ``effective_required_inputs``.

Covers Tests 3-7 of the spec: promotion from unbound children,
edge-bind coverage, direct+promoted union, DAG re-convergence memo
soundness, and cycle-safe bounded recursion via the ``_probe``
counter.

The function is a pure computation over a parsed ``Workflow`` + an
``AssetCatalog``; these tests write child yaml files under
``tmp_path/bundles/<bundle>/workflows/<name>.yaml`` and seed the
catalog with matching entries so ``catalog.lookup(ref)`` resolves
just like production.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from assets.catalog import AssetCatalog, AssetEntry
from workflows.inputs_scan import effective_required_inputs
from workflows.parser import load_workflow

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _entry(ref: str, path: Path) -> AssetEntry:
    return AssetEntry(
        ref=ref,
        asset_type="workflow",
        bundle=ref.split("/", 1)[0],
        path=str(path),
    )


# ---------------------------------------------------------------------------
# Test 3 — promotion from an unbound sub-workflow child.
# ---------------------------------------------------------------------------


def test_promotion_from_unbound_child(tmp_path: Path) -> None:
    """Parent with a sub-workflow node (no inbound edges) promotes ``url``."""
    parent_yaml = """
    nodes:
      sub:
        type: kolsch/nodes/workflow
        ref: demo/workflows/child
    graph:
      edges: []
    """
    child_yaml = """
    nodes:
      fetch:
        type: kolsch/nodes/bash
        command: "curl {{ inputs.url }}"
    graph:
      edges: []
    """
    parent_path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
    child_path = tmp_path / "bundles" / "demo" / "workflows" / "child.yaml"
    _write(parent_path, parent_yaml)
    _write(child_path, child_yaml)
    catalog = AssetCatalog(
        assets={
            "demo/workflows/parent": _entry("demo/workflows/parent", parent_path),
            "demo/workflows/child": _entry("demo/workflows/child", child_path),
        }
    )
    parent_wf = load_workflow(parent_path)
    result = effective_required_inputs(
        parent_wf,
        catalog,
        source=parent_path.read_text(encoding="utf-8"),
        ref="demo/workflows/parent",
    )
    assert result == ["url"]


# ---------------------------------------------------------------------------
# Test 4 — edge bind covers child requirement (not promoted).
# ---------------------------------------------------------------------------


def test_edge_bind_covers_child_requirement(tmp_path: Path) -> None:
    """Inbound edge ``binds: {url: ...}`` masks the child's ``url`` requirement."""
    parent_yaml = """
    nodes:
      ready:
        type: kolsch/nodes/noop
      sub:
        type: kolsch/nodes/workflow
        ref: demo/workflows/child
    graph:
      edges:
        - from: ready
          to: sub
          binds:
            url: "{{ inputs.something_else }}"
    """
    child_yaml = """
    nodes:
      fetch:
        type: kolsch/nodes/bash
        command: "curl {{ inputs.url }}"
    graph:
      edges: []
    """
    parent_path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
    child_path = tmp_path / "bundles" / "demo" / "workflows" / "child.yaml"
    _write(parent_path, parent_yaml)
    _write(child_path, child_yaml)
    catalog = AssetCatalog(
        assets={
            "demo/workflows/parent": _entry("demo/workflows/parent", parent_path),
            "demo/workflows/child": _entry("demo/workflows/child", child_path),
        }
    )
    parent_wf = load_workflow(parent_path)
    result = effective_required_inputs(
        parent_wf,
        catalog,
        source=parent_path.read_text(encoding="utf-8"),
        ref="demo/workflows/parent",
    )
    # `url` is covered by the inbound edge bind; the bind's rhs
    # references `inputs.something_else` at the parent level, so
    # `something_else` gets promoted via the parent's own direct scan.
    assert result == ["something_else"]


# ---------------------------------------------------------------------------
# Test 5 — direct + promoted union.
# ---------------------------------------------------------------------------


def test_merges_direct_and_promoted(tmp_path: Path) -> None:
    """Parent's own ``inputs.a`` union child's promoted ``inputs.b`` → ``[a, b]``."""
    parent_yaml = """
    nodes:
      greet:
        type: kolsch/nodes/bash
        command: "echo {{ inputs.a }}"
      sub:
        type: kolsch/nodes/workflow
        ref: demo/workflows/child
    graph:
      edges: []
    """
    child_yaml = """
    nodes:
      fetch:
        type: kolsch/nodes/bash
        command: "echo {{ inputs.b }}"
    graph:
      edges: []
    """
    parent_path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
    child_path = tmp_path / "bundles" / "demo" / "workflows" / "child.yaml"
    _write(parent_path, parent_yaml)
    _write(child_path, child_yaml)
    catalog = AssetCatalog(
        assets={
            "demo/workflows/parent": _entry("demo/workflows/parent", parent_path),
            "demo/workflows/child": _entry("demo/workflows/child", child_path),
        }
    )
    parent_wf = load_workflow(parent_path)
    result = effective_required_inputs(
        parent_wf,
        catalog,
        source=parent_path.read_text(encoding="utf-8"),
        ref="demo/workflows/parent",
    )
    assert result == ["a", "b"]


# ---------------------------------------------------------------------------
# Test 6 — sibling nodes to same child memoize (DAG re-convergence).
# ---------------------------------------------------------------------------


def test_sibling_children_same_ref_memoize(tmp_path: Path) -> None:
    """Two sibling sub-workflow nodes → C; N1 covers ``foo``, N2 doesn't."""
    parent_yaml = """
    nodes:
      ready:
        type: kolsch/nodes/noop
      n1:
        type: kolsch/nodes/workflow
        ref: demo/workflows/c
      n2:
        type: kolsch/nodes/workflow
        ref: demo/workflows/c
    graph:
      edges:
        - from: ready
          to: n1
          binds:
            foo: "covered-by-n1"
    """
    child_yaml = """
    nodes:
      use:
        type: kolsch/nodes/bash
        command: "echo {{ inputs.foo }}"
    graph:
      edges: []
    """
    parent_path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
    child_path = tmp_path / "bundles" / "demo" / "workflows" / "c.yaml"
    _write(parent_path, parent_yaml)
    _write(child_path, child_yaml)
    catalog = AssetCatalog(
        assets={
            "demo/workflows/parent": _entry("demo/workflows/parent", parent_path),
            "demo/workflows/c": _entry("demo/workflows/c", child_path),
        }
    )
    parent_wf = load_workflow(parent_path)
    probe: list[int] = []
    result = effective_required_inputs(
        parent_wf,
        catalog,
        source=parent_path.read_text(encoding="utf-8"),
        ref="demo/workflows/parent",
        _probe=probe,
    )
    # Soundness: N2's descent is not skipped by a shared `_seen` bug —
    # it correctly sees C's full requirement set (memoized), then
    # subtracts N2's edge binds (empty) → `foo` promotes.
    assert "foo" in result
    # Memoization proof: C is loaded exactly ONCE across both siblings.
    assert len(probe) == 1, f"expected 1 child load, got {probe}"


# ---------------------------------------------------------------------------
# Test 7 — cycle-safe bounded recursion (A → B → A).
# ---------------------------------------------------------------------------


def test_cycle_safe_bounded_recursion(tmp_path: Path) -> None:
    """A calls B, B calls A — bounded via ``in_progress``; probe count = 2."""
    a_yaml = """
    nodes:
      call_b:
        type: kolsch/nodes/workflow
        ref: demo/workflows/b
    graph:
      edges: []
    """
    b_yaml = """
    nodes:
      call_a:
        type: kolsch/nodes/workflow
        ref: demo/workflows/a
    graph:
      edges: []
    """
    a_path = tmp_path / "bundles" / "demo" / "workflows" / "a.yaml"
    b_path = tmp_path / "bundles" / "demo" / "workflows" / "b.yaml"
    _write(a_path, a_yaml)
    _write(b_path, b_yaml)
    catalog = AssetCatalog(
        assets={
            "demo/workflows/a": _entry("demo/workflows/a", a_path),
            "demo/workflows/b": _entry("demo/workflows/b", b_path),
        }
    )
    a_wf = load_workflow(a_path)
    probe: list[int] = []
    result = effective_required_inputs(
        a_wf,
        catalog,
        source=a_path.read_text(encoding="utf-8"),
        ref="demo/workflows/a",
        _probe=probe,
    )
    # A visits B (probe += 1); B's descent tries A which enters
    # `in_progress` there and DOES get loaded → probe += 1; the load
    # of A then attempts B again, which IS in in_progress, so cycle
    # guard fires and returns set() — no further loads. Total: 2.
    assert len(probe) == 2, f"expected 2 child loads under cycle, got {probe}"
    # Neither workflow has any inputs; the result is an empty list.
    assert result == []

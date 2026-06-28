"""Phase 3.10 Step 0 — parser field extensions.

Covers the parser-shape contract for the Phase 3.10 fields per
build doc ACs 0.1 + 0.2:

- ``Workflow.concurrency`` defaults to ``"sequential"``; ``"auto"``
  parses; any other value raises Pydantic ``ValidationError``.
- ``MapNodeDef.parallel`` defaults to ``False``;
  ``MapNodeDef.concurrency`` defaults to ``None``.
- ``MapNodeDef`` with ``parallel: true, concurrency: 5`` parses.
- ``MapNodeDef`` with ``parallel: true, concurrency: -1`` PARSES
  (Step 1's run-start gate raises
  :class:`WorkflowInvalidConcurrency`, not the parser).

These fields are SHAPE-ONLY at Step 0; no runtime behavior change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from workflows.parser import MapNodeDef, Workflow, load_workflow


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC 0.1 — Workflow.concurrency field
# ---------------------------------------------------------------------------


def test_workflow_concurrency_defaults_to_sequential(tmp_path: Path) -> None:
    """A workflow with no ``concurrency:`` key defaults to ``sequential``."""
    yaml_text = """
nodes:
  n: { type: kolsch/nodes/noop }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.concurrency == "sequential"


def test_workflow_concurrency_auto_parses(tmp_path: Path) -> None:
    yaml_text = """
concurrency: auto
nodes:
  n: { type: kolsch/nodes/noop }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.concurrency == "auto"


def test_workflow_concurrency_sequential_parses_explicitly(tmp_path: Path) -> None:
    yaml_text = """
concurrency: sequential
nodes:
  n: { type: kolsch/nodes/noop }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.concurrency == "sequential"


@pytest.mark.parametrize(
    "bad_value",
    ["foo", "Auto", "SEQUENTIAL", "parallel", "true", "1", ""],
)
def test_workflow_concurrency_unknown_value_rejected(
    tmp_path: Path, bad_value: str
) -> None:
    """The ``Literal["sequential", "auto"]`` typing rejects any other string
    via Pydantic's standard validation path. ``load_workflow`` wraps
    the underlying ``ValidationError`` in a ``WorkflowValidationError``.
    """
    from workflows.errors import WorkflowValidationError

    yaml_text = f"""
concurrency: {bad_value!r}
nodes:
  n: {{ type: kolsch/nodes/noop }}
"""
    with pytest.raises(WorkflowValidationError):
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))


def test_workflow_concurrency_unknown_value_via_direct_model() -> None:
    """Programmatic construction also rejects unknown values."""
    with pytest.raises(ValidationError):
        Workflow.model_validate({"concurrency": "parallel", "nodes": {}})


# ---------------------------------------------------------------------------
# AC 0.2 — MapNodeDef.parallel + concurrency
# ---------------------------------------------------------------------------


def test_map_node_def_defaults(tmp_path: Path) -> None:
    """A map without ``parallel:`` / ``concurrency:`` defaults to the
    Phase 3.7 sequential contract."""
    yaml_text = """
nodes:
  fan:
    type: kolsch/nodes/map
    apply: worker
    over: "{{ items }}"
    item_as: row
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    node = wf.nodes["fan"]
    assert isinstance(node, MapNodeDef)
    assert node.parallel is False
    assert node.concurrency is None


def test_map_node_def_parallel_with_concurrency(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  fan:
    type: kolsch/nodes/map
    apply: worker
    over: "{{ items }}"
    item_as: row
    parallel: true
    concurrency: 5
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    node = wf.nodes["fan"]
    assert isinstance(node, MapNodeDef)
    assert node.parallel is True
    assert node.concurrency == 5


def test_map_node_def_parallel_unbounded_parses(tmp_path: Path) -> None:
    """``parallel: true`` with no ``concurrency:`` means unbounded
    parallelism (the spec §16.3 default)."""
    yaml_text = """
nodes:
  fan:
    type: kolsch/nodes/map
    apply: worker
    over: "{{ items }}"
    item_as: row
    parallel: true
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    node = wf.nodes["fan"]
    assert isinstance(node, MapNodeDef)
    assert node.parallel is True
    assert node.concurrency is None


def test_map_node_def_negative_concurrency_parses(tmp_path: Path) -> None:
    """The parser is SHAPE-ONLY at Step 0 — ``concurrency: -1`` flows
    through and is rejected later by Step 1's run-start gate as
    :class:`WorkflowInvalidConcurrency`. Mirrors the pattern at
    :func:`test_top_level_timeout_seconds_negative_parses_but_step_5_rejects`.
    """
    yaml_text = """
nodes:
  fan:
    type: kolsch/nodes/map
    apply: worker
    over: "{{ items }}"
    item_as: row
    parallel: true
    concurrency: -1
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    node = wf.nodes["fan"]
    assert isinstance(node, MapNodeDef)
    assert node.parallel is True
    assert node.concurrency == -1


def test_map_node_def_zero_concurrency_parses(tmp_path: Path) -> None:
    """Same as the -1 case — Step 1's gate handles the positivity check."""
    yaml_text = """
nodes:
  fan:
    type: kolsch/nodes/map
    apply: worker
    over: "{{ items }}"
    item_as: row
    parallel: true
    concurrency: 0
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    node = wf.nodes["fan"]
    assert isinstance(node, MapNodeDef)
    assert node.concurrency == 0


def test_map_node_def_concurrency_non_int_rejected(tmp_path: Path) -> None:
    """``concurrency:`` is ``int | None`` — a non-int string raises
    Pydantic at parse."""
    from workflows.errors import WorkflowValidationError

    yaml_text = """
nodes:
  fan:
    type: kolsch/nodes/map
    apply: worker
    over: "{{ items }}"
    item_as: row
    parallel: true
    concurrency: "five"
"""
    with pytest.raises(WorkflowValidationError):
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))


# ---------------------------------------------------------------------------
# AC 0.5 — No runtime behavior change yet.
# ---------------------------------------------------------------------------


def test_executor_has_no_concurrency_references() -> None:
    """AC 0.5 — Step 0 ships parser + errors only. The executor must
    not yet reference the new fields; Step 3 wires runtime."""
    src = (
        Path(__file__).resolve().parent.parent.parent / "src" / "workflows"
    )
    text = (src / "executor.py").read_text(encoding="utf-8")
    # Allow the existing 'concurrent' word used in docstrings about
    # Phase 3.4 pause/resume; the field name `self._workflow.concurrency`
    # would be the smoking gun.
    assert "self._workflow.concurrency" not in text
    assert "_continue_worklist_concurrent" not in text


def test_map_runtime_has_no_parallel_references() -> None:
    """AC 0.5 — the map runtime must not yet reference ``parallel``
    or ``concurrency`` fields."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "workflows"
        / "builtins"
        / "map.py"
    )
    text = src.read_text(encoding="utf-8")
    assert "node_def.parallel" not in text
    assert "node_def.concurrency" not in text
    assert "_run_parallel" not in text

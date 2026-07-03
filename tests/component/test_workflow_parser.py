"""Phase 3.0 Step 2 — workflow YAML parser component tests.

Covers the parser's shape validation per Step 2 ACs 2.1-2.17:

- All 14 built-in node types parse with `kolsch/nodes/<name>` refs.
- `CatalogNodeDef` parses for authored Python nodes (any other 3-seg).
- Non-3-seg `type:` raises `WorkflowInvalidRef`.
- Reserved/built-in collisions, `core` bundle name, and bundle-name
  reservations are all NON-events (master spec §4.0.1).
- `copy_of:` works; `instance_of:` is rejected.
- Edge-level `when:` works; edge-level `condition:` / `ref:` rejected.
- Top-level `policy:` works; `permissions:` rejected.
- Top-level + per-node `timeout_seconds:` parse (sign-check is Step 5).
- `description:` (min_length=1) accepted on all NodeDef types.
- Multi-input edges parse cleanly.
- YAML loaded via `_yamlio.load` (ruamel under the hood).
- Per-built-in `Inputs(BaseModel)` classes declared per the locked
  Round 5 H8 table.

Step 2 explicitly EXCLUDES semantic validation — DAG, predecessors,
bind-collisions, timeout sign, branching arms all land in Step 5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from workflows.catalog_node import BUILTIN_TYPE_NAMES, CatalogNodeDef
from workflows.errors import WorkflowInvalidRef, WorkflowValidationError
from workflows.parser import (
    AgentInputs,
    AgentNodeDef,
    BashInputs,
    BashNodeDef,
    BranchInputs,
    BranchNodeDef,
    BrowseUrlNodeDef,
    ComputeInputs,
    ComputeNodeDef,
    CopyOfNodeDef,
    DeleteStateInputs,
    DeleteStateNodeDef,
    FilterInputs,
    FilterNodeDef,
    ListFilesInputs,
    ListFilesNodeDef,
    MapInputs,
    MapNodeDef,
    NoopInputs,
    NoopNodeDef,
    ReadFileInputs,
    ReadFileNodeDef,
    SetStateInputs,
    SetStateNodeDef,
    UserInputInputs,
    UserInputNodeDef,
    WorkflowNodeDef,
    WorkflowSubInputs,
    WriteFileInputs,
    WriteFileNodeDef,
    load_workflow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC 2.1 — Workflow top-level shape
# ---------------------------------------------------------------------------


def test_minimal_runnable_workflow_parses(tmp_path: Path) -> None:
    """Master spec §6.0 — single-node + empty edges = runnable."""
    yaml_text = """
nodes:
  greet:
    type: kolsch/nodes/bash
    command: echo hello
graph:
  edges: []
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.kind == "workflow"
    assert isinstance(wf.nodes["greet"], BashNodeDef)
    assert wf.nodes["greet"].command == "echo hello"
    assert wf.graph is not None
    assert wf.graph.edges == []


def test_workflow_top_level_defaults_apply(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  greet:
    type: kolsch/nodes/noop
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.description == ""
    assert wf.extends == []
    assert wf.policy == []
    assert wf.timeout_seconds is None
    assert wf.graph is None


# ---------------------------------------------------------------------------
# AC 2.2 — EXACTLY 14 built-in NodeDef variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_ref", "extra", "expected_cls"),
    [
        ("kolsch/nodes/bash", {"command": "ls"}, BashNodeDef),
        (
            "kolsch/nodes/browse_url",
            {"url": "https://example.com"},
            BrowseUrlNodeDef,
        ),
        ("kolsch/nodes/agent", {"agent": "acme/agents/bot"}, AgentNodeDef),
        ("kolsch/nodes/workflow", {"ref": "acme/workflows/triage"}, WorkflowNodeDef),
        ("kolsch/nodes/noop", {}, NoopNodeDef),
        ("kolsch/nodes/read_file", {"path": "x.txt"}, ReadFileNodeDef),
        (
            "kolsch/nodes/write_file",
            {"path": "x.txt", "content": "hi"},
            WriteFileNodeDef,
        ),
        ("kolsch/nodes/list_files", {"path": "."}, ListFilesNodeDef),
        ("kolsch/nodes/set_state", {"keys": {"a": 1}}, SetStateNodeDef),
        ("kolsch/nodes/delete_state", {"keys": ["a"]}, DeleteStateNodeDef),
        (
            "kolsch/nodes/filter",
            {"over": "{{ items }}", "condition": "{{ item.active }}"},
            FilterNodeDef,
        ),
        (
            "kolsch/nodes/map",
            {"apply": "worker", "over": "{{ items }}", "item_as": "row"},
            MapNodeDef,
        ),
        ("kolsch/nodes/branch", {"condition": "{{ ok }}"}, BranchNodeDef),
        ("kolsch/nodes/compute", {"value": "{{ items | length }}"}, ComputeNodeDef),
        (
            "kolsch/nodes/user_input",
            {"prompt": "what?"},
            UserInputNodeDef,
        ),
    ],
)
def test_all_15_builtin_types_parse(
    tmp_path: Path,
    type_ref: str,
    extra: dict[str, Any],
    expected_cls: type,
) -> None:
    """Every built-in slot parses into the matching *NodeDef class."""
    body = {"type": type_ref, **extra}
    payload = {"nodes": {"n": body}}
    yaml_text = _to_yaml({"nodes": {"n": body}})
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert isinstance(wf.nodes["n"], expected_cls)
    assert payload["nodes"]["n"]["type"] == type_ref  # sanity


def test_builtin_type_name_set_size() -> None:
    """AC sanity — the 15-name set is EXACTLY 15 entries."""
    assert len(BUILTIN_TYPE_NAMES) == 15
    expected = {
        "agent",
        "bash",
        "branch",
        "browse_url",
        "compute",
        "delete_state",
        "filter",
        "list_files",
        "map",
        "noop",
        "read_file",
        "set_state",
        "user_input",
        "workflow",
        "write_file",
    }
    assert expected == BUILTIN_TYPE_NAMES


def test_no_python_or_aggregation_variants_in_parser() -> None:
    """Grep the parser module for any leaked legacy variants."""
    src = Path(__file__).resolve().parent.parent.parent / "src"
    text = (src / "workflows" / "parser.py").read_text(encoding="utf-8")
    assert "PythonNodeDef" not in text
    for old in ("SumNodeDef", "CountNodeDef", "MinNodeDef", "MaxNodeDef"):
        assert old not in text
    for old in ("SortNodeDef", "FlattenNodeDef", "AnyNodeDef", "AllNodeDef"):
        assert old not in text


# ---------------------------------------------------------------------------
# AC 2.3 — Non-3-seg `type:` rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_type",
    [
        "bash",  # bare 1-seg (legacy shorthand)
        "sum",  # 1-seg (dropped aggregation)
        "python",  # 1-seg (dropped discriminator)
        "kolsch/bash",  # 2-seg
        "kolsch/nodes/bash/extra",  # 4-seg
    ],
)
def test_non_3_seg_type_raises_invalid_ref(tmp_path: Path, bad_type: str) -> None:
    yaml_text = f"""
nodes:
  n:
    type: {bad_type}
"""
    with pytest.raises(WorkflowInvalidRef) as exc_info:
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert bad_type in str(exc_info.value)


def test_unknown_3_seg_lands_in_catalog_node_def(tmp_path: Path) -> None:
    """Any 3-seg ref that isn't a reserved built-in slot parses as
    :class:`CatalogNodeDef` (Round 4 item #3 — authored Python node)."""
    yaml_text = """
nodes:
  n:
    type: acme/nodes/echo
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert isinstance(wf.nodes["n"], CatalogNodeDef)
    assert wf.nodes["n"].type == "acme/nodes/echo"


def test_bundle_name_reservation_removed(tmp_path: Path) -> None:
    """R3 #5 — bundle named ``bash`` is fine; `core` is fine too."""
    yaml_text = """
nodes:
  a:
    type: bash/nodes/parse
  b:
    type: core/nodes/echo
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert isinstance(wf.nodes["a"], CatalogNodeDef)
    assert isinstance(wf.nodes["b"], CatalogNodeDef)


def test_catalog_node_def_module_location() -> None:
    """Round 4 item #2 — :class:`CatalogNodeDef` lives in
    ``src/workflows/catalog_node.py``, NOT in ``parser.py``."""
    assert CatalogNodeDef.__module__ == "workflows.catalog_node"


def test_no_catalog_node_ref_survivors_in_workflows() -> None:
    """Round 4 item #3 — the class was renamed; no legacy survivors."""
    src = Path(__file__).resolve().parent.parent.parent / "src" / "workflows"
    for py in src.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "CatalogNodeRef" not in text, py


# ---------------------------------------------------------------------------
# AC 2.4 — copy_of: vs instance_of:
# ---------------------------------------------------------------------------


def test_copy_of_parses_without_type(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  parent:
    type: kolsch/nodes/bash
    command: echo a
  child:
    copy_of: parent
    command: echo b
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert isinstance(wf.nodes["parent"], BashNodeDef)
    assert isinstance(wf.nodes["child"], CopyOfNodeDef)
    assert wf.nodes["child"].copy_of == "parent"
    # Override fields are stored as extras (semantic resolution at Step 5).
    assert getattr(wf.nodes["child"], "command", None) == "echo b"


def test_instance_of_rejected_with_fix_path(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n:
    instance_of: parent
"""
    with pytest.raises(WorkflowValidationError) as exc_info:
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert "instance_of" in str(exc_info.value)
    assert "copy_of" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC 2.5 — EdgeDef shape (Round 4 item #11)
# ---------------------------------------------------------------------------


def test_edge_parses_with_from_to_binds(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  a: { type: kolsch/nodes/noop }
  b: { type: kolsch/nodes/noop }
graph:
  edges:
    - from: a
      to: b
      binds: { count: 5 }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.graph is not None
    edge = wf.graph.edges[0]
    assert edge.from_ == "a"
    assert edge.to == "b"
    assert edge.binds == {"count": 5}
    assert edge.branch is None
    assert edge.when is None


def test_edge_branch_field_parses(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  a: { type: kolsch/nodes/branch, condition: "{{ ok }}" }
  b: { type: kolsch/nodes/noop }
  c: { type: kolsch/nodes/noop }
graph:
  edges:
    - { from: a, to: b, branch: true }
    - { from: a, to: c, branch: false }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.graph is not None
    assert wf.graph.edges[0].branch is True
    assert wf.graph.edges[1].branch is False


def test_edge_level_condition_rejected_with_fix_path(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  a: { type: kolsch/nodes/noop }
  b: { type: kolsch/nodes/noop }
graph:
  edges:
    - from: a
      to: b
      condition: "{{ ok }}"
"""
    with pytest.raises(WorkflowValidationError) as exc_info:
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert "condition" in str(exc_info.value)
    assert "when" in str(exc_info.value)


def test_edge_ref_field_rejected_with_fix_path(tmp_path: Path) -> None:
    """Round 4 item #11 — ``ref:`` is gone; multi-input edges are
    the fan-in mechanism."""
    yaml_text = """
nodes:
  a: { type: kolsch/nodes/noop }
  b: { type: kolsch/nodes/noop }
graph:
  edges:
    - from: a
      to: b
      ref: [c, d]
"""
    with pytest.raises(WorkflowValidationError) as exc_info:
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert "ref" in str(exc_info.value)
    assert "multi-input" in str(exc_info.value) or "fan-in" in str(exc_info.value)


def test_edge_when_parses(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  a: { type: kolsch/nodes/noop }
  b: { type: kolsch/nodes/noop }
graph:
  edges:
    - from: a
      to: b
      when: "{{ nodes.a.success }}"
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.graph is not None
    assert wf.graph.edges[0].when == "{{ nodes.a.success }}"


def test_multi_input_edges_parse(tmp_path: Path) -> None:
    """Round 4 item #11 — two edges sharing a ``to:`` are LEGAL at
    parse time (bind-collision check is Step 5)."""
    yaml_text = """
nodes:
  a: { type: kolsch/nodes/noop }
  b: { type: kolsch/nodes/noop }
  merge: { type: kolsch/nodes/noop }
graph:
  edges:
    - { from: a, to: merge, binds: { x: "{{ nodes.a.value }}" } }
    - { from: b, to: merge, binds: { y: "{{ nodes.b.value }}" } }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.graph is not None
    targets = [e.to for e in wf.graph.edges]
    assert targets == ["merge", "merge"]


# ---------------------------------------------------------------------------
# AC 2.6 — load_workflow surfaces YAML errors with path+line
# ---------------------------------------------------------------------------


def test_malformed_yaml_surfaces_path_and_line(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  a: { type: kolsch/nodes/noop
  b: { type: kolsch/nodes/noop }
"""
    path = _write(tmp_path / "wf.yaml", yaml_text)
    with pytest.raises(WorkflowValidationError) as exc_info:
        load_workflow(path)
    text = str(exc_info.value)
    assert str(path) in text
    # The exact line ruamel reports varies; require ``<path>:<int>``.
    assert f"{path}:" in text


def test_loader_uses_yamlio_helper() -> None:
    """AC 2.6 grep guard — no PyYAML import in parser.py."""
    src = Path(__file__).resolve().parent.parent.parent / "src"
    text = (src / "workflows" / "parser.py").read_text(encoding="utf-8")
    assert "yaml.safe_load" not in text
    assert "import yaml" not in text
    assert "from _yamlio import" in text


# ---------------------------------------------------------------------------
# AC 2.7 — Kind inference
# ---------------------------------------------------------------------------


def test_node_library_has_no_graph_key(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  greet: { type: kolsch/nodes/noop }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.kind == "node_library"
    assert wf.graph is None


def test_empty_edges_is_still_runnable(tmp_path: Path) -> None:
    """Polish-pass Decision P3 — single-node with empty edges = runnable."""
    yaml_text = """
nodes:
  greet: { type: kolsch/nodes/noop }
graph:
  edges: []
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.kind == "workflow"


# ---------------------------------------------------------------------------
# AC 2.8 — Unknown top-level keys
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n: { type: kolsch/nodes/noop }
foo: bar
"""
    with pytest.raises(WorkflowValidationError):
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))


# ---------------------------------------------------------------------------
# AC 2.9 — Binds accept YAML literals OR Jinja-marked strings (J5/β)
# ---------------------------------------------------------------------------


def test_binds_accept_yaml_literals_and_jinja(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  a: { type: kolsch/nodes/noop }
  b: { type: kolsch/nodes/noop }
graph:
  edges:
    - from: a
      to: b
      binds:
        count: 5
        flag: true
        items: [a, b]
        data: null
        url: "https://example.com"
        rendered: "{{ nodes.a.url }}"
        bare_string_NOT_a_ref: "foo.bar"
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.graph is not None
    binds = wf.graph.edges[0].binds
    assert binds["count"] == 5
    assert binds["flag"] is True
    assert binds["items"] == ["a", "b"]
    assert binds["data"] is None
    assert binds["url"] == "https://example.com"
    assert binds["rendered"] == "{{ nodes.a.url }}"
    # Round 6 J5/β: bare strings are LITERAL strings, never auto-ref.
    assert binds["bare_string_NOT_a_ref"] == "foo.bar"


def test_workflow_bind_must_be_jinja_class_is_removed() -> None:
    """Round 6 J5/β — :class:`WorkflowBindMustBeJinja` is gone."""
    from workflows import errors as workflow_errors

    assert not hasattr(workflow_errors, "WorkflowBindMustBeJinja")


# ---------------------------------------------------------------------------
# AC 2.10 — compute node parses with value: + description:
# ---------------------------------------------------------------------------


def test_compute_node_parses_with_description(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  total:
    type: kolsch/nodes/compute
    value: "{{ items | length }}"
    description: "count of items"
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    n = wf.nodes["total"]
    assert isinstance(n, ComputeNodeDef)
    assert n.value == "{{ items | length }}"
    assert n.description == "count of items"


# ---------------------------------------------------------------------------
# AC 2.11 — Node-level `collect:` rejected (Round 4 item #11)
# ---------------------------------------------------------------------------


def test_node_level_collect_rejected_with_fix_path(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n:
    type: kolsch/nodes/noop
    collect: [a, b]
"""
    with pytest.raises(WorkflowValidationError) as exc_info:
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    msg = str(exc_info.value)
    assert "collect" in msg
    assert "edges" in msg or "fan-in" in msg


# ---------------------------------------------------------------------------
# AC 2.12 — Node-level condition: preserved on branch/filter
# ---------------------------------------------------------------------------


def test_branch_node_keeps_condition_as_primary_config(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  b:
    type: kolsch/nodes/branch
    condition: "{{ x > 0 }}"
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    n = wf.nodes["b"]
    assert isinstance(n, BranchNodeDef)
    assert n.condition == "{{ x > 0 }}"


def test_filter_node_keeps_condition_as_primary_config(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  f:
    type: kolsch/nodes/filter
    over: "{{ items }}"
    condition: "{{ item.active }}"
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    n = wf.nodes["f"]
    assert isinstance(n, FilterNodeDef)
    assert n.condition == "{{ item.active }}"


def test_branch_condition_literal_bool_also_parses(tmp_path: Path) -> None:
    """Round 6 J5/β — bare YAML ``true`` / ``false`` is valid."""
    yaml_text = """
nodes:
  b:
    type: kolsch/nodes/branch
    condition: true
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    n = wf.nodes["b"]
    assert isinstance(n, BranchNodeDef)
    assert n.condition is True


# ---------------------------------------------------------------------------
# AC 2.14 — `description:` (R3 #7)
# ---------------------------------------------------------------------------


def test_description_min_length_one_enforced(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n:
    type: kolsch/nodes/noop
    description: ""
"""
    with pytest.raises(WorkflowValidationError):
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))


def test_description_absent_defaults_to_none(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n: { type: kolsch/nodes/noop }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.nodes["n"].description is None


def test_description_parses_on_catalog_node_def(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n:
    type: acme/nodes/echo
    description: "echo node"
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert isinstance(wf.nodes["n"], CatalogNodeDef)
    assert wf.nodes["n"].description == "echo node"


# ---------------------------------------------------------------------------
# AC 2.15 — `timeout_seconds:` (R3 #2)
# ---------------------------------------------------------------------------


def test_top_level_timeout_seconds_parses(tmp_path: Path) -> None:
    yaml_text = """
timeout_seconds: 60
nodes:
  n: { type: kolsch/nodes/noop }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.timeout_seconds == 60


def test_top_level_timeout_seconds_negative_parses_but_step_5_rejects(
    tmp_path: Path,
) -> None:
    """Parser accepts -5 for shape; Step 5 raises ``WorkflowInvalidTimeout``."""
    yaml_text = """
timeout_seconds: -5
nodes:
  n: { type: kolsch/nodes/noop }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.timeout_seconds == -5


def test_per_node_timeout_seconds_parses(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n:
    type: kolsch/nodes/noop
    timeout_seconds: 30
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.nodes["n"].timeout_seconds == 30


# ---------------------------------------------------------------------------
# AC 2.16 — Per-built-in Inputs(BaseModel) classes (Round 5 H8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inputs_cls", "expected_extra"),
    [
        (BashInputs, "allow"),
        (AgentInputs, "allow"),
        (WorkflowSubInputs, "allow"),
        (NoopInputs, "forbid"),
        (UserInputInputs, "allow"),
        (ReadFileInputs, "forbid"),
        (WriteFileInputs, "forbid"),
        (ListFilesInputs, "forbid"),
        (SetStateInputs, "allow"),
        (DeleteStateInputs, "forbid"),
        (FilterInputs, "allow"),
        (MapInputs, "allow"),
        (BranchInputs, "allow"),
        (ComputeInputs, "allow"),
    ],
)
def test_per_builtin_inputs_extra_config_matches_locked_table(
    inputs_cls: type, expected_extra: str
) -> None:
    assert inputs_cls.model_config["extra"] == expected_extra


def test_filter_inputs_has_strict_typed_items_field() -> None:
    assert "items" in FilterInputs.model_fields
    # The locked table types items as list[Any].
    annotation = FilterInputs.model_fields["items"].annotation
    assert str(annotation).startswith("list")


def test_map_inputs_has_strict_typed_over_field() -> None:
    assert "over" in MapInputs.model_fields
    annotation = MapInputs.model_fields["over"].annotation
    assert str(annotation).startswith("list")


def test_all_14_inputs_classes_exist() -> None:
    """AC 2.16 sanity — count the *Inputs classes by name."""
    names = {
        "BashInputs",
        "AgentInputs",
        "WorkflowSubInputs",
        "NoopInputs",
        "UserInputInputs",
        "ReadFileInputs",
        "WriteFileInputs",
        "ListFilesInputs",
        "SetStateInputs",
        "DeleteStateInputs",
        "FilterInputs",
        "MapInputs",
        "BranchInputs",
        "ComputeInputs",
    }
    assert len(names) == 14
    from workflows import parser

    for n in names:
        assert hasattr(parser, n), n


# ---------------------------------------------------------------------------
# AC 2.17 — Top-level policy: renamed from permissions:
# ---------------------------------------------------------------------------


def test_policy_top_level_field_parses(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n: { type: kolsch/nodes/noop }
policy:
  - what: "kolsch/bash"
    pattern: "*"
    effect: allow
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert len(wf.policy) == 1
    assert wf.policy[0].what == "kolsch/bash"
    assert wf.policy[0].effect == "allow"


def test_permissions_top_level_key_rejected_with_fix_path(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n: { type: kolsch/nodes/noop }
permissions:
  - what: "kolsch/bash"
    pattern: "*"
    effect: allow
"""
    with pytest.raises(WorkflowValidationError) as exc_info:
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    msg = str(exc_info.value)
    assert "permissions" in msg
    assert "policy" in msg


def test_internal_authz_module_path_unchanged() -> None:
    """H1 lock — code rename is queued; the module path stays."""
    from providers import authz

    assert authz.__name__ == "providers.authz"


# ---------------------------------------------------------------------------
# Extends:
# ---------------------------------------------------------------------------


def test_extends_parses_as_list_of_strings(tmp_path: Path) -> None:
    yaml_text = """
extends:
  - acme/workflows/base
  - acme/workflows/mixin
nodes:
  n: { type: kolsch/nodes/noop }
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    assert wf.extends == ["acme/workflows/base", "acme/workflows/mixin"]


# ---------------------------------------------------------------------------
# AC 2.13 — CatalogNodeDef carries common fields
# ---------------------------------------------------------------------------


def test_catalog_node_def_carries_common_fields(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  n:
    type: acme/nodes/echo
    description: "echo handler"
    timeout_seconds: 30
    when: "{{ state.ready }}"
    retry: 2
    secrets: [API_KEY]
    extra_field: "passed through"
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    n = wf.nodes["n"]
    assert isinstance(n, CatalogNodeDef)
    assert n.description == "echo handler"
    assert n.timeout_seconds == 30
    assert n.when == "{{ state.ready }}"
    assert n.retry == 2
    assert n.secrets == ["API_KEY"]
    # Type-specific config passes through untouched (Step 5 validates it).
    assert getattr(n, "extra_field", None) == "passed through"


# ---------------------------------------------------------------------------
# browse_url — Step 1 Test 1 (accepts valid) + Test 2 (rejects invalid).
# Per qol-browse-url-node.md Tests 1-2.
# ---------------------------------------------------------------------------


def test_browse_url_parser_accepts_valid_defaults(tmp_path: Path) -> None:
    """Test 1 — YAML `{type: kolsch/nodes/browse_url, url: ...}` parses to a
    :class:`BrowseUrlNodeDef` carrying the documented defaults
    (`format="text"`, `wait_selector=None`, `headers=None`,
    `cookies=None`, `request_timeout_seconds=30`).
    """
    yaml_text = """
nodes:
  fetch:
    type: kolsch/nodes/browse_url
    url: "https://example.com"
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    node = wf.nodes["fetch"]
    assert isinstance(node, BrowseUrlNodeDef)
    assert node.type == "kolsch/nodes/browse_url"
    assert node.url == "https://example.com"
    assert node.format == "text"
    assert node.wait_selector is None
    assert node.headers is None
    assert node.cookies is None
    assert node.request_timeout_seconds == 30


def test_browse_url_parser_accepts_full_config(tmp_path: Path) -> None:
    """All optional type-specific fields land on the parsed model unchanged."""
    yaml_text = """
nodes:
  fetch:
    type: kolsch/nodes/browse_url
    url: "{{ inputs.target }}"
    format: html
    wait_selector: "main"
    headers:
      X-From: "kolsch"
    cookies:
      session: "abc"
    request_timeout_seconds: 45
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    node = wf.nodes["fetch"]
    assert isinstance(node, BrowseUrlNodeDef)
    assert node.url == "{{ inputs.target }}"
    assert node.format == "html"
    assert node.wait_selector == "main"
    assert node.headers == {"X-From": "kolsch"}
    assert node.cookies == {"session": "abc"}
    assert node.request_timeout_seconds == 45


def test_browse_url_parser_invalid_missing_url(tmp_path: Path) -> None:
    """Test 2 — missing `url` fails shape validation."""
    yaml_text = """
nodes:
  fetch:
    type: kolsch/nodes/browse_url
"""
    with pytest.raises(WorkflowValidationError):
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))


def test_browse_url_parser_invalid_bad_format_literal(tmp_path: Path) -> None:
    """Test 2 — `format: markdown` is rejected (Literal["text","html"])."""
    yaml_text = """
nodes:
  fetch:
    type: kolsch/nodes/browse_url
    url: "https://example.com"
    format: markdown
"""
    with pytest.raises(WorkflowValidationError):
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))


def test_browse_url_parser_invalid_timeout_below_bound(tmp_path: Path) -> None:
    """Test 2 — `request_timeout_seconds: 0` is out of `ge=1`."""
    yaml_text = """
nodes:
  fetch:
    type: kolsch/nodes/browse_url
    url: "https://example.com"
    request_timeout_seconds: 0
"""
    with pytest.raises(WorkflowValidationError):
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))


def test_browse_url_parser_invalid_timeout_above_bound(tmp_path: Path) -> None:
    """Test 2 — `request_timeout_seconds: 300` is out of `le=120`."""
    yaml_text = """
nodes:
  fetch:
    type: kolsch/nodes/browse_url
    url: "https://example.com"
    request_timeout_seconds: 300
"""
    with pytest.raises(WorkflowValidationError):
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))


def test_browse_url_parser_rejects_ambiguous_timeout_seconds(tmp_path: Path) -> None:
    """Naked ``timeout_seconds`` on browse_url is the confusion case.

    ``_BaseNodeDef.timeout_seconds`` bounds the workflow-executor budget,
    while ``BrowseUrlNodeDef.request_timeout_seconds`` bounds the tool's
    per-request timeout. A YAML author writing only ``timeout_seconds``
    almost certainly meant the tool budget — but the value silently
    lands on the executor knob. Parser must raise with a disambiguation
    hint rather than accept it.
    """
    yaml_text = """
nodes:
  fetch:
    type: kolsch/nodes/browse_url
    url: "https://example.com"
    timeout_seconds: 30
"""
    with pytest.raises(WorkflowValidationError) as exc_info:
        load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    # Error message must name both knobs so the user can pick.
    msg = str(exc_info.value)
    assert "request_timeout_seconds" in msg
    assert "timeout_seconds" in msg


def test_browse_url_parser_accepts_both_timeouts_explicitly(tmp_path: Path) -> None:
    """When both timeout knobs are set explicitly, the user disambiguated."""
    yaml_text = """
nodes:
  fetch:
    type: kolsch/nodes/browse_url
    url: "https://example.com"
    timeout_seconds: 60
    request_timeout_seconds: 30
"""
    wf = load_workflow(_write(tmp_path / "wf.yaml", yaml_text))
    node = wf.nodes["fetch"]
    assert node.timeout_seconds == 60  # executor budget
    assert node.request_timeout_seconds == 30  # tool budget


# ---------------------------------------------------------------------------
# Helper used by parameterised tests above (kept at file end for locality)
# ---------------------------------------------------------------------------


def _to_yaml(payload: dict[str, Any]) -> str:
    """Render a small dict to YAML for parser-input fixtures.

    Use ``_yamlio.dump`` so the round-trip exercises the same loader
    the parser uses.
    """
    from _yamlio import dump

    return dump(payload)

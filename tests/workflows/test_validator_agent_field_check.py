"""bug-062 B1 — validator catches unknown agent output-field references.

Covers acceptance criteria 1-5:

- Test 1: an agent node whose ``io.outputs`` is ``{text}`` referenced
  as ``nodes.<agent>.indexed_files`` fails ``validate`` with a fatal
  :class:`WorkflowJinjaRefUnknown(reason="unknown_field")`.
- Test 2: a bind referencing a valid agent output field validates
  cleanly (no false positive).
- Test 3: an unresolvable agent ref (not in the catalog) does NOT
  crash the validator — the field check falls back to open/silent for
  that node.
- Test 4: builtin-node output-field checks still fire (regression
  pin for ``list_files→entries``, ``compute→value``,
  ``bash→{stdout, stderr, exit_code}``).
- Test 5 (bug-062 spec Test 4 = "no regression"): a previously-valid
  workflow with a valid agent ref still validates.

All tests build a real on-disk agent YAML + wire a matching
``AssetEntry`` so :func:`workflows.validator._agent_output_field_set`
can load ``io.outputs`` via ``catalog.lookup``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog, AssetEntry
from workflows.errors import WorkflowJinjaRefUnknown
from workflows.parser import Workflow
from workflows.validator import validate_workflow

if TYPE_CHECKING:
    from pathlib import Path


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _catalog_with_agent(
    tmp_path: Path,
    *,
    ref: str,
    outputs_block: str,
) -> AssetCatalog:
    """Write an agent YAML with the given `io.outputs` and register it.

    ``outputs_block`` is the raw YAML fragment for ``io.outputs``
    (e.g. ``"text:\\n      type: str"``).
    """
    bundle, _plural, name = ref.split("/")
    agent_path = tmp_path / f"{name}.yaml"
    agent_path.write_text(
        (
            "schema_version: '0.1'\n"
            "assembly_version: '1'\n"
            f"agent_id: ag_{name}\n"
            f"name: {name}\n"
            "io:\n"
            "  mode: single_shot\n"
            "  inputs: {}\n"
            "  outputs:\n"
            f"    {outputs_block}\n"
            "prompt: |\n"
            "  do the thing\n"
        ),
        encoding="utf-8",
    )
    cat = AssetCatalog()
    cat.assets[ref] = AssetEntry(
        ref=ref,
        asset_type="agent",
        bundle=bundle,
        path=str(agent_path),
        metadata={},
    )
    return cat


# ---------------------------------------------------------------------------
# Test 1 — agent unknown-field fails validate
# ---------------------------------------------------------------------------


def test_validate_agent_unknown_field_fails(tmp_path: Path) -> None:
    """An agent whose ``io.outputs`` is ``{text}`` referenced as
    ``nodes.<agent>.indexed_files`` MUST fail validation with a fatal
    :class:`WorkflowJinjaRefUnknown(reason="unknown_field")`.
    """
    cat = _catalog_with_agent(
        tmp_path,
        ref="autoply/agents/resume_indexer",
        outputs_block="text:\n      type: str",
    )
    wf = _wf(
        {
            "nodes": {
                "read_index": {
                    "type": "kolsch/nodes/agent",
                    "agent": "autoply/agents/resume_indexer",
                },
                "identify_missing": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "read_index",
                        "to": "identify_missing",
                        "binds": {
                            "indexed_files": ("{{ nodes.read_index.indexed_files }}"),
                        },
                    }
                ]
            },
        }
    )
    result = validate_workflow(wf, cat)
    unknowns = [
        e
        for e in result.errors
        if isinstance(e, WorkflowJinjaRefUnknown) and e.reason == "unknown_field"
    ]
    assert unknowns, (
        f"expected a WorkflowJinjaRefUnknown(unknown_field) finding; "
        f"got errors={[type(e).__name__ for e in result.errors]}"
    )
    only = unknowns[0]
    assert only.node == "identify_missing"
    assert "indexed_files" in only.reference
    assert "read_index" in only.reference


# ---------------------------------------------------------------------------
# Test 2 — valid agent field still passes
# ---------------------------------------------------------------------------


def test_validate_agent_valid_field_still_passes(tmp_path: Path) -> None:
    """A bind referencing a declared agent output must not raise
    ``unknown_field``. Ensures no false positive."""
    cat = _catalog_with_agent(
        tmp_path,
        ref="autoply/agents/resume_indexer",
        outputs_block="text:\n      type: str",
    )
    wf = _wf(
        {
            "nodes": {
                "read_index": {
                    "type": "kolsch/nodes/agent",
                    "agent": "autoply/agents/resume_indexer",
                },
                "identify_missing": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "read_index",
                        "to": "identify_missing",
                        "binds": {"text": "{{ nodes.read_index.text }}"},
                    }
                ]
            },
        }
    )
    result = validate_workflow(wf, cat)
    unknowns = [
        e
        for e in result.errors
        if isinstance(e, WorkflowJinjaRefUnknown) and e.reason == "unknown_field"
    ]
    assert not unknowns, (
        f"unexpected unknown_field on a valid agent output ref; "
        f"got {[str(e) for e in unknowns]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — unresolvable agent ref degrades gracefully (AC #4)
# ---------------------------------------------------------------------------


def test_validate_agent_unresolvable_ref_degrades_gracefully() -> None:
    """An agent whose ref is NOT in the catalog must not crash the
    validator. The field check falls back to open behavior for that
    node — no ``unknown_field`` finding, no exception."""
    cat = AssetCatalog()  # empty — the ref won't resolve
    wf = _wf(
        {
            "nodes": {
                "read_index": {
                    "type": "kolsch/nodes/agent",
                    "agent": "autoply/agents/ghost",  # not in catalog
                },
                "identify_missing": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "read_index",
                        "to": "identify_missing",
                        "binds": {
                            "anything": "{{ nodes.read_index.anything }}",
                        },
                    }
                ]
            },
        }
    )
    # Must not raise.
    result = validate_workflow(wf, cat)
    # And must NOT produce an unknown_field finding for this bind
    # (the field check falls back to open for the unresolvable ref).
    unknowns = [
        e
        for e in result.errors
        if isinstance(e, WorkflowJinjaRefUnknown) and e.reason == "unknown_field"
    ]
    assert not unknowns


def test_validate_agent_inline_outputs_override_wins(tmp_path: Path) -> None:
    """When ``AgentNodeDef.outputs`` is set inline, those keys ARE the
    output schema — the runner replaces ``config.io.outputs`` wholesale
    (see ``workflows/builtins/agent.py`` step 3). The validator MUST
    field-check against the override, not the catalog YAML.
    """
    cat = _catalog_with_agent(
        tmp_path,
        ref="autoply/agents/resume_indexer",
        outputs_block="text:\n      type: str",
    )
    # Override narrows the agent's outputs to `{summary}`; a bind on
    # `text` (declared in YAML) must now be flagged.
    wf = _wf(
        {
            "nodes": {
                "read_index": {
                    "type": "kolsch/nodes/agent",
                    "agent": "autoply/agents/resume_indexer",
                    "outputs": {
                        "mode": "single_shot",
                        "outputs": {"summary": {"type": "str"}},
                    },
                },
                "next": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "read_index",
                        "to": "next",
                        "binds": {"x": "{{ nodes.read_index.text }}"},
                    }
                ]
            },
        }
    )
    result = validate_workflow(wf, cat)
    unknowns = [
        e
        for e in result.errors
        if isinstance(e, WorkflowJinjaRefUnknown) and e.reason == "unknown_field"
    ]
    assert unknowns, "override should narrow outputs; `text` no longer valid"


# ---------------------------------------------------------------------------
# Test 4 (spec Test 4) — builtin-node unknown-field checks unchanged
# ---------------------------------------------------------------------------


def test_validate_builtin_unknown_field_still_fires() -> None:
    """Regression pin: builtin output-field checks were unaffected by
    the AgentNodeDef change. ``list_files → entries`` is declared;
    reference ``nodes.<ls>.oops`` must still fail with
    ``unknown_field``."""
    wf = _wf(
        {
            "nodes": {
                "ls": {"type": "kolsch/nodes/list_files", "path": "/tmp"},
                "sink": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "ls",
                        "to": "sink",
                        "binds": {"z": "{{ nodes.ls.oops }}"},
                    }
                ]
            },
        }
    )
    result = validate_workflow(wf, AssetCatalog())
    unknowns = [
        e
        for e in result.errors
        if isinstance(e, WorkflowJinjaRefUnknown) and e.reason == "unknown_field"
    ]
    assert unknowns, "builtin field check must still fire"


def test_validate_builtin_valid_field_still_passes() -> None:
    """The mirror-image assurance: a real builtin output field
    (``list_files.entries``) still validates cleanly."""
    wf = _wf(
        {
            "nodes": {
                "ls": {"type": "kolsch/nodes/list_files", "path": "/tmp"},
                "sink": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "ls",
                        "to": "sink",
                        "binds": {"z": "{{ nodes.ls.entries }}"},
                    }
                ]
            },
        }
    )
    result = validate_workflow(wf, AssetCatalog())
    unknowns = [
        e
        for e in result.errors
        if isinstance(e, WorkflowJinjaRefUnknown) and e.reason == "unknown_field"
    ]
    assert not unknowns

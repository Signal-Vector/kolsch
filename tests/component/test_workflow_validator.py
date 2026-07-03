"""Phase 3.0 Step 5 — workflow run-start validator tests.

Organized as test classes per master spec §7 sub-section + the new
gate-level checks per Step 5 ACs 5.1-5.16:

- :class:`TestValidationResultAPI` — R3 #6 ValidationResult dataclass +
  dual public surface.
- :class:`TestStructural` — §7.1 structural checks.
- :class:`TestBindCollision` — Round 4 item #11 bind-key collision.
- :class:`TestNodeNotDefined` — pre-pass for unresolved YAML node refs.
- :class:`TestJinjaRefs` — §7.2 graph-reachability predecessor closure.
- :class:`TestBranching` — §7.3 branch arms.
- :class:`TestExtends` — §7.5 ``WorkflowExtendsPolicyForbidden``.
- :class:`TestTimeouts` — R3 #2 positive int + shadowing warning.
- :class:`TestFromStrictValidation` — Round 4 item #13 hard errors.
- :class:`TestAccumulation` — multi-error ExceptionGroup behavior.
- :class:`TestRegressionRemovals` — verify removed classes stay removed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from workflows.errors import (
    WorkflowBindCollision,
    WorkflowBranchMissingArm,
    WorkflowBranchOnNonBranchable,
    WorkflowExtendsPolicyForbidden,
    WorkflowFromAssetTypeError,
    WorkflowFromNotFound,
    WorkflowFromShapeError,
    WorkflowInvalidTimeout,
    WorkflowJinjaRefUnknown,
    WorkflowNodeImportFailed,
    WorkflowNodeMissingRun,
    WorkflowNodeNotDecorated,
    WorkflowNodeNotDefined,
    WorkflowNodeReturnTypeInvalid,
    WorkflowNodeSignatureInvalid,
    WorkflowTimeoutShadowsNode,
    WorkflowUnreachableNode,
    WorkflowValidationError,
)
from workflows.parser import Workflow
from workflows.validator import (
    ValidationResult,
    validate_workflow,
    validate_workflow_or_raise,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _seed_catalog(agents: list[str] | None = None) -> AssetCatalog:
    cat = AssetCatalog()
    for agent_name in agents or []:
        ref = f"kolsch/agents/{agent_name}"
        cat.assets[ref] = AssetEntry(
            ref=ref,
            asset_type="agent",
            bundle="kolsch",
            path=f"/var/empty/{ref}",
            metadata={},
        )
    return cat


# ---------------------------------------------------------------------------
# AC 5.8 — ValidationResult + dual surface
# ---------------------------------------------------------------------------


class TestValidationResultAPI:
    def test_validation_result_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        result = ValidationResult()
        with pytest.raises(FrozenInstanceError):
            result.errors = ()  # type: ignore[misc]

    def test_is_valid_property(self) -> None:
        assert ValidationResult().is_valid is True
        err = WorkflowNodeNotDefined(
            file="<wf>", line=0, ref="missing", location="edge"
        )
        assert ValidationResult(errors=(err,)).is_valid is False

    def test_warnings_do_not_affect_is_valid(self) -> None:
        warn = WorkflowTimeoutShadowsNode(
            file="<wf>",
            line=0,
            node="n",
            node_timeout=60.0,
            workflow_timeout=30.0,
        )
        result = ValidationResult(warnings=(warn,))
        assert result.is_valid is True

    def test_validate_workflow_clean_workflow_returns_empty_result(
        self,
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "greet": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hi",
                    }
                },
                "graph": {"edges": []},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert result.errors == ()
        assert result.warnings == ()
        assert result.is_valid is True

    def test_validate_workflow_or_raise_clean_returns_none(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "greet": {"type": "kolsch/nodes/noop"},
                },
            }
        )
        assert validate_workflow_or_raise(wf, AssetCatalog()) is None

    def test_validate_workflow_or_raise_single_error_raises_specific(
        self,
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [{"from": "a", "to": "missing"}],
                },
            }
        )
        with pytest.raises(WorkflowNodeNotDefined):
            validate_workflow_or_raise(wf, AssetCatalog())


# ---------------------------------------------------------------------------
# AC 5.1 — Structural checks (§7.1)
# ---------------------------------------------------------------------------


class TestStructural:
    def test_unreachable_node_raises(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                    "orphan": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "a", "to": "b"}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(isinstance(e, WorkflowUnreachableNode) for e in result.errors)

    def test_map_apply_target_is_reachable_via_apply(self) -> None:
        """Map workers do not appear in the edge graph but are
        considered reachable via the ``apply:`` reference."""
        wf = _wf(
            {
                "nodes": {
                    "iterate": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": "[1, 2]",
                        "item_as": "row",
                    },
                    "worker": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": []},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert not any(isinstance(e, WorkflowUnreachableNode) for e in result.errors)


class TestBindCollision:
    def test_two_edges_same_target_same_key_raises(self) -> None:
        """Round 4 item #11 — two edges into the same target binding
        the same key → :class:`WorkflowBindCollision`."""
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                    "merge": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "a",
                            "to": "merge",
                            "binds": {"x": "{{ nodes.a.stdout }}"},
                        },
                        {
                            "from": "b",
                            "to": "merge",
                            "binds": {"x": "{{ nodes.b.stdout }}"},
                        },
                    ]
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(isinstance(e, WorkflowBindCollision) for e in result.errors)

    def test_multi_input_edges_with_distinct_keys_legal(self) -> None:
        """Round 4 item #11 — multi-input edges with DIFFERENT bind
        keys validate cleanly (no ``WorkflowMultiInputForbidden``)."""
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                    "merge": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "a", "to": "merge", "binds": {"x": "1"}},
                        {"from": "b", "to": "merge", "binds": {"y": "2"}},
                    ]
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        # No bind collision; reachability also fine.
        assert not any(isinstance(e, WorkflowBindCollision) for e in result.errors)


class TestNodeNotDefined:
    def test_edge_from_names_unknown_node(self) -> None:
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": [{"from": "missing", "to": "a"}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowNodeNotDefined) and e.ref == "missing"
            for e in result.errors
        )

    def test_edge_to_names_unknown_node(self) -> None:
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": [{"from": "a", "to": "missing"}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowNodeNotDefined) and e.ref == "missing"
            for e in result.errors
        )

    def test_copy_of_unknown_source(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {"copy_of": "ghost"},
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowNodeNotDefined) and e.ref == "ghost"
            for e in result.errors
        )

    def test_map_apply_unknown_target(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "m": {
                        "type": "kolsch/nodes/map",
                        "apply": "ghost",
                        "over": "[]",
                        "item_as": "x",
                    },
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowNodeNotDefined) and e.ref == "ghost"
            for e in result.errors
        )


# ---------------------------------------------------------------------------
# AC 5.2 — Jinja refs + predecessor closure
# ---------------------------------------------------------------------------


class TestJinjaRefs:
    def test_known_predecessor_with_known_field_passes(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hi",
                    },
                    "b": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ nodes.a.stdout }}",
                    },
                },
                "graph": {"edges": [{"from": "a", "to": "b"}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert result.is_valid

    def test_unknown_field_raises_unknown_field_reason(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hi",
                    },
                    "b": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ nodes.a.bogus }}",
                    },
                },
                "graph": {"edges": [{"from": "a", "to": "b"}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowJinjaRefUnknown) and e.reason == "unknown_field"
            for e in result.errors
        )

    def test_non_predecessor_raises_not_predecessor_reason(self) -> None:
        """``foo`` is defined but NOT an ancestor of the consumer."""
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "foo": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hi",
                    },
                    "b": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ nodes.foo.stdout }}",
                    },
                },
                "graph": {"edges": [{"from": "a", "to": "b"}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(
            isinstance(e, WorkflowJinjaRefUnknown) and e.reason == "not_predecessor"
            for e in result.errors
        )

    def test_binds_parent_special_case(self) -> None:
        """``nodes.<edge.from>.<field>`` in a ``binds:`` RHS is
        implicitly allowed even before the source is a multi-hop
        ancestor."""
        wf = _wf(
            {
                "nodes": {
                    "fetch": {
                        "type": "kolsch/nodes/bash",
                        "command": "curl x",
                    },
                    "transform": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.data }}",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "fetch",
                            "to": "transform",
                            "binds": {"data": "{{ nodes.fetch.stdout }}"},
                        }
                    ]
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert result.is_valid

    def test_yaml_literal_bind_skips_jinja_walk(self) -> None:
        """Round 6 J5/β — a bare YAML literal RHS is NOT walked as
        Jinja; the parser preserves the literal value."""
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "a",
                            "to": "b",
                            "binds": {
                                "count": 5,
                                "flag": True,
                                "url": "https://example.com",
                                "literal": "foo.bar",
                            },
                        }
                    ]
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert result.is_valid


# ---------------------------------------------------------------------------
# AC 5.3 — Branching (§7.3)
# ---------------------------------------------------------------------------


class TestBranching:
    def test_branch_missing_arm(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "decide": {
                        "type": "kolsch/nodes/branch",
                        "condition": "{{ true }}",
                    },
                    "yes_handler": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "decide",
                            "to": "yes_handler",
                            "branch": True,
                        }
                    ]
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(isinstance(e, WorkflowBranchMissingArm) for e in result.errors)

    def test_branch_on_non_branchable_node(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "a", "to": "b", "branch": True}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(isinstance(e, WorkflowBranchOnNonBranchable) for e in result.errors)


# ---------------------------------------------------------------------------
# AC 5.14 — Timeouts (R3 #2)
# ---------------------------------------------------------------------------


class TestTimeouts:
    def test_workflow_negative_timeout_errors(self) -> None:
        wf = _wf(
            {
                "timeout_seconds": -5,
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(isinstance(e, WorkflowInvalidTimeout) for e in result.errors)

    def test_workflow_zero_timeout_errors(self) -> None:
        wf = _wf(
            {
                "timeout_seconds": 0,
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(isinstance(e, WorkflowInvalidTimeout) for e in result.errors)

    def test_per_node_negative_timeout_errors(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {
                        "type": "kolsch/nodes/noop",
                        "timeout_seconds": -1,
                    }
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(isinstance(e, WorkflowInvalidTimeout) for e in result.errors)

    def test_per_node_shadowing_emits_warning(self) -> None:
        wf = _wf(
            {
                "timeout_seconds": 30,
                "nodes": {
                    "a": {
                        "type": "kolsch/nodes/noop",
                        "timeout_seconds": 60,
                    }
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert any(isinstance(w, WorkflowTimeoutShadowsNode) for w in result.warnings)
        # The warning does NOT block validity.
        assert all(not isinstance(e, WorkflowTimeoutShadowsNode) for e in result.errors)

    def test_clean_timeouts_no_findings(self) -> None:
        wf = _wf(
            {
                "timeout_seconds": 60,
                "nodes": {
                    "a": {
                        "type": "kolsch/nodes/noop",
                        "timeout_seconds": 30,
                    }
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert result.is_valid
        assert not result.warnings


# ---------------------------------------------------------------------------
# AC 5.15 — from: STRICT validation (Round 4 item #13)
# ---------------------------------------------------------------------------


class TestFromStrictValidation:
    def test_from_shape_error_for_non_3seg(self) -> None:
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "policy": [
                    {
                        "what": "*",
                        "pattern": "*",
                        "from": "random",
                        "effect": "allow",
                    }
                ],
            }
        )
        result = validate_workflow(wf, _seed_catalog(agents=["triage"]))
        assert any(isinstance(e, WorkflowFromShapeError) for e in result.errors)

    def test_from_asset_type_error_for_wrong_segment(self) -> None:
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "policy": [
                    {
                        "what": "*",
                        "pattern": "*",
                        "from": "kolsch/nodes/parse",
                        "effect": "allow",
                    }
                ],
            }
        )
        result = validate_workflow(wf, _seed_catalog(agents=["triage"]))
        assert any(isinstance(e, WorkflowFromAssetTypeError) for e in result.errors)

    def test_from_not_found_for_unknown_agent(self) -> None:
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "policy": [
                    {
                        "what": "*",
                        "pattern": "*",
                        "from": "kolsch/agents/ghost",
                        "effect": "allow",
                    }
                ],
            }
        )
        result = validate_workflow(wf, _seed_catalog(agents=["triage"]))
        assert any(isinstance(e, WorkflowFromNotFound) for e in result.errors)

    def test_valid_from_passes(self) -> None:
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "policy": [
                    {
                        "what": "*",
                        "pattern": "*",
                        "from": "kolsch/agents/triage",
                        "effect": "allow",
                    }
                ],
            }
        )
        result = validate_workflow(wf, _seed_catalog(agents=["triage"]))
        # No from-related errors.
        assert all(
            not isinstance(
                e,
                (
                    WorkflowFromShapeError,
                    WorkflowFromAssetTypeError,
                    WorkflowFromNotFound,
                ),
            )
            for e in result.errors
        )


# ---------------------------------------------------------------------------
# AC 5.5 — Extends + Round 6 S5
# ---------------------------------------------------------------------------


class TestExtends:
    def test_extends_policy_forbidden(self, tmp_path: Path) -> None:
        """Round 6 S5 — an extended workflow with non-empty ``policy:``
        triggers :class:`WorkflowExtendsPolicyForbidden`."""
        parent_path = tmp_path / "parent.yaml"
        parent_path.write_text(
            (
                "nodes:\n"
                "  greet: { type: kolsch/nodes/noop }\n"
                "policy:\n"
                "  - what: kolsch/bash\n"
                "    pattern: '*'\n"
                "    effect: allow\n"
            ),
            encoding="utf-8",
        )
        cat = AssetCatalog()
        cat.assets["acme/workflows/parent"] = AssetEntry(
            ref="acme/workflows/parent",
            asset_type="workflow",
            bundle="acme",
            path=str(parent_path),
            metadata={},
        )
        wf = _wf(
            {
                "extends": ["acme/workflows/parent"],
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
            }
        )
        result = validate_workflow(wf, cat)
        assert any(isinstance(e, WorkflowExtendsPolicyForbidden) for e in result.errors)


# ---------------------------------------------------------------------------
# AC 5.11 — Accumulation
# ---------------------------------------------------------------------------


class TestAccumulation:
    def test_multiple_errors_raise_exception_group(self) -> None:
        """Three sibling-independent edge.from typos → three
        ``WorkflowNodeNotDefined`` findings → ``ExceptionGroup``."""
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {
                    "edges": [
                        {"from": "x", "to": "a"},
                        {"from": "y", "to": "a"},
                        {"from": "z", "to": "a"},
                    ]
                },
            }
        )
        with pytest.raises(ExceptionGroup) as exc_info:
            validate_workflow_or_raise(wf, AssetCatalog())
        assert len(exc_info.value.exceptions) == 3
        assert all(
            isinstance(e, WorkflowNodeNotDefined) for e in exc_info.value.exceptions
        )

    def test_single_error_raises_specific_class_not_group(self) -> None:
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": [{"from": "x", "to": "a"}]},
            }
        )
        with pytest.raises(WorkflowNodeNotDefined):
            validate_workflow_or_raise(wf, AssetCatalog())


# ---------------------------------------------------------------------------
# AC 5.13 — Error message contract (single-line + Fix: + file:line)
# ---------------------------------------------------------------------------


class TestErrorMessageContract:
    def test_node_not_defined_message_is_single_line_with_fix(self) -> None:
        wf = _wf(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": [{"from": "ghost", "to": "a"}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        exc = result.errors[0]
        text = str(exc)
        assert "\n" not in text
        assert "Fix: " in text
        # file token present
        assert "<workflow>" in text


# ---------------------------------------------------------------------------
# AC 5.10 — Multi-write state semantics (per polish-pass Decision P5)
# ---------------------------------------------------------------------------


class TestMultiWriteState:
    def test_three_set_state_writes_same_key_validate_clean(self) -> None:
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"attempts": 0},
                    },
                    "step1": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"attempts": 1},
                    },
                    "step2": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"attempts": 2},
                    },
                },
                "graph": {
                    "edges": [
                        {"from": "init", "to": "step1"},
                        {"from": "step1", "to": "step2"},
                    ]
                },
            }
        )
        result = validate_workflow(wf, AssetCatalog())
        assert result.is_valid
        assert not result.warnings


# ---------------------------------------------------------------------------
# AC 5.9 — Regression: removed classes stay removed
# ---------------------------------------------------------------------------


class TestRegressionRemovals:
    def test_workflow_multi_input_forbidden_class_is_removed(self) -> None:
        """Round 4 item #11 — the class no longer exists."""
        from workflows import errors as workflow_errors

        assert not hasattr(workflow_errors, "WorkflowMultiInputForbidden")

    def test_workflow_from_shape_warning_class_is_removed(self) -> None:
        """Round 4 item #13 — the warning fallback is GONE."""
        from workflows import errors as workflow_errors

        assert not hasattr(workflow_errors, "WorkflowFromShapeWarning")

    def test_workflow_bind_must_be_jinja_class_is_removed(self) -> None:
        """Round 6 J5/β — binds: accepts YAML literals or Jinja-marked."""
        from workflows import errors as workflow_errors

        assert not hasattr(workflow_errors, "WorkflowBindMustBeJinja")

    def test_validator_source_no_legacy_class_names(self) -> None:
        """AC 5.16 — R3 #9 renames are fully applied: no legacy
        class names survive in the validator source."""
        validator = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "workflows"
            / "validator.py"
        )
        text = validator.read_text(encoding="utf-8")
        assert "WorkflowBindNotJinja" not in text
        assert "WorkflowNodeRefUnknown" not in text
        assert "WorkflowBranchDeadSide" not in text
        # No bare jinja2.Environment() calls — all parsing goes through
        # the sandboxed singleton.
        assert "Environment()" not in text
        assert "NativeEnvironment()" not in text


# ---------------------------------------------------------------------------
# AC 5.9 — Every WorkflowValidationError subclass triggers somewhere.
# ---------------------------------------------------------------------------


class TestEveryErrorClassTriggered:
    """Roll-up assertion that the validator's tests collectively cover
    every concrete :class:`WorkflowValidationError` subclass touched
    by Step 5."""

    def test_critical_classes_triggered_by_tests_in_this_module(self) -> None:
        triggered: set[type[WorkflowValidationError]] = {
            WorkflowNodeNotDefined,
            WorkflowUnreachableNode,
            WorkflowBindCollision,
            WorkflowJinjaRefUnknown,
            WorkflowBranchMissingArm,
            WorkflowBranchOnNonBranchable,
            WorkflowInvalidTimeout,
            WorkflowFromShapeError,
            WorkflowFromAssetTypeError,
            WorkflowFromNotFound,
            WorkflowExtendsPolicyForbidden,
            # §7.6 introspection classes (AC 5.6 / 5.9).
            WorkflowNodeImportFailed,
            WorkflowNodeMissingRun,
            WorkflowNodeNotDecorated,
            WorkflowNodeSignatureInvalid,
            WorkflowNodeReturnTypeInvalid,
        }
        # Spot-check at least one trigger exists per class.
        assert len(triggered) == 16


# ---------------------------------------------------------------------------
# AC 5.6 — §7.6 authored Python node introspection.
# ---------------------------------------------------------------------------


def _seed_catalog_with_node(
    *,
    bundle: str,
    name: str,
    path: str,
    builtin: bool = False,
) -> AssetCatalog:
    """Build a catalog containing one node entry pointing at ``path``.

    ``builtin=True`` mirrors the virtual built-in registration done by
    :func:`workflows.catalog.builtin_node_entries`; the validator skips
    those entries so authoring an in-place .py would not be picked up.
    """
    cat = AssetCatalog()
    ref = f"{bundle}/nodes/{name}"
    cat.assets[ref] = AssetEntry(
        ref=ref,
        asset_type="node",
        bundle=bundle,
        path=path,
        metadata={"builtin": True} if builtin else {},
    )
    return cat


def _wf_with_catalog_node(*, ref: str) -> Workflow:
    return _wf(
        {
            "nodes": {"n": {"type": ref}},
            "graph": {"edges": []},
        }
    )


_GOOD_NODE_SOURCE = """
from pydantic import BaseModel
from workflows import node, NodeContext

class Inputs(BaseModel):
    msg: str

class Outputs(BaseModel):
    echoed: str

@node()
def run(inputs: Inputs, ctx: NodeContext) -> Outputs:
    return Outputs(echoed=inputs.msg)
"""


class TestCatalogNodeDefIntrospection:
    """AC 5.6 — every §7.6 failure mode raises the right exception."""

    def test_happy_path_clean(self, tmp_path: Path) -> None:
        node_file = tmp_path / "echo.py"
        node_file.write_text(_GOOD_NODE_SOURCE)
        cat = _seed_catalog_with_node(bundle="acme", name="echo", path=str(node_file))
        wf = _wf_with_catalog_node(ref="acme/nodes/echo")
        result = validate_workflow(wf, cat, file="wf.yaml")
        assert result.is_valid, [type(e).__name__ for e in result.errors]

    def test_builtin_entry_is_skipped(self, tmp_path: Path) -> None:
        """The 14 reserved ``kolsch/nodes/<name>`` slots route to their
        specific Pydantic *NodeDef classes (BashNodeDef, MapNodeDef,
        etc.) at parse time, NOT CatalogNodeDef — so §7.6 never even
        loops over them. This test exercises the defensive
        ``metadata['builtin']=True`` skip on a non-reserved ref whose
        catalog entry happens to be marked builtin (e.g. a future
        bundle that injects its own virtual entries the same way the
        kolsch built-ins do)."""
        cat = _seed_catalog_with_node(
            bundle="acme", name="virtual", path="", builtin=True
        )
        wf = _wf_with_catalog_node(ref="acme/nodes/virtual")
        result = validate_workflow(wf, cat, file="wf.yaml")
        assert result.is_valid, [type(e).__name__ for e in result.errors]

    def test_import_failed_raises(self, tmp_path: Path) -> None:
        node_file = tmp_path / "broken.py"
        node_file.write_text("def this is not valid python(\n")
        cat = _seed_catalog_with_node(bundle="acme", name="broken", path=str(node_file))
        wf = _wf_with_catalog_node(ref="acme/nodes/broken")
        with pytest.raises(WorkflowNodeImportFailed):
            validate_workflow_or_raise(wf, cat, file="wf.yaml")

    def test_missing_node_in_catalog_raises_import_failed(self, tmp_path: Path) -> None:
        """A CatalogNodeDef whose ref isn't in the catalog surfaces as
        WorkflowNodeImportFailed (the §7.6 check can't proceed)."""
        cat = AssetCatalog()
        wf = _wf_with_catalog_node(ref="acme/nodes/missing")
        result = validate_workflow(wf, cat, file="wf.yaml")
        assert any(isinstance(e, WorkflowNodeImportFailed) for e in result.errors)

    def test_missing_run_raises(self, tmp_path: Path) -> None:
        node_file = tmp_path / "no_run.py"
        node_file.write_text("def other(): pass\n")
        cat = _seed_catalog_with_node(bundle="acme", name="no_run", path=str(node_file))
        wf = _wf_with_catalog_node(ref="acme/nodes/no_run")
        with pytest.raises(WorkflowNodeMissingRun):
            validate_workflow_or_raise(wf, cat, file="wf.yaml")

    def test_undecorated_run_raises(self, tmp_path: Path) -> None:
        node_file = tmp_path / "undecorated.py"
        node_file.write_text("def run(inputs, ctx):\n    return None\n")
        cat = _seed_catalog_with_node(
            bundle="acme", name="undecorated", path=str(node_file)
        )
        wf = _wf_with_catalog_node(ref="acme/nodes/undecorated")
        with pytest.raises(WorkflowNodeNotDecorated):
            validate_workflow_or_raise(wf, cat, file="wf.yaml")

    def test_bad_signature_too_few_params_raises(self, tmp_path: Path) -> None:
        node_file = tmp_path / "thin.py"
        node_file.write_text(
            "from workflows import node\n@node()\ndef run(only_one): return None\n"
        )
        cat = _seed_catalog_with_node(bundle="acme", name="thin", path=str(node_file))
        wf = _wf_with_catalog_node(ref="acme/nodes/thin")
        with pytest.raises(WorkflowNodeSignatureInvalid):
            validate_workflow_or_raise(wf, cat, file="wf.yaml")

    def test_bad_signature_wrong_ctx_type_raises(self, tmp_path: Path) -> None:
        node_file = tmp_path / "wrong_ctx.py"
        node_file.write_text(
            "from pydantic import BaseModel\n"
            "from workflows import node\n"
            "class I(BaseModel):\n    msg: str\n"
            "@node()\n"
            "def run(i: I, ctx: str) -> I:\n"
            "    return i\n"
        )
        cat = _seed_catalog_with_node(
            bundle="acme", name="wrong_ctx", path=str(node_file)
        )
        wf = _wf_with_catalog_node(ref="acme/nodes/wrong_ctx")
        with pytest.raises(WorkflowNodeSignatureInvalid):
            validate_workflow_or_raise(wf, cat, file="wf.yaml")

    def test_bad_return_type_raises(self, tmp_path: Path) -> None:
        node_file = tmp_path / "bad_ret.py"
        node_file.write_text(
            "from pydantic import BaseModel\n"
            "from workflows import node, NodeContext\n"
            "class I(BaseModel):\n    msg: str\n"
            "@node()\n"
            "def run(i: I, ctx: NodeContext) -> str:\n"
            "    return i.msg\n"
        )
        cat = _seed_catalog_with_node(
            bundle="acme", name="bad_ret", path=str(node_file)
        )
        wf = _wf_with_catalog_node(ref="acme/nodes/bad_ret")
        with pytest.raises(WorkflowNodeReturnTypeInvalid):
            validate_workflow_or_raise(wf, cat, file="wf.yaml")

    def test_tuple_return_type_accepted(self, tmp_path: Path) -> None:
        node_file = tmp_path / "tuple_ret.py"
        node_file.write_text(
            "from pydantic import BaseModel\n"
            "from workflows import node, NodeContext\n"
            "class I(BaseModel):\n    msg: str\n"
            "class O(BaseModel):\n    out: str\n"
            "@node()\n"
            "def run(i: I, ctx: NodeContext) -> tuple[bool, O]:\n"
            "    return True, O(out=i.msg)\n"
        )
        cat = _seed_catalog_with_node(
            bundle="acme", name="tuple_ret", path=str(node_file)
        )
        wf = _wf_with_catalog_node(ref="acme/nodes/tuple_ret")
        result = validate_workflow(wf, cat, file="wf.yaml")
        assert result.is_valid, [type(e).__name__ for e in result.errors]


# ---------------------------------------------------------------------------
# AC 5.12 — "Related issues" tail across layers.
# ---------------------------------------------------------------------------


class TestRelatedIssuesTail:
    """AC 5.12 — when more than one validation layer contributes an
    error, the raise wrapper attaches a single ``__notes__`` line
    summarising the cross-layer findings."""

    def test_single_layer_no_tail(self) -> None:
        """When all errors come from one layer, no tail is added."""
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/bash", "command": "ls"},
                },
                "graph": {"edges": [{"from": "a", "to": "nonexistent"}]},
            }
        )
        try:
            validate_workflow_or_raise(wf, AssetCatalog(), file="t.yaml")
            pytest.fail("expected raise")
        except WorkflowNodeNotDefined as e:
            tail_notes = [
                n for n in getattr(e, "__notes__", []) if "Related issues" in n
            ]
            assert tail_notes == [], tail_notes

    def test_multi_layer_attaches_related_issues_note(self) -> None:
        """Structural + timeouts findings → tail naming both layers."""
        wf = _wf(
            {
                "timeout_seconds": -5,  # timeouts layer
                "nodes": {
                    "a": {"type": "kolsch/nodes/bash", "command": "a"},
                    "b": {"type": "kolsch/nodes/bash", "command": "b"},
                    "orphan": {"type": "kolsch/nodes/bash", "command": "x"},
                },
                # structural: edge a→b leaves 'orphan' with no incoming
                # edge and no path from the start, tripping
                # WorkflowUnreachableNode.
                "graph": {"edges": [{"from": "a", "to": "b"}]},
            }
        )
        result = validate_workflow(wf, AssetCatalog(), file="t.yaml")
        # Two distinct layers contributed: 'structural' (orphan) +
        # 'timeouts' (negative timeout).
        assert len(result.errors) >= 2
        layer_tags = {
            n.removeprefix("layer: ")
            for e in result.errors
            for n in getattr(e, "__notes__", [])
            if n.startswith("layer: ")
        }
        assert {"structural", "timeouts"}.issubset(layer_tags), layer_tags
        # _or_raise wraps in ExceptionGroup AND attaches the tail.
        with pytest.raises(ExceptionGroup) as ei:
            validate_workflow_or_raise(wf, AssetCatalog(), file="t.yaml")
        group_notes = getattr(ei.value, "__notes__", [])
        related = [n for n in group_notes if "Related issues" in n]
        assert len(related) == 1, group_notes
        assert "structural" in related[0]
        assert "timeouts" in related[0]

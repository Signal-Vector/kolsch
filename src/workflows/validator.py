"""Run-start workflow validation gate.

Covers structural validation plus:

- Cross-graph `nodes.<X>.<field>` pre-validation via Jinja-AST walk +
  graph-reachability predecessor closure.
- `WorkflowNodeNotDefined` pre-pass for unresolved YAML node-name
  references in `edge.from` / `edge.to` / `copy_of:` source /
  `map.apply:` target.
- `WorkflowBindCollision` when two edges to the same target bind the
  same key.
- `WorkflowInvalidTimeout` for non-positive `timeout_seconds` at
  workflow OR per-node scope.
- `WorkflowTimeoutShadowsNode` warning when a per-node timeout
  exceeds the workflow-level timeout.
- `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` /
  `WorkflowFromNotFound` — strict hard errors when `policy:` rules
  declare a malformed `from:` field.
- `WorkflowExtendsPolicyForbidden` when an extended workflow
  declares non-empty `policy:` rules.

**Public surface (dual).**

- `validate_workflow` returns
  [`ValidationResult`][workflows.validator.ValidationResult]. NEVER
  raises on validation failure (only on internal contract
  violations).
- `validate_workflow_or_raise` raises on errors (warnings are silent
  in the raise path). Single error → raises the specific
  [`WorkflowValidationError`][workflows.errors.WorkflowValidationError]
  subclass; multiple errors → raises stdlib
  `ExceptionGroup[WorkflowValidationError]`.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import typing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jinja2 import nodes as jinja_nodes
from pydantic import BaseModel

from workflows.catalog_node import CatalogNodeDef
from workflows.context import NodeContext
from workflows.errors import (
    WorkflowBindCollision,
    WorkflowBranchMissingArm,
    WorkflowBranchOnNonBranchable,
    WorkflowError,
    WorkflowExtendsPolicyForbidden,
    WorkflowFromAssetTypeError,
    WorkflowFromNotFound,
    WorkflowFromShapeError,
    WorkflowInvalidConcurrency,
    WorkflowInvalidTimeout,
    WorkflowJinjaRefUnknown,
    WorkflowMapperWorkerNotAddressable,
    WorkflowNodeImportFailed,
    WorkflowNodeMissingRun,
    WorkflowNodeNotDecorated,
    WorkflowNodeNotDefined,
    WorkflowNodeReturnTypeInvalid,
    WorkflowNodeSignatureInvalid,
    WorkflowParallelStateConflict,
    WorkflowTimeoutShadowsNode,
    WorkflowUnreachableNode,
    WorkflowValidationError,
    WorkflowValidationWarning,
)
from workflows.jinja_env import JINJA_ENV
from workflows.parser import (
    AgentNodeDef,
    BashNodeDef,
    BranchNodeDef,
    ComputeNodeDef,
    CopyOfNodeDef,
    DeleteStateNodeDef,
    EdgeDef,
    FilterNodeDef,
    ListFilesNodeDef,
    MapNodeDef,
    NoopNodeDef,
    ReadFileNodeDef,
    SetStateNodeDef,
    UserInputNodeDef,
    Workflow,
    WorkflowNodeDef,
    WriteFileNodeDef,
)

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog

__all__ = [
    "ValidationResult",
    "validate_workflow",
    "validate_workflow_or_raise",
]


# ---------------------------------------------------------------------------
# Output-schema table for built-in node types.
# ---------------------------------------------------------------------------


_BUILTIN_OUTPUT_FIELDS: dict[type, frozenset[str]] = {
    BashNodeDef: frozenset({"stdout", "stderr", "exit_code"}),
    NoopNodeDef: frozenset(),
    UserInputNodeDef: frozenset({"response"}),
    ReadFileNodeDef: frozenset({"content"}),
    WriteFileNodeDef: frozenset({"bytes_written"}),
    ListFilesNodeDef: frozenset({"entries"}),
    SetStateNodeDef: frozenset(),
    DeleteStateNodeDef: frozenset(),
    FilterNodeDef: frozenset({"result"}),
    MapNodeDef: frozenset({"results"}),
    BranchNodeDef: frozenset({"value"}),
    ComputeNodeDef: frozenset({"value"}),
}
"""Map from NodeDef class → known output keys.

Agent and sub-workflow nodes have variable output schemas (depend on
the resolved asset's declared outputs); they are treated as "open"
for the field-existence check — any field passes. `CatalogNodeDef`
(authored Python nodes) is similarly "open"; proper introspection
lands when the executor wires up the `@node`-decorated `run`
registration.
"""


_OPEN_OUTPUT_CLASSES: tuple[type, ...] = (
    AgentNodeDef,
    WorkflowNodeDef,
    CatalogNodeDef,
)


# ---------------------------------------------------------------------------
# ValidationResult.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Structured return shape from `validate_workflow`.

    The structured API NEVER raises on validation failure; callers
    inspect `errors` / `warnings` and decide what to do. The thin
    wrapper `validate_workflow_or_raise` adapts this into a
    raise-on-error surface for callers that prefer it.
    """

    errors: tuple[WorkflowValidationError, ...] = field(default_factory=tuple)
    warnings: tuple[WorkflowValidationWarning, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        """Return `True` iff no errors were collected.

        Warnings do NOT affect validity — they surface non-blocking
        advice.
        """
        return not self.errors


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------


_FILE_PLACEHOLDER = "<workflow>"
"""Source-file placeholder used by error constructors.

Applies when the validator runs against an in-memory `Workflow`
model. YAML-origin callers pass through `load_workflow` which
records the real path on its own error sites.
"""


# `run` MUST accept 2 or 3 positional params (Inputs, NodeContext,
# optional Config). Named constants here so the signature check reads
# intent rather than literal magic numbers (ruff PLR2004).
_MIN_RUN_PARAMS = 2
_MAX_RUN_PARAMS = 3
# The `tuple[bool, BaseModel]` return shape.
_TUPLE_RETURN_LEN = 2


def validate_workflow(
    workflow: Workflow,
    catalog: AssetCatalog,
    *,
    file: str = _FILE_PLACEHOLDER,
) -> ValidationResult:
    """Run every validation layer on `workflow` and return findings.

    **Layered execution.**

    1. `_check_extends_policy` — forbid `policy:` inheritance.
    2. `_check_structural` — pre-pass for unresolved node names; DAG
       + start + reachability; bind-key collision.
    3. `_check_branching`.
    4. `_check_jinja_refs` — graph-reachability predecessor closure;
       Jinja-AST walk on Jinja-marked strings.
    5. `_check_type_specific`.
    6. `_check_catalog_node_defs` — authored Python node
       introspection (import / decorator / signature / return-type).
    7. `_check_authz` — strict `from:` validation.
    8. `_check_timeouts` — positive int + shadowing warning.
    9. `_check_concurrency_invariants`.

    Within each layer, all findings accumulate; the layer never
    short-circuits. Across layers, run every layer in best-effort
    mode and accumulate; `validate_workflow_or_raise` adds the
    "Related issues" tail when raising.

    Args:
        workflow: The parsed workflow to validate.
        catalog: The asset catalog used for ref resolution.
        file: The source file path used in error messages.

    Returns:
        The accumulated [`ValidationResult`][workflows.validator.ValidationResult].
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    for layer_name, check in (
        ("extends-policy", _check_extends_policy),
        ("structural", _check_structural),
        ("branching", _check_branching),
        ("jinja-refs", _check_jinja_refs),
        ("type-specific", _check_type_specific),
        ("catalog-node-defs", _check_catalog_node_defs),
        ("authz", _check_authz),
        ("timeouts", _check_timeouts),
        ("concurrency", _check_concurrency_invariants),
    ):
        layer_errors, layer_warnings = check(
            workflow=workflow, catalog=catalog, file=file
        )
        # Tag each error with its originating layer via __notes__ so
        # raise consumers can group findings (AC 5.12). Notes do not
        # affect equality / type checks; tests using exc.exceptions
        # see the same instances regardless.
        for err in layer_errors:
            err.add_note(f"layer: {layer_name}")
        errors.extend(layer_errors)
        warnings.extend(layer_warnings)

    return ValidationResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def validate_workflow_or_raise(
    workflow: Workflow,
    catalog: AssetCatalog,
    *,
    file: str = _FILE_PLACEHOLDER,
) -> None:
    """Thin raise wrapper around `validate_workflow`.

    On a clean result, returns `None`. On a single error, raises the
    specific
    [`WorkflowValidationError`][workflows.errors.WorkflowValidationError]
    subclass. On multiple errors, raises stdlib `ExceptionGroup`.
    Warnings are silent in the raise path.

    **Related issues tail.** When more than one validation layer
    contributed an error, attaches a single `__notes__` line to the
    raised exception (or `ExceptionGroup`) summarising the other
    layers' findings. Layer tags are pre-attached during
    `validate_workflow` (each error carries a `layer: <name>` note);
    this wrapper rolls them up into one human-readable summary so
    single-error raises still tell the operator "by the way, there's
    also a schema error you'll hit next".

    Args:
        workflow: The parsed workflow to validate.
        catalog: The asset catalog used for ref resolution.
        file: The source file path used in error messages.

    Raises:
        WorkflowValidationError: A single validation finding.
        ExceptionGroup: Multiple validation findings.
    """
    result = validate_workflow(workflow, catalog, file=file)
    if not result.errors:
        return
    tail = _related_issues_tail(result.errors)
    if len(result.errors) == 1:
        single = result.errors[0]
        if tail is not None:
            single.add_note(tail)
        raise single
    group = ExceptionGroup(
        "Workflow validation failed",
        list(result.errors),
    )
    if tail is not None:
        group.add_note(tail)
    raise group


def _related_issues_tail(
    errors: tuple[WorkflowValidationError, ...],
) -> str | None:
    """Build the "Related issues" summary line.

    Walks the per-error `__notes__` attached during validation,
    counts how many findings each layer contributed, and returns a
    one-line summary when more than one layer fired. Returns `None`
    when all findings come from a single layer (no tail needed).
    """
    counts: dict[str, int] = {}
    for err in errors:
        for note in getattr(err, "__notes__", ()):
            if note.startswith("layer: "):
                layer = note.removeprefix("layer: ")
                counts[layer] = counts.get(layer, 0) + 1
                break
    if len(counts) <= 1:
        return None
    items = ", ".join(f"{layer} ({n})" for layer, n in sorted(counts.items()))
    return f"Related issues across layers: {items}."


# ---------------------------------------------------------------------------
# Extends + composition: forbid policy inheritance.
# ---------------------------------------------------------------------------


def _check_extends_policy(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Forbid `policy:` inheritance across `extends:`.

    An extended workflow's `policy:` does NOT merge across `extends:`.
    If ANY extended workflow (transitive) carries non-empty `policy:`
    rules, raise
    [`WorkflowExtendsPolicyForbidden`][workflows.errors.WorkflowExtendsPolicyForbidden].
    Cycle / depth detection is the engine's responsibility at extends
    resolution time; the validator only catches the policy-shadowing
    case here.
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    if not workflow.extends:
        return errors, warnings

    from pathlib import Path  # noqa: PLC0415

    from _yamlio import load  # noqa: PLC0415

    for ref in workflow.extends:
        entry = catalog.lookup(ref, expected_type="workflow")
        if entry is None:
            # The extends-unresolved class fires in the engine's
            # resolver. This layer is about policy-shadowing only;
            # skip unresolved refs and let the resolver catch them.
            continue
        try:
            raw = load(Path(entry.path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001, S112 — defensive; resolver re-raises
            continue
        if not isinstance(raw, dict):
            continue
        policy_rules = raw.get("policy") or []
        if isinstance(policy_rules, list) and policy_rules:
            errors.append(
                WorkflowExtendsPolicyForbidden(file=file, line=0, extended_ref=ref)
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# Structural checks.
# ---------------------------------------------------------------------------


def _check_structural(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Graph shape and bind-key collision."""
    _ = catalog  # unused at this layer; signature kept uniform.
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    node_names = set(workflow.nodes.keys())
    edges = list(workflow.graph.edges) if workflow.graph is not None else []

    # --- Pre-pass: every YAML node-name ref resolves --------------------
    # `edge.ref` is GONE; the trigger surfaces are edge.from / edge.to /
    # copy_of:source / map.apply:target.
    for idx, edge in enumerate(edges):
        if edge.from_ not in node_names:
            errors.append(
                WorkflowNodeNotDefined(
                    file=file,
                    line=idx,
                    ref=edge.from_,
                    location=f"edges[{idx}].from",
                )
            )
        if edge.to not in node_names:
            errors.append(
                WorkflowNodeNotDefined(
                    file=file,
                    line=idx,
                    ref=edge.to,
                    location=f"edges[{idx}].to",
                )
            )

    for node_name, node in workflow.nodes.items():
        if isinstance(node, CopyOfNodeDef) and node.copy_of not in node_names:
            errors.append(
                WorkflowNodeNotDefined(
                    file=file,
                    line=0,
                    ref=node.copy_of,
                    location=f"nodes.{node_name}.copy_of",
                )
            )
        if isinstance(node, MapNodeDef) and node.apply not in node_names:
            errors.append(
                WorkflowNodeNotDefined(
                    file=file,
                    line=0,
                    ref=node.apply,
                    location=f"nodes.{node_name}.apply",
                )
            )

    # Stop here if the pre-pass surfaced any unresolved name — downstream
    # checks would chase ghosts.
    if errors:
        return errors, warnings

    # --- Reachability: every defined node MUST be reachable from start
    # OR be a map.apply: target -----------------------------------------
    # When the workflow has no `graph:` (node library), reachability is
    # not enforced.
    if workflow.graph is None:
        return errors, warnings

    incoming: dict[str, list[str]] = {n: [] for n in node_names}
    outgoing: dict[str, list[str]] = {n: [] for n in node_names}
    for edge in edges:
        incoming[edge.to].append(edge.from_)
        outgoing[edge.from_].append(edge.to)

    map_apply_targets: set[str] = {
        node.apply for node in workflow.nodes.values() if isinstance(node, MapNodeDef)
    }
    copy_of_sources: set[str] = {
        node.copy_of
        for node in workflow.nodes.values()
        if isinstance(node, CopyOfNodeDef)
    }

    # The "real" start has zero incoming edges AND non-zero outgoing
    # edges. A node with zero of both (an orphan) is unreachable when
    # other nodes participate in the edge graph; without this guard,
    # the orphan would be its own BFS root and would falsely pass the
    # reachability check.
    if edges:
        real_starts = [n for n in node_names if not incoming[n] and outgoing[n]]
        if not real_starts:
            # Fallback: take every zero-incoming node (handles the
            # single-node workflow + odd single-edge shapes).
            real_starts = [n for n in node_names if not incoming[n]]
    else:
        # No edges at all — every node is trivially reachable.
        real_starts = list(node_names)

    reachable: set[str] = set()
    queue = list(real_starts)
    while queue:
        current = queue.pop()
        if current in reachable:
            continue
        reachable.add(current)
        queue.extend(outgoing.get(current, []))

    for node_name in node_names:
        if node_name in reachable:
            continue
        if node_name in map_apply_targets:
            continue  # map workers don't appear in the edge graph
        errors.append(WorkflowUnreachableNode(file=file, line=0, node=node_name))

    # ``copy_of:`` source must be graph-reachable (no template-only nodes).
    for node in workflow.nodes.values():
        if not isinstance(node, CopyOfNodeDef):
            continue
        if node.copy_of in copy_of_sources and node.copy_of not in reachable:
            # The source is referenced by a copy_of: but isn't itself
            # graph-reachable. Surface it as unreachable.
            errors.append(WorkflowUnreachableNode(file=file, line=0, node=node.copy_of))

    # --- Bind-key collision across multi-input edges --------------------
    edges_by_target: dict[str, list[EdgeDef]] = {n: [] for n in node_names}
    for edge in edges:
        edges_by_target.setdefault(edge.to, []).append(edge)
    for target, inbound in edges_by_target.items():
        if len(inbound) < 2:
            continue
        seen: dict[str, list[str]] = {}
        for edge in inbound:
            for bind_key in edge.binds:
                seen.setdefault(bind_key, []).append(edge.from_)
        for bind_key, sources in seen.items():
            if len(sources) > 1:
                errors.append(
                    WorkflowBindCollision(
                        file=file,
                        line=0,
                        node=target,
                        bind=bind_key,
                        sources=tuple(sources),
                    )
                )

    return errors, warnings


# ---------------------------------------------------------------------------
# Branching.
# ---------------------------------------------------------------------------


def _check_branching(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Branching invariants on branch-capable sources.

    The branchable set is `kolsch/nodes/branch`, `kolsch/nodes/bash`
    (the exit code drives dispatch), and authored Python `@node`
    modules whose introspected return annotation is `bool` or
    `tuple[bool, BaseModel]`. Authored Python whose return type is a
    plain `BaseModel` is NOT branchable — `branch:` edges from such
    nodes raise `WorkflowBranchOnNonBranchable`.

    `WorkflowBranchMissingArm` applies only to
    [`BranchNodeDef`][workflows.parser.BranchNodeDef] — the branch
    node type's contract requires both arms wired. Bash and authored
    Python may legitimately use only one arm (e.g. a guarded retry
    that prunes the failure path); the runtime treats the missing
    arm as "no edges fire" without complaint.
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    if workflow.graph is None:
        return errors, warnings

    branchable_names: set[str] = set()
    require_both_arms: set[str] = set()
    for name, node in workflow.nodes.items():
        if isinstance(node, BranchNodeDef):
            branchable_names.add(name)
            require_both_arms.add(name)
            continue
        if isinstance(node, BashNodeDef):
            branchable_names.add(name)
            continue
        if isinstance(node, CatalogNodeDef) and _is_branchable_catalog_node(
            node=node, catalog=catalog
        ):
            branchable_names.add(name)

    edges_by_source: dict[str, list[EdgeDef]] = {}
    for edge in workflow.graph.edges:
        edges_by_source.setdefault(edge.from_, []).append(edge)

    for source, outbound in edges_by_source.items():
        if source not in branchable_names:
            for edge in outbound:
                if edge.branch is not None:
                    errors.append(
                        WorkflowBranchOnNonBranchable(
                            file=file,
                            line=0,
                            from_node=source,
                            to_node=edge.to,
                        )
                    )

    for branch_node in require_both_arms:
        outbound = edges_by_source.get(branch_node, [])
        branch_values = {e.branch for e in outbound if e.branch is not None}
        for required in (True, False):
            if required not in branch_values:
                errors.append(
                    WorkflowBranchMissingArm(
                        file=file,
                        line=0,
                        node=branch_node,
                        missing_arm="true" if required else "false",
                    )
                )

    return errors, warnings


def _is_branchable_catalog_node(*, node: CatalogNodeDef, catalog: AssetCatalog) -> bool:
    """Return `True` iff this authored Python `@node` is branchable.

    "Branchable" means the introspected return annotation is `bool`
    or `tuple[bool, BaseModel]`. Defensive on every failure mode — a
    missing catalog entry, import failure, or unreadable signature
    returns `False` so the branching check defers to the per-node
    failure paths in `_check_catalog_node_defs`; the user gets the
    more specific diagnostic there.
    """
    entry = catalog.lookup(node.type, expected_type="node")
    if entry is None or not entry.path:
        return False
    if entry.metadata.get("builtin") is True:
        return False
    module = _import_node_module(entry.path)
    if isinstance(module, Exception):
        return False
    run_fn = getattr(module, "run", None)
    if run_fn is None or getattr(run_fn, "_kolsch_node", None) is None:
        return False
    try:
        hints = typing.get_type_hints(run_fn)
    except (TypeError, NameError):
        return False
    ret = hints.get("return")
    if ret is bool:
        return True
    origin = typing.get_origin(ret)
    args = typing.get_args(ret)
    return (
        origin is tuple
        and len(args) == _TUPLE_RETURN_LEN
        and args[0] is bool
        and isinstance(args[1], type)
        and issubclass(args[1], BaseModel)
    )


# ---------------------------------------------------------------------------
# Schema / cross-graph `nodes.<X>.<field>` pre-validation.
# ---------------------------------------------------------------------------


def _is_jinja_marked(value: Any) -> bool:
    """Return `True` iff `value` is a Jinja-marked string.

    A value is Jinja iff it is a string containing `"{{"`. Pure YAML
    literals (non-string, or string without that marker) skip Jinja
    parsing.
    """
    return isinstance(value, str) and "{{" in value


def _extract_nodes_refs(expression: str) -> list[tuple[str, str | None]]:
    """Return `(X, Y)` pairs for every `nodes.<X>.<Y>` access in `expression`.

    Parses via the singleton sandbox env. `Y` may be `None` when the
    access form is just `nodes.<X>` (rare but legal; treated as "the
    field is unspecified — pass").

    Returns an empty list if the expression fails to parse — the
    sandbox plus `JinjaSecurityError` surface is the executor's
    concern; we don't want a parse-time failure here to mask other
    validator findings.
    """
    try:
        ast = JINJA_ENV.parse(expression)
    except Exception:  # noqa: BLE001 — best-effort parse
        return []

    pairs: list[tuple[str, str | None]] = []
    nodes_x_y_sources: set[str] = set()
    for getattr_node in ast.find_all(jinja_nodes.Getattr):
        # Outer form: Getattr(node=Getattr(node=Name('nodes'), attr=X), attr=Y).
        outer_attr = getattr_node.attr
        inner = getattr_node.node
        if (
            isinstance(inner, jinja_nodes.Getattr)
            and isinstance(inner.node, jinja_nodes.Name)
            and inner.node.name == "nodes"
        ):
            pairs.append((inner.attr, outer_attr))
            nodes_x_y_sources.add(inner.attr)
            continue
        # Inner form alone: Getattr(node=Name('nodes'), attr=X) → (X, None).
        if (
            isinstance(getattr_node.node, jinja_nodes.Name)
            and getattr_node.node.name == "nodes"
        ):
            pairs.append((outer_attr, None))
    # Dedupe: when the same Jinja expression already produced a `(X,
    # Y)` pair, drop the inner `(X, None)` that
    # `Template.find_all` also surfaces. The bare `nodes.X` access is
    # only meaningful when no field traversal was authored.
    return [(x, y) for (x, y) in pairs if not (y is None and x in nodes_x_y_sources)]


def _output_field_set(node: Any) -> frozenset[str] | None:
    """Return the known output-field set for a node.

    Returns `None` when the schema is unconstrained (agent /
    sub-workflow / authored Python) and the field-existence check
    should pass-through.
    """
    if isinstance(node, _OPEN_OUTPUT_CLASSES):
        return None
    cls = type(node)
    return _BUILTIN_OUTPUT_FIELDS.get(cls)


def _check_jinja_refs(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Pre-validate `nodes.<X>.<field>` references in every Jinja-marked string field.

    Walks the Jinja AST via the singleton sandbox env; the
    predecessor relationship is computed from graph-reachability (NOT
    edge-level `ref:`, which no longer exists).
    """
    _ = catalog
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    if workflow.graph is None:
        return errors, warnings

    node_names = set(workflow.nodes.keys())
    map_apply_targets: set[str] = {
        node.apply for node in workflow.nodes.values() if isinstance(node, MapNodeDef)
    }

    # Predecessor closure via BFS back from each node along inbound edges.
    incoming: dict[str, list[str]] = {n: [] for n in node_names}
    for edge in workflow.graph.edges:
        if edge.from_ in node_names and edge.to in node_names:
            incoming[edge.to].append(edge.from_)
    predecessors: dict[str, set[str]] = {}
    for node_name in node_names:
        seen: set[str] = set()
        queue = list(incoming.get(node_name, []))
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(incoming.get(current, []))
        predecessors[node_name] = seen

    # --- Walk per-edge `binds:` + per-edge `when:` ---------------------
    for edge in workflow.graph.edges:
        # The parent special case applies: in an edge's binds:,
        # `nodes.<edge.from>.<field>` is allowed implicitly even
        # though the source isn't yet a "transitive ancestor" of the
        # target (binds is literally for parent→target wiring).
        implicit_parent = {edge.from_}
        target = workflow.nodes.get(edge.to)
        consuming_preds = predecessors.get(edge.to, set())
        for value in edge.binds.values():
            errors.extend(
                _check_value_for_refs(
                    value=value,
                    file=file,
                    consuming_node=edge.to,
                    consuming_preds=consuming_preds | implicit_parent,
                    node_names=node_names,
                    workflow=workflow,
                    map_apply_targets=map_apply_targets,
                )
            )
        if edge.when is not None:
            errors.extend(
                _check_value_for_refs(
                    value=edge.when,
                    file=file,
                    consuming_node=edge.to,
                    consuming_preds=consuming_preds | implicit_parent,
                    node_names=node_names,
                    workflow=workflow,
                    map_apply_targets=map_apply_targets,
                )
            )
        _ = target  # reserved for future field-typed-check work

    # --- Walk per-node config + when modifier --------------------------
    for node_name, node in workflow.nodes.items():
        node_preds = predecessors.get(node_name, set())
        # Common `when` modifier across all node types.
        when_value = getattr(node, "when", None)
        if when_value is not None:
            errors.extend(
                _check_value_for_refs(
                    value=when_value,
                    file=file,
                    consuming_node=node_name,
                    consuming_preds=node_preds,
                    node_names=node_names,
                    workflow=workflow,
                    map_apply_targets=map_apply_targets,
                )
            )
        # Per-type Jinja-evaluable fields.
        type_specific_fields = _type_specific_jinja_values(node)
        for value in type_specific_fields:
            errors.extend(
                _check_value_for_refs(
                    value=value,
                    file=file,
                    consuming_node=node_name,
                    consuming_preds=node_preds,
                    node_names=node_names,
                    workflow=workflow,
                    map_apply_targets=map_apply_targets,
                )
            )

    return errors, warnings


def _type_specific_jinja_values(node: Any) -> list[Any]:
    """Per-type Jinja-evaluable fields that should be ref-checked."""
    if isinstance(node, BashNodeDef):
        return [node.command]
    if isinstance(node, ComputeNodeDef):
        return [node.value]
    if isinstance(node, BranchNodeDef):
        return [node.condition]
    if isinstance(node, FilterNodeDef):
        return [node.over, node.condition]
    if isinstance(node, MapNodeDef):
        return [node.over]
    if isinstance(node, SetStateNodeDef):
        return list(node.keys.values())
    if isinstance(node, ReadFileNodeDef):
        return [node.path]
    if isinstance(node, WriteFileNodeDef):
        return [node.path, node.content if isinstance(node.content, str) else ""]
    if isinstance(node, ListFilesNodeDef):
        return [node.path]
    if isinstance(node, UserInputNodeDef):
        return [node.prompt]
    return []


def _check_value_for_refs(
    *,
    value: Any,
    file: str,
    consuming_node: str,
    consuming_preds: set[str],
    node_names: set[str],
    workflow: Workflow,
    map_apply_targets: set[str],
) -> list[WorkflowValidationError]:
    """Inspect `value` for `nodes.<X>.<Y>` refs and return any findings.

    Only Jinja-marked strings are walked; pure YAML literals pass
    through.

    **Check order.**

    1. `X` exists in `workflow.nodes`? → `WorkflowNodeNotDefined`.
    2. `X` in consumer's predecessor closure →
       `WorkflowJinjaRefUnknown(reason="not_predecessor")`. The
       `binds:` parent special case is folded into `consuming_preds`
       upstream.
    3. `X` is a map worker referenced from outside the map →
       `WorkflowMapperWorkerNotAddressable`.
    4. `X`'s output schema is known? → if not,
       `WorkflowJinjaRefUnknown(reason="not_declared")`.
    5. `Y` in `X`'s output fields? → if not,
       `WorkflowJinjaRefUnknown(reason="unknown_field")`.
    """
    findings: list[WorkflowValidationError] = []
    if not _is_jinja_marked(value):
        return findings
    pairs = _extract_nodes_refs(str(value))
    for x_name, y_field in pairs:
        if x_name not in node_names:
            findings.append(
                WorkflowNodeNotDefined(
                    file=file,
                    line=0,
                    ref=x_name,
                    location=f"nodes.{consuming_node}",
                )
            )
            continue
        if x_name in map_apply_targets and not _is_inside_same_map(
            workflow, consuming_node, x_name
        ):
            findings.append(
                WorkflowMapperWorkerNotAddressable(
                    file=file,
                    line=0,
                    node=consuming_node,
                    worker=x_name,
                )
            )
            continue
        if x_name not in consuming_preds:
            findings.append(
                WorkflowJinjaRefUnknown(
                    file=file,
                    line=0,
                    node=consuming_node,
                    reference=f"nodes.{x_name}.{y_field or '*'}",
                    reason="not_predecessor",
                )
            )
            continue
        if y_field is None:
            continue  # bare `nodes.X` access — pass.
        source_node = workflow.nodes[x_name]
        output_fields = _output_field_set(source_node)
        if output_fields is None:
            # Open schema (agent / sub-workflow / CatalogNodeDef). For
            # CatalogNodeDef we can't introspect the .py at v1 — flag
            # as not_declared so authors know the field is unverified.
            if isinstance(source_node, CatalogNodeDef):
                findings.append(
                    WorkflowJinjaRefUnknown(
                        file=file,
                        line=0,
                        node=consuming_node,
                        reference=f"nodes.{x_name}.{y_field}",
                        reason="not_declared",
                    )
                )
            continue
        if y_field not in output_fields:
            findings.append(
                WorkflowJinjaRefUnknown(
                    file=file,
                    line=0,
                    node=consuming_node,
                    reference=f"nodes.{x_name}.{y_field}",
                    reason="unknown_field",
                )
            )
    return findings


def _is_inside_same_map(
    workflow: Workflow, _consuming_node: str, worker_name: str
) -> bool:
    """Return `True` iff `consuming_node` is the map that targets `worker_name`.

    Worker references are only legal from the same `map` node (which
    targets the worker via `apply:`).
    """
    for node in workflow.nodes.values():
        if isinstance(node, MapNodeDef) and node.apply == worker_name:
            # consuming_node must BE this map node — workers don't have
            # their own siblings in the engine graph.
            return False  # external reference is the only access path
    return False


# ---------------------------------------------------------------------------
# Type-specific checks.
# ---------------------------------------------------------------------------


def _check_type_specific(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Per-type sanity beyond shape parsing.

    Per-type shape is delegated to the parser's Pydantic models (each
    `*NodeDef` class encodes the required fields). Future per-type
    checks land here.
    """
    _ = catalog
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []
    # No additional type-specific checks; the parser covers shape.
    # Future QoL: tighten built-in input schemas + cross-reference
    # subworkflow output keys.
    _ = workflow
    _ = file
    return errors, warnings


# ---------------------------------------------------------------------------
# Authored Python node introspection.
# ---------------------------------------------------------------------------


def _check_catalog_node_defs(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Authored Python node introspection.

    For every
    [`CatalogNodeDef`][workflows.catalog_node.CatalogNodeDef] in
    `workflow.nodes` whose catalog entry is NOT a virtual built-in
    (`metadata["builtin"]` is not `True`):

    1. Resolve the catalog entry via
       [`AssetCatalog.lookup`][assets.catalog.AssetCatalog.lookup]
       with `expected_type="node"`.
    2. Import the entry's `path` via `importlib.util` — failure
       (missing file, `SyntaxError`, `ImportError` etc.) raises
       `WorkflowNodeImportFailed`.
    3. The module MUST expose a top-level `run` symbol; otherwise
       `WorkflowNodeMissingRun`.
    4. `run` MUST be decorated with `@node` (sentinel `_kolsch_node`
       set by `workflows.decorator`); otherwise
       `WorkflowNodeNotDecorated`.
    5. `run`'s signature MUST match the authored-node contract (param
       1 is a `pydantic.BaseModel` subclass; param 2 is
       [`NodeContext`][workflows.context.NodeContext] exactly;
       optional param 3 is a `BaseModel` subclass; no further
       parameters); otherwise `WorkflowNodeSignatureInvalid`.
    6. `run`'s return annotation MUST be one of `BaseModel` subclass,
       `bool`, or `tuple[bool, BaseModel subclass]`; otherwise
       `WorkflowNodeReturnTypeInvalid`.

    Built-in entries (`kolsch/nodes/<name>`) are skipped — their
    Pydantic implementations live in `workflows.parser` and need no
    on-disk introspection. Entries whose `path` is empty are also
    skipped defensively (the same marker the catalog uses for
    virtuals).

    Secret-existence validation is deferred until the workflow gate
    threads a secrets provider; the validator only sees
    `secrets_needed` as a list of names with no resolver to ask.
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    for node_name, node in workflow.nodes.items():
        if not isinstance(node, CatalogNodeDef):
            continue
        ref = node.type
        entry = catalog.lookup(ref, expected_type="node")
        if entry is None:
            # The catalog has no node entry for this ref — surface as
            # `WorkflowNodeImportFailed` since the check can't proceed
            # without the catalog-resolved path. (The catalog scan
            # honors `WorkflowBuiltinShadowed` for reserved slots;
            # reaching this branch with a `None` entry means the ref
            # is genuinely missing from the catalog.)
            errors.append(
                WorkflowNodeImportFailed(
                    file=file,
                    line=0,
                    ref=ref,
                    reason=f"node {ref!r} is not in the catalog",
                )
            )
            continue
        if entry.metadata.get("builtin") is True or not entry.path:
            continue
        module = _import_node_module(entry.path)
        if isinstance(module, Exception):
            errors.append(
                WorkflowNodeImportFailed(file=file, line=0, ref=ref, reason=str(module))
            )
            continue
        run_fn = getattr(module, "run", None)
        if run_fn is None:
            errors.append(WorkflowNodeMissingRun(file=file, line=0, ref=ref))
            continue
        if getattr(run_fn, "_kolsch_node", None) is None:
            errors.append(WorkflowNodeNotDecorated(file=file, line=0, ref=ref))
            continue
        sig_error = _check_run_signature(file=file, ref=ref, run_fn=run_fn)
        if sig_error is not None:
            errors.append(sig_error)
            continue
        ret_error = _check_run_return_type(file=file, ref=ref, run_fn=run_fn)
        if ret_error is not None:
            errors.append(ret_error)
            continue
        _ = node_name  # reserved for future per-node error tagging

    return errors, warnings


def _import_node_module(path: str) -> Any:
    """Best-effort import the authored node module from `path`.

    Returns the loaded module on success or the original `Exception`
    (NOT a re-raise) on failure so the caller can fold the reason
    into `WorkflowNodeImportFailed`. Modules are loaded under a
    synthetic `_kolsch_node_<id>` name to avoid polluting
    `sys.modules` with bundle-internal namespaces; full cleanup
    discipline lives in the executor.

    Return type is `Any` (ANN401 acknowledged): the success branch
    yields `types.ModuleType` while the failure branch yields the
    captured `Exception`. Narrowing to a union here would leak
    type-checker noise to every caller; the caller's
    `isinstance(module, Exception)` discriminates cleanly.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            f"_kolsch_node_{abs(hash(path))}", path
        )
        if spec is None or spec.loader is None:
            return ImportError(f"could not load spec for {path!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (
        ImportError,
        SyntaxError,
        FileNotFoundError,
        AttributeError,
        TypeError,
        ValueError,
    ) as exc:
        return exc
    return module


# PLR0911 / ANN401 acknowledged: the signature check has five
# independent violation modes (parameter count, positional-only kinds,
# param-1 annotation, param-2 annotation, optional param-3
# annotation), each with an early-return for the SPECIFIC reason
# string. Collapsing into a single exit would lose the per-failure
# `reason` text. The `run_fn` parameter is intrinsically `Any` — it's
# a user-authored function we just inspect, not call.
def _check_run_signature(
    *,
    file: str,
    ref: str,
    run_fn: Any,
) -> WorkflowNodeSignatureInvalid | None:
    """Verify `run` matches the authored-node signature rules.

    Returns `None` on success or `WorkflowNodeSignatureInvalid` with
    a one-sentence reason on failure.
    """
    try:
        sig = inspect.signature(run_fn)
        hints = typing.get_type_hints(run_fn)
    except (TypeError, NameError) as exc:
        return WorkflowNodeSignatureInvalid(
            file=file, line=0, ref=ref, reason=f"unreadable signature: {exc}"
        )
    params = list(sig.parameters.values())
    if len(params) < _MIN_RUN_PARAMS or len(params) > _MAX_RUN_PARAMS:
        return WorkflowNodeSignatureInvalid(
            file=file,
            line=0,
            ref=ref,
            reason=(
                f"expected {_MIN_RUN_PARAMS} or {_MAX_RUN_PARAMS} "
                f"parameters, got {len(params)}"
            ),
        )
    for p in params:
        if p.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ):
            return WorkflowNodeSignatureInvalid(
                file=file,
                line=0,
                ref=ref,
                reason=f"parameter {p.name!r} must be positional",
            )
    inputs_ann = hints.get(params[0].name)
    if not isinstance(inputs_ann, type) or not issubclass(inputs_ann, BaseModel):
        return WorkflowNodeSignatureInvalid(
            file=file,
            line=0,
            ref=ref,
            reason=(
                f"param 1 ({params[0].name!r}) must be annotated with a "
                "pydantic.BaseModel subclass"
            ),
        )
    ctx_ann = hints.get(params[1].name)
    if ctx_ann is not NodeContext:
        return WorkflowNodeSignatureInvalid(
            file=file,
            line=0,
            ref=ref,
            reason=(
                f"param 2 ({params[1].name!r}) must be annotated exactly as NodeContext"
            ),
        )
    if len(params) == _MAX_RUN_PARAMS:
        config_ann = hints.get(params[2].name)
        if not isinstance(config_ann, type) or not issubclass(config_ann, BaseModel):
            return WorkflowNodeSignatureInvalid(
                file=file,
                line=0,
                ref=ref,
                reason=(
                    f"optional param 3 ({params[2].name!r}) must be a "
                    "pydantic.BaseModel subclass"
                ),
            )
    return None


def _check_run_return_type(
    *,
    file: str,
    ref: str,
    run_fn: Any,
) -> WorkflowNodeReturnTypeInvalid | None:
    """Verify `run`'s return annotation is one of the accepted shapes.

    Accepted: `BaseModel` subclass, `bool`, or `tuple[bool, BaseModel
    subclass]`.
    """
    try:
        hints = typing.get_type_hints(run_fn)
    except (TypeError, NameError):
        # Already caught by _check_run_signature in practice.
        return None
    ret_ann = hints.get("return")
    if ret_ann is None:
        return WorkflowNodeReturnTypeInvalid(
            file=file, line=0, ref=ref, return_type="<missing>"
        )
    if isinstance(ret_ann, type) and issubclass(ret_ann, BaseModel):
        return None
    if ret_ann is bool:
        return None
    origin = typing.get_origin(ret_ann)
    args = typing.get_args(ret_ann)
    if (
        origin is tuple
        and len(args) == _TUPLE_RETURN_LEN
        and args[0] is bool
        and isinstance(args[1], type)
        and issubclass(args[1], BaseModel)
    ):
        return None
    return WorkflowNodeReturnTypeInvalid(
        file=file, line=0, ref=ref, return_type=repr(ret_ann)
    )


# ---------------------------------------------------------------------------
# Policy / authz checks — strict `from:` validation.
# ---------------------------------------------------------------------------


def _check_authz(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Strict `from:` validation for the workflow's `policy:` rules.

    Uses the provider's compile path so the same
    `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` /
    `WorkflowFromNotFound` exception classes fire here as do at
    provider construction.
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    if not workflow.policy:
        return errors, warnings

    from providers.authz import RuleBasedAuthorizationProvider  # noqa: PLC0415

    for idx, rule in enumerate(workflow.policy):
        try:
            provider = RuleBasedAuthorizationProvider(
                config={
                    "rules": [
                        rule.model_dump(
                            by_alias=True,
                            exclude={"id", "who"} if not rule.is_legacy else set(),
                            exclude_none=True,
                        )
                    ]
                },
                catalog=catalog,
            )
            _ = provider
        except (
            WorkflowFromShapeError,
            WorkflowFromAssetTypeError,
            WorkflowFromNotFound,
        ) as exc:
            # Re-stamp with this workflow's file context. The provider's
            # ``_validate_from`` raises with ``file="<config>"`` —
            # the validator owns the workflow file context.
            new_exc = type(exc)(
                file=file,
                line=idx,
                from_ref=exc.from_ref,
                **(
                    {"asset_type": exc.asset_type}
                    if isinstance(exc, WorkflowFromAssetTypeError)
                    else {}
                ),
            )
            errors.append(new_exc)
        except WorkflowError:
            # Other rule-compile failures (malformed shape, unknown
            # what:, ambiguous shorthand) surface at provider construction
            # in the engine path; the validator's job here is from:
            # STRICT only.
            continue

    return errors, warnings


# ---------------------------------------------------------------------------
# Timeouts.
# ---------------------------------------------------------------------------


def _check_timeouts(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Positive-int validation plus per-node shadowing warning."""
    _ = catalog
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    wf_timeout = workflow.timeout_seconds
    if wf_timeout is not None and wf_timeout <= 0:
        errors.append(
            WorkflowInvalidTimeout(
                file=file,
                line=0,
                scope="workflow",
                value=float(wf_timeout),
            )
        )

    for node_name, node in workflow.nodes.items():
        node_timeout = getattr(node, "timeout_seconds", None)
        if node_timeout is None:
            continue
        if node_timeout <= 0:
            errors.append(
                WorkflowInvalidTimeout(
                    file=file,
                    line=0,
                    scope=f"node {node_name!r}",
                    value=float(node_timeout),
                )
            )
            continue
        if wf_timeout is not None and wf_timeout > 0 and node_timeout > wf_timeout:
            warnings.append(
                WorkflowTimeoutShadowsNode(
                    file=file,
                    line=0,
                    node=node_name,
                    node_timeout=float(node_timeout),
                    workflow_timeout=float(wf_timeout),
                )
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# Concurrency invariants.
# ---------------------------------------------------------------------------


# Conservative `state.<key>` reference scanner. Matches a literal
# `state.`-dot prefix followed by a Python identifier. Over-matches by
# design: any string containing the substring (a stray log line such
# as `"state.foo updated"`) WILL register as a state read. The gate's
# contract is to REJECT possibly-unsafe workflows; a false positive is
# on-spec, a false negative would be a correctness bug.
_STATE_REF_RE: re.Pattern[str] = re.compile(r"state\.([a-zA-Z_][a-zA-Z_0-9]*)")


def _collect_parallel_branch_groups(
    workflow: Workflow,
) -> list[tuple[str, list[set[str]]]]:
    """Decompose every fan-out point in `workflow` into its parallel branches.

    Returns one `(fan_out_node, branches)` entry per fan-out point. A
    fan-out point is a node with two or more *distinct* outgoing
    edges WHERE NONE carry a `branch:` selector (branch edges are
    exclusive — only one fires per dispatch — so they are NOT a
    concurrency surface).

    Each branch is the connected forward subgraph rooted at one of the
    fan-out's outgoing-edge targets, walked along non-branch edges
    only, stopping at the next confluence (a node with two or more
    non-branch incoming edges). The confluence node itself is
    EXCLUDED from the branch (the post-confluence subgraph belongs to
    a different analysis frame).

    Nested fan-outs are NOT collapsed here — they are surfaced as
    their own `(fan_out_node, branches)` entry, which gets its own
    pair-wise conflict pass.
    """
    if workflow.graph is None:
        return []

    node_names = set(workflow.nodes.keys())
    non_branch_outgoing: dict[str, set[str]] = {n: set() for n in node_names}
    non_branch_incoming_count: dict[str, int] = dict.fromkeys(node_names, 0)
    for edge in workflow.graph.edges:
        if edge.branch is not None:
            continue
        if edge.from_ not in node_names or edge.to not in node_names:
            continue
        if edge.to not in non_branch_outgoing[edge.from_]:
            non_branch_outgoing[edge.from_].add(edge.to)
            non_branch_incoming_count[edge.to] += 1

    groups: list[tuple[str, list[set[str]]]] = []
    for node_name in workflow.nodes:
        targets = non_branch_outgoing.get(node_name, set())
        if len(targets) < 2:
            continue
        branches = [
            _walk_parallel_branch(
                start=target,
                outgoing=non_branch_outgoing,
                incoming_count=non_branch_incoming_count,
            )
            for target in sorted(targets)
        ]
        groups.append((node_name, branches))
    return groups


def _walk_parallel_branch(
    *,
    start: str,
    outgoing: dict[str, set[str]],
    incoming_count: dict[str, int],
) -> set[str]:
    """BFS forward from `start` along non-branch edges.

    Stops descending through any node that is a confluence (non-branch
    `incoming_count > 1`) other than the starting node itself.
    """
    visited: set[str] = set()
    queue: list[str] = [start]
    while queue:
        node = queue.pop()
        if node in visited:
            continue
        # Confluence nodes terminate the branch (the branch ENDS at
        # the confluence; the confluence belongs to no branch).
        if node != start and incoming_count.get(node, 0) > 1:
            continue
        visited.add(node)
        for nxt in outgoing.get(node, set()):
            if nxt not in visited:
                queue.append(nxt)
    return visited


def _branch_state_write_set(workflow: Workflow, branch: set[str]) -> dict[str, str]:
    """Collect every `state.<key>` mutated by the branch.

    Returns a mapping `state_key -> node_name` so the gate can
    identify *which* node touched the conflicting key when raising
    `WorkflowParallelStateConflict`.

    Sequential maps (`parallel: false`) recurse into the worker node;
    parallel maps (`parallel: true`) are inspected separately by
    `_check_parallel_map_workers` and contribute no writes here (a
    parallel map that writes state was already rejected). Sub-workflow
    nodes are OPAQUE — child state is isolated, so a parent branch's
    write set does not include the child's writes.
    """
    writes: dict[str, str] = {}
    visited_workers: set[str] = set()

    def _collect_from_node(node_name: str) -> None:
        node = workflow.nodes.get(node_name)
        if node is None:
            return
        if isinstance(node, SetStateNodeDef):
            for key in node.keys:
                writes.setdefault(str(key), node_name)
            return
        if isinstance(node, DeleteStateNodeDef):
            for key in node.keys:
                writes.setdefault(str(key), node_name)
            return
        if isinstance(node, MapNodeDef):
            # Parallel maps that survive validation have no state
            # writes (the gate rejects them otherwise). Sequential
            # maps contribute their worker's writes transitively.
            if getattr(node, "parallel", False):
                return
            worker = node.apply
            if worker in visited_workers:
                return
            visited_workers.add(worker)
            _collect_from_node(worker)
            return
        # All other node types (bash, agent, sub-workflow, etc.) do
        # not mutate state at this layer. Sub-workflow opacity is
        # deliberate — its internal mutations belong to the child run,
        # not the parent.

    for node_name in branch:
        _collect_from_node(node_name)
    return writes


def _branch_state_read_set(workflow: Workflow, branch: set[str]) -> dict[str, str]:
    """Conservative scan for `state.<key>` references inside `branch`.

    Walks every Jinja-evaluable string field of every node in
    `branch`. Returns a mapping `state_key -> node_name` for the
    first node in the branch found to reference the key. Sequential
    maps recurse into their worker; sub-workflows are opaque (the
    child's reads don't leak to the parent's read set).
    """
    reads: dict[str, str] = {}
    visited_workers: set[str] = set()

    def _scan_value(value: Any, owner: str) -> None:
        if isinstance(value, str):
            for match in _STATE_REF_RE.finditer(value):
                reads.setdefault(match.group(1), owner)
            return
        if isinstance(value, list):
            for item in value:
                _scan_value(item, owner)
            return
        if isinstance(value, dict):
            for inner in value.values():
                _scan_value(inner, owner)

    def _scan_node(node_name: str) -> None:
        node = workflow.nodes.get(node_name)
        if node is None:
            return
        # Common ``when:`` modifier across all node types.
        when_value = getattr(node, "when", None)
        if when_value is not None:
            _scan_value(when_value, node_name)
        for value in _type_specific_jinja_values(node):
            _scan_value(value, node_name)
        # `SetState`'s `keys:` values are scanned by the type-specific
        # helper above; `DeleteState` only carries key names, not
        # values.
        if isinstance(node, MapNodeDef):
            if getattr(node, "parallel", False):
                return
            worker = node.apply
            if worker in visited_workers:
                return
            visited_workers.add(worker)
            _scan_node(worker)

    for node_name in branch:
        _scan_node(node_name)
    return reads


def _walk_worker_descendants(workflow: Workflow, start: str) -> list[str]:
    """Return the transitive worker subgraph rooted at `start`.

    Used by `_check_parallel_map_workers` to find state mutators
    inside a `parallel: true` map's worker chain. Sub-workflow nodes
    terminate the walk (their children are isolated; the gate does
    not peer into them). Sequential maps recurse into their own
    worker; parallel maps inside the chain are flagged separately by
    the outer pass.
    """
    visited: list[str] = []
    seen: set[str] = set()

    def _recurse(node_name: str) -> None:
        if node_name in seen:
            return
        seen.add(node_name)
        node = workflow.nodes.get(node_name)
        if node is None:
            return
        visited.append(node_name)
        if isinstance(node, MapNodeDef):
            _recurse(node.apply)
        # `WorkflowNodeDef` is opaque; do not recurse into `ref:`.

    _recurse(start)
    return visited


def _check_parallel_map_workers(
    *,
    workflow: Workflow,
    file: str,
) -> list[WorkflowValidationError]:
    """Reject `parallel: true` maps whose worker chain mutates state.

    Also rejects non-positive `concurrency:` integers with
    `WorkflowInvalidConcurrency`. Sequential maps and the
    `concurrency:` field on sequential maps are out of scope here.
    """
    errors: list[WorkflowValidationError] = []
    for node_name, node in workflow.nodes.items():
        if not isinstance(node, MapNodeDef):
            continue
        # Validate `concurrency:` positivity regardless of `parallel:`
        # — a `concurrency:` knob on a sequential map is meaningless
        # but harmless; a non-positive value is still a config error.
        if node.concurrency is not None and (
            not isinstance(node.concurrency, int)
            or isinstance(node.concurrency, bool)
            or node.concurrency <= 0
        ):
            errors.append(
                WorkflowInvalidConcurrency(
                    file=file,
                    line=0,
                    node=node_name,
                    value=node.concurrency,
                )
            )
        if not node.parallel:
            continue
        for descendant in _walk_worker_descendants(workflow, node.apply):
            descendant_node = workflow.nodes.get(descendant)
            if isinstance(descendant_node, (SetStateNodeDef, DeleteStateNodeDef)):
                # Name the first state key the descendant writes so
                # the error message points at the conflicting surface.
                keys_iter: Any = (
                    descendant_node.keys
                    if isinstance(descendant_node, DeleteStateNodeDef)
                    else list(descendant_node.keys)
                )
                first_key = next(iter(keys_iter), "<unknown>")
                errors.append(
                    WorkflowParallelStateConflict(
                        file=file,
                        line=0,
                        node_a=node_name,
                        node_b=descendant,
                        state_key=str(first_key),
                    )
                )
                break
    return errors


def _check_concurrency_invariants(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Run-start gate static analysis for concurrent execution safety.

    Three independent sub-passes; all findings accumulate (the layer
    never short-circuits):

    1. `parallel: true` map worker subgraph reaches a state mutator
       → `WorkflowParallelStateConflict`.
    2. `concurrency:` integer `<= 0` → `WorkflowInvalidConcurrency`.
    3. (`auto` mode only) sibling parallel branches whose state-write
       or state-read sets overlap → `WorkflowParallelStateConflict`.

    `sequential`-mode workflows skip the third sub-pass entirely —
    the branches do NOT execute concurrently, so no conflict is
    possible. Sub-passes 1 and 2 still run because they reject
    structural misuse independent of the workflow-level mode.
    """
    _ = catalog
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    # Sub-pass 1 + 2 — parallel map worker walk + positive concurrency.
    errors.extend(_check_parallel_map_workers(workflow=workflow, file=file))

    # Sub-pass 3 — sibling branch conflict, ``auto`` mode only.
    if workflow.concurrency != "auto":
        return errors, warnings
    if workflow.graph is None:
        return errors, warnings

    seen_pairs: set[tuple[str, str, str]] = set()
    for _fan_out_node, branches in _collect_parallel_branch_groups(workflow):
        # Pre-compute per-branch write/read sets once; pair-check
        # below. Each set is ``{state_key: owning_node_name}`` so the
        # raised error can name the actual offending node (not just
        # the branch root).
        sets: list[tuple[dict[str, str], dict[str, str]]] = [
            (
                _branch_state_write_set(workflow, branch),
                _branch_state_read_set(workflow, branch),
            )
            for branch in branches
        ]
        for i, (writes_a, reads_a) in enumerate(sets):
            for j in range(i + 1, len(sets)):
                writes_b, reads_b = sets[j]
                # Write-write, then write-read in each direction.
                for key, owner_a in writes_a.items():
                    if key in writes_b:
                        _record_conflict(
                            errors=errors,
                            seen=seen_pairs,
                            file=file,
                            node_a=owner_a,
                            node_b=writes_b[key],
                            state_key=key,
                        )
                    if key in reads_b:
                        _record_conflict(
                            errors=errors,
                            seen=seen_pairs,
                            file=file,
                            node_a=owner_a,
                            node_b=reads_b[key],
                            state_key=key,
                        )
                for key, owner_b in writes_b.items():
                    if key in reads_a:
                        _record_conflict(
                            errors=errors,
                            seen=seen_pairs,
                            file=file,
                            node_a=reads_a[key],
                            node_b=owner_b,
                            state_key=key,
                        )

    return errors, warnings


def _record_conflict(
    *,
    errors: list[WorkflowValidationError],
    seen: set[tuple[str, str, str]],
    file: str,
    node_a: str,
    node_b: str,
    state_key: str,
) -> None:
    """Append a `WorkflowParallelStateConflict` finding, deduped.

    The same pair-key combination only surfaces once even when the
    write-write and write-read passes both match.
    """
    pair_key = (
        node_a if node_a < node_b else node_b,
        node_b if node_a < node_b else node_a,
        state_key,
    )
    if pair_key in seen:
        return
    seen.add(pair_key)
    errors.append(
        WorkflowParallelStateConflict(
            file=file,
            line=0,
            node_a=node_a,
            node_b=node_b,
            state_key=state_key,
        )
    )

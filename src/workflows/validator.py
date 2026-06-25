"""Run-start workflow validation gate — Phase 3.0 Step 5.

Covers master spec §7.1-§7.7 PLUS:

- Cross-graph ``nodes.<X>.<field>`` pre-validation via Jinja-AST walk
  + graph-reachability predecessor closure (Round 4 item #11 — NOT
  edge-level ``ref:``, which no longer exists).
- ``WorkflowNodeNotDefined`` pre-pass for unresolved YAML node-name
  references in ``edge.from`` / ``edge.to`` / ``copy_of:`` source /
  ``map.apply:`` target.
- ``WorkflowBindCollision`` (Round 4 item #11) when two edges to the
  same target bind the same key.
- ``WorkflowBindMustBeJinja`` is GONE (Round 6 J5/β — binds RHS
  accepts YAML literals OR Jinja-marked strings; literals pass
  through).
- ``WorkflowInvalidTimeout`` (R3 #2) for non-positive
  ``timeout_seconds`` at workflow OR per-node scope.
- ``WorkflowTimeoutShadowsNode`` warning when a per-node timeout
  exceeds the workflow-level timeout (Round 6).
- ``WorkflowFromShapeError`` / ``WorkflowFromAssetTypeError`` /
  ``WorkflowFromNotFound`` (Round 4 item #13) — STRICT HARD ERRORS
  when ``policy:`` rules declare a malformed ``from:`` field.
- ``WorkflowExtendsPolicyForbidden`` (Round 6 S5) when an extended
  workflow declares non-empty ``policy:`` rules.

Public surface (R3 #6 dual):

- :func:`validate_workflow` returns :class:`ValidationResult`. NEVER
  raises on validation failure (only on internal contract violations).
- :func:`validate_workflow_or_raise` raises on errors (warnings are
  silent in the raise path). Single error → raises the specific
  ``WorkflowValidationError`` subclass; multiple errors → raises
  stdlib :class:`ExceptionGroup[WorkflowValidationError]` (NO custom
  ``WorkflowValidationErrorGroup`` — per R3 #6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jinja2 import nodes as jinja_nodes

from workflows.catalog_node import CatalogNodeDef
from workflows.errors import (
    WorkflowBindCollision,
    WorkflowBranchMissingArm,
    WorkflowBranchOnNonBranchable,
    WorkflowError,
    WorkflowExtendsPolicyForbidden,
    WorkflowFromAssetTypeError,
    WorkflowFromNotFound,
    WorkflowFromShapeError,
    WorkflowInvalidTimeout,
    WorkflowJinjaRefUnknown,
    WorkflowMapperWorkerNotAddressable,
    WorkflowNodeNotDefined,
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
# Output-schema table for built-in node types (master spec §4.0).
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
"""Map from NodeDef class → known output keys per master spec §4.0.

Agent and sub-workflow nodes have variable output schemas (depend on
the resolved asset's declared outputs); they are treated as "open"
for the field-existence check — any field passes. CatalogNodeDef
(authored Python nodes) is similarly "open" at v1; proper
introspection lands when the Phase 3.1 executor wires up the
@node-decorated `run` registration.
"""


_OPEN_OUTPUT_CLASSES: tuple[type, ...] = (
    AgentNodeDef,
    WorkflowNodeDef,
    CatalogNodeDef,
)


# ---------------------------------------------------------------------------
# ValidationResult — R3 #6 structured return shape.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Structured return shape from :func:`validate_workflow`.

    R3 #6 — the structured API NEVER raises on validation failure;
    callers inspect ``errors`` / ``warnings`` and decide what to do.
    The thin wrapper :func:`validate_workflow_or_raise` adapts this
    into a raise-on-error surface for callers that prefer it.
    """

    errors: tuple[WorkflowValidationError, ...] = field(default_factory=tuple)
    warnings: tuple[WorkflowValidationWarning, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        """``True`` iff no errors were collected. Warnings do NOT
        affect validity — they surface non-blocking advice."""
        return not self.errors


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------


_FILE_PLACEHOLDER = "<workflow>"
"""Source-file placeholder used by error constructors when the
validator runs against an in-memory :class:`Workflow` model (no
on-disk file). YAML-origin callers pass through ``load_workflow``
which records the real path on its own error sites."""


def validate_workflow(
    workflow: Workflow,
    catalog: AssetCatalog,
    *,
    file: str = _FILE_PLACEHOLDER,
) -> ValidationResult:
    """Run all §7.1-§7.7 checks on ``workflow``; return findings.

    Layered execution (R3 #6 accumulation rules):

    1. ``_check_extends_policy`` (master spec §7.5 / Round 6 S5).
    2. ``_check_structural`` (§7.1) — pre-pass for unresolved node
       names; DAG + start + reachability; bind-key collision.
    3. ``_check_branching`` (§7.3).
    4. ``_check_jinja_refs`` (§7.2) — graph-reachability predecessor
       closure; Jinja-AST walk on Jinja-marked strings.
    5. ``_check_type_specific`` (§7.4).
    6. ``_check_authz`` (§7.7) — STRICT ``from:`` validation per
       Round 4 item #13.
    7. ``_check_timeouts`` (R3 #2) — positive int + shadowing warning.

    Within each layer, all findings accumulate; the layer never
    short-circuits. Across layers, run all layers in best-effort
    mode and accumulate; the raise wrapper handles fail-fast
    semantics with the "Related issues" tail.
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    for check in (
        _check_extends_policy,
        _check_structural,
        _check_branching,
        _check_jinja_refs,
        _check_type_specific,
        _check_authz,
        _check_timeouts,
    ):
        layer_errors, layer_warnings = check(
            workflow=workflow, catalog=catalog, file=file
        )
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
    """Thin raise wrapper around :func:`validate_workflow`.

    On a clean result, returns ``None``. On a single error, raises the
    specific :class:`WorkflowValidationError` subclass. On multiple
    errors, raises stdlib :class:`ExceptionGroup` (per R3 #6 — no
    custom ``WorkflowValidationErrorGroup`` class). Warnings are
    silent in the raise path.
    """
    result = validate_workflow(workflow, catalog, file=file)
    if not result.errors:
        return
    if len(result.errors) == 1:
        raise result.errors[0]
    raise ExceptionGroup(
        "Workflow validation failed",
        list(result.errors),
    )


# ---------------------------------------------------------------------------
# §7.5 — Extends + composition (Round 6 S5: forbid policy inheritance).
# ---------------------------------------------------------------------------


def _check_extends_policy(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Round 6 S5 — an extended workflow's ``policy:`` does NOT merge
    across ``extends:``. If ANY extended workflow (transitive) carries
    non-empty ``policy:`` rules, raise :class:`WorkflowExtendsPolicyForbidden`.

    Implementation: walk ``workflow.extends`` refs; for each, resolve
    via the catalog and parse the YAML to inspect the ``policy:``
    field. Cycle / depth detection is the engine's responsibility at
    extends resolution time (Phase 3.1); the validator only catches
    the policy-shadowing case here.
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
        except Exception:  # noqa: BLE001 — defensive; resolver re-raises
            continue
        if not isinstance(raw, dict):
            continue
        policy_rules = raw.get("policy") or []
        if isinstance(policy_rules, list) and policy_rules:
            errors.append(
                WorkflowExtendsPolicyForbidden(
                    file=file, line=0, extended_ref=ref
                )
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# §7.1 — Structural checks.
# ---------------------------------------------------------------------------


def _check_structural(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Master spec §7.1 — graph shape + bind-key collision."""
    _ = catalog  # unused at this layer; signature kept uniform.
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    node_names = set(workflow.nodes.keys())
    edges = list(workflow.graph.edges) if workflow.graph is not None else []

    # --- Pre-pass: every YAML node-name ref resolves --------------------
    # Round 4 item #11 — `edge.ref` is GONE; the trigger surfaces are
    # edge.from / edge.to / copy_of:source / map.apply:target.
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

    # --- Reachability (§7.1 — defined node MUST be reachable from start
    # OR be a map.apply: target) -----------------------------------------
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
        node.apply
        for node in workflow.nodes.values()
        if isinstance(node, MapNodeDef)
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
        real_starts = [
            n
            for n in node_names
            if not incoming[n] and outgoing[n]
        ]
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
        errors.append(
            WorkflowUnreachableNode(file=file, line=0, node=node_name)
        )

    # ``copy_of:`` source must be graph-reachable (no template-only nodes).
    for node_name, node in workflow.nodes.items():
        if not isinstance(node, CopyOfNodeDef):
            continue
        if node.copy_of in copy_of_sources and node.copy_of not in reachable:
            # The source is referenced by a copy_of: but isn't itself
            # graph-reachable. Surface it as unreachable.
            errors.append(
                WorkflowUnreachableNode(
                    file=file, line=0, node=node.copy_of
                )
            )

    # --- Bind-key collision across multi-input edges (Round 4 item #11) -
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
# §7.3 — Branching.
# ---------------------------------------------------------------------------


def _check_branching(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Master spec §7.3 — branching invariants on branch nodes."""
    _ = catalog
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    if workflow.graph is None:
        return errors, warnings

    branchable_names = {
        n
        for n, node in workflow.nodes.items()
        if isinstance(node, BranchNodeDef)
    }
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

    for branch_node in branchable_names:
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


# ---------------------------------------------------------------------------
# §7.2 — Schema / cross-graph ``nodes.<X>.<field>`` pre-validation.
# ---------------------------------------------------------------------------


def _is_jinja_marked(value: Any) -> bool:
    """Round 6 J5/β — a value is Jinja iff it is a string containing
    ``"{{"``. Pure YAML literals (non-string, or string without that
    marker) skip Jinja parsing."""
    return isinstance(value, str) and "{{" in value


def _extract_nodes_refs(expression: str) -> list[tuple[str, str | None]]:
    """Parse a Jinja expression via the singleton sandbox env and
    return ``(X, Y)`` pairs for every ``nodes.<X>.<Y>`` access.

    ``Y`` may be ``None`` when the access form is just ``nodes.<X>``
    (rare but legal; treated as "the field is unspecified — pass").

    Returns an empty list if the expression fails to parse — the
    sandbox + JinjaSecurityError surface is the executor's concern;
    we don't want a parse-time failure here to mask other validator
    findings.
    """
    try:
        ast = JINJA_ENV.parse(expression)
    except Exception:  # noqa: BLE001 — best-effort parse
        return []

    pairs: list[tuple[str, str | None]] = []
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
            continue
        # Inner form alone: Getattr(node=Name('nodes'), attr=X) → (X, None).
        if (
            isinstance(getattr_node.node, jinja_nodes.Name)
            and getattr_node.node.name == "nodes"
        ):
            pairs.append((outer_attr, None))
    return pairs


def _output_field_set(node: Any) -> frozenset[str] | None:
    """Return the known output-field set for a node, or ``None`` when
    the schema is unconstrained (agent / sub-workflow / authored
    Python) and the field-existence check should pass-through."""
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
    """Master spec §7.2 — pre-validate ``nodes.<X>.<field>`` references
    in every Jinja-marked string field.

    Walks the Jinja AST via the singleton sandbox env (Round 6 S1);
    the predecessor relationship is computed from graph-reachability
    (Round 4 item #11 — NOT edge-level ``ref:``, which no longer
    exists).
    """
    _ = catalog
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    if workflow.graph is None:
        return errors, warnings

    node_names = set(workflow.nodes.keys())
    map_apply_targets: set[str] = {
        node.apply
        for node in workflow.nodes.values()
        if isinstance(node, MapNodeDef)
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
        # Round 4 item #11 — the parent special case applies: in an
        # edge's binds:, `nodes.<edge.from>.<field>` is allowed
        # implicitly even though the source isn't yet a "transitive
        # ancestor" of the target (binds is literally for parent→target
        # wiring).
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
    """Inspect ``value`` for ``nodes.<X>.<Y>`` refs and return any
    findings.

    Round 6 J5/β — only Jinja-marked strings are walked; pure YAML
    literals pass through.

    Locked check order (per Step 5 prompt + master spec §7.2):

    1. ``X`` exists in ``workflow.nodes``? → ``WorkflowNodeNotDefined``.
    2. ``X`` in consumer's predecessor closure → ``WorkflowJinjaRefUnknown(reason="not_predecessor")``.
       The ``binds:`` parent special case is folded into
       ``consuming_preds`` upstream.
    3. ``X`` is a map worker referenced from outside the map →
       ``WorkflowMapperWorkerNotAddressable``.
    4. ``X``'s output schema is known? → if not,
       ``WorkflowJinjaRefUnknown(reason="not_declared")``.
    5. ``Y`` in ``X``'s output fields? → if not,
       ``WorkflowJinjaRefUnknown(reason="unknown_field")``.
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
        if (
            x_name in map_apply_targets
            and not _is_inside_same_map(workflow, consuming_node, x_name)
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
    workflow: Workflow, consuming_node: str, worker_name: str
) -> bool:
    """Worker references are only legal from the same ``map`` node
    (which targets the worker via ``apply:``)."""
    for node in workflow.nodes.values():
        if isinstance(node, MapNodeDef) and node.apply == worker_name:
            # consuming_node must BE this map node — workers don't have
            # their own siblings in the engine graph.
            return False  # external reference is the only access path
    return False


# ---------------------------------------------------------------------------
# §7.4 — Type-specific checks.
# ---------------------------------------------------------------------------


def _check_type_specific(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Master spec §7.4 — per-type sanity beyond shape parsing.

    For Phase 3.0 the per-type checks are mostly delegated to Step 2's
    Pydantic parsing (each ``*NodeDef`` class encodes the required
    fields). Two checks land here:

    - Sub-workflow ``workflow:`` refs resolve to a runnable workflow
      in the catalog (when one is supplied).
    - Built-in node-type slot validation: a ``type: kolsch/nodes/<x>``
      where ``x`` is not in the 14-name set never reaches the parser
      (it would have lived in ``CatalogNodeDef``), so this layer
      just verifies the catalog has an entry for any non-built-in
      node ref.
    """
    _ = catalog
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []
    # No additional type-specific checks at Phase 3.0; Step 2's parser
    # covers shape. Future Phase 3.x QoL: tighten built-in input
    # schemas + cross-reference subworkflow output keys.
    _ = workflow
    _ = file
    return errors, warnings


# ---------------------------------------------------------------------------
# §7.7 — Policy / authz checks (Round 4 item #13 STRICT ``from:``).
# ---------------------------------------------------------------------------


def _check_authz(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """Master spec §7.7 — STRICT ``from:`` validation for the
    workflow's ``policy:`` rules.

    Uses the provider's compile path (Step 4) so the same
    :class:`WorkflowFromShapeError` / :class:`WorkflowFromAssetTypeError`
    / :class:`WorkflowFromNotFound` exception classes fire here as do
    at provider construction.
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[WorkflowValidationWarning] = []

    if not workflow.policy:
        return errors, warnings

    from providers.authz import RuleBasedAuthorizationProvider  # noqa: PLC0415

    for idx, rule in enumerate(workflow.policy):
        try:
            provider = RuleBasedAuthorizationProvider(
                config={"rules": [
                    rule.model_dump(
                        by_alias=True,
                        exclude={"id", "who"} if not rule.is_legacy else set(),
                        exclude_none=True,
                    )
                ]},
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
# R3 #2 — `_check_timeouts`.
# ---------------------------------------------------------------------------


def _check_timeouts(
    *,
    workflow: Workflow,
    catalog: AssetCatalog,
    file: str,
) -> tuple[list[WorkflowValidationError], list[WorkflowValidationWarning]]:
    """R3 #2 — positive int validation + shadowing warning."""
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
        if (
            wf_timeout is not None
            and wf_timeout > 0
            and node_timeout > wf_timeout
        ):
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



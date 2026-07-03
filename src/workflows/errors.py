"""Workflow exception hierarchy.

Every class follows these conventions:

- Structured attributes via `__init__` keyword args (each one becomes
  `self.<attr>`). NO error codes; NO enum lookups.
- `__str__()` is STRICT single-line. YAML-origin classes embed
  `<file>:<line>` + identifying names + the literal token `"Fix: "`
  (capital F, colon, space).
- `caller_message: ClassVar[str]` — a 1-3 sentence plain-English
  statement for non-engineer end users. Surfaced by chat capsules
  and the CLI's `--format json` envelope. NO class names, file/line
  tokens, Jinja delimiters, or backticks.
- `identifying_names_keys: ClassVar[tuple[str, ...]]` — listed keys
  become the JSON envelope's `identifying_names: {...}` dict (the
  rest fall into `attrs`).
- Wrapped causes go in `__cause__` via `raise X(...) from y`.
- Multi-error reporting uses stdlib `ExceptionGroup`; NO custom
  group class.

The single-line format for YAML-origin errors:

```text
<ClassName>: <file>:<line> <identifying-names>: <problem>. Fix: <action>.
```

Fix-portion strict-prose rules: NO `{{` / `}}` / `{%` / `%}`, NO
backticks, NO YAML literal syntax. Single-quoted identifiers and
prose vocabulary only; cross-reference exact syntax via `see
docs/workflows#<anchor>`.
"""

from __future__ import annotations

from typing import Any, ClassVar

__all__ = [
    "BashNonZeroExit",
    "ChildWorkflowFailed",
    "NodeTimeoutExceeded",
    "NodeTypeNotImplemented",
    "WorkflowAmbiguousStart",
    "WorkflowBindCollision",
    "WorkflowBindTypeMismatch",
    "WorkflowBranchMissingArm",
    "WorkflowBranchOnNonBranchable",
    "WorkflowBuiltinShadowed",
    "WorkflowCallCycle",
    "WorkflowDuplicateEdge",
    "WorkflowDuplicateNode",
    "WorkflowError",
    "WorkflowExtendsCycle",
    "WorkflowExtendsDepthExceeded",
    "WorkflowExtendsPolicyForbidden",
    "WorkflowExtendsUnresolved",
    "WorkflowFileAccessDenied",
    "WorkflowFromAssetTypeError",
    "WorkflowFromNotFound",
    "WorkflowFromShapeError",
    "WorkflowInputInvalid",
    "WorkflowInvalidConcurrency",
    "WorkflowInvalidRef",
    "WorkflowInvalidTimeout",
    "WorkflowJinjaRefUnknown",
    "WorkflowJinjaSecurityError",
    "WorkflowMapperWorkerMissingField",
    "WorkflowMapperWorkerNotAddressable",
    "WorkflowNoStartNode",
    "WorkflowNodeFailed",
    "WorkflowNodeImportFailed",
    "WorkflowNodeMissingRun",
    "WorkflowNodeNotDecorated",
    "WorkflowNodeNotDefined",
    "WorkflowNodeReturnTypeInvalid",
    "WorkflowNodeSignatureInvalid",
    "WorkflowNotRunnable",
    "WorkflowParallelStateConflict",
    "WorkflowPausedForInput",
    "WorkflowResumeRejected",
    "WorkflowRunNotFound",
    "WorkflowRuntimeError",
    "WorkflowSecretUnresolved",
    "WorkflowTimeoutExceeded",
    "WorkflowTimeoutShadowsNode",
    "WorkflowUnreachableNode",
    "WorkflowValidationError",
    "WorkflowValidationWarning",
]


# ---------------------------------------------------------------------------
# Base + format envelope
# ---------------------------------------------------------------------------


class WorkflowError(Exception):
    """Root of the workflow exception hierarchy."""

    caller_message: ClassVar[str] = (
        "Something went wrong running the workflow. "
        "Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ()


def _identifying_names(exc: WorkflowError) -> str:
    """Render the identifying-names segment of the canonical line."""
    parts: list[str] = []
    for key in exc.identifying_names_keys:
        if not hasattr(exc, key):
            continue
        parts.append(f"{key}={getattr(exc, key)!r}")
    return ", ".join(parts)


def _yaml_origin_str(
    exc: WorkflowError,
    *,
    file: str,
    line: int,
    problem: str,
    fix: str,
) -> str:
    """Canonical single-line YAML-origin format."""
    ids = _identifying_names(exc)
    prefix = f"{type(exc).__name__}: {file}:{line}"
    if ids:
        prefix = f"{prefix} {ids}"
    return f"{prefix}: {problem}. Fix: {fix}."


def _runtime_str(
    exc: WorkflowError,
    *,
    problem: str,
) -> str:
    """Single-line runtime format (no file:line, no Fix: token required)."""
    ids = _identifying_names(exc)
    prefix = type(exc).__name__
    if ids:
        prefix = f"{prefix} {ids}"
    return f"{prefix}: {problem}"


# ---------------------------------------------------------------------------
# Validation-error tree
# ---------------------------------------------------------------------------


class WorkflowValidationError(WorkflowError):
    """Run-start gate finding. Subclasses MUST render YAML-origin format."""

    caller_message: ClassVar[str] = (
        "The workflow file has a problem. Contact your engineer."
    )


class WorkflowValidationWarning(WorkflowError):
    """Non-fatal run-start finding.

    Surfaced by the validator alongside errors but does not block
    execution. Currently hosts
    [`WorkflowTimeoutShadowsNode`][workflows.errors.WorkflowTimeoutShadowsNode].
    """

    caller_message: ClassVar[str] = (
        "The workflow has a non-blocking issue worth reviewing. Contact your engineer."
    )


class WorkflowDuplicateNode(WorkflowValidationError):
    """Raised when two entries under `nodes:` share a name."""

    caller_message: ClassVar[str] = (
        "The workflow has two steps with the same name. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node",)

    def __init__(self, *, file: str, line: int, node: str) -> None:
        self.file = file
        self.line = line
        self.node = node
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=f"node {node!r} is declared more than once",
                fix=(
                    f"rename one of the {node!r} entries in 'nodes:' so "
                    "each key is unique"
                ),
            )
        )


class WorkflowDuplicateEdge(WorkflowValidationError):
    """Raised when the same `(from, to)` edge appears twice under `edges:`."""

    caller_message: ClassVar[str] = (
        "The workflow has the same connection between two steps declared twice. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("from_node", "to_node")

    def __init__(self, *, file: str, line: int, from_node: str, to_node: str) -> None:
        self.file = file
        self.line = line
        self.from_node = from_node
        self.to_node = to_node
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"edge from {from_node!r} to {to_node!r} is declared more than once"
                ),
                fix=(
                    f"remove the duplicate edge between {from_node!r} "
                    f"and {to_node!r} in 'edges:'"
                ),
            )
        )


class WorkflowExtendsCycle(WorkflowValidationError):
    """Raised when the workflow `extends:` chain forms a cycle."""

    caller_message: ClassVar[str] = (
        "The workflow's 'extends' chain loops back on itself. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("cycle",)

    def __init__(self, *, file: str, line: int, cycle: tuple[str, ...]) -> None:
        self.file = file
        self.line = line
        self.cycle = cycle
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    "the extends chain forms a cycle through "
                    + " -> ".join(repr(c) for c in cycle)
                ),
                fix=(
                    "break the cycle by removing one of the 'extends:' refs "
                    "in the chain (see docs/workflows#extends)"
                ),
            )
        )


class WorkflowExtendsDepthExceeded(WorkflowValidationError):
    """Raised when the `extends:` chain exceeds the depth limit."""

    caller_message: ClassVar[str] = (
        "The workflow's 'extends' chain is too long. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("depth", "limit")

    def __init__(self, *, file: str, line: int, depth: int, limit: int = 16) -> None:
        self.file = file
        self.line = line
        self.depth = depth
        self.limit = limit
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(f"extends chain depth {depth} exceeds the limit of {limit}"),
                fix=(
                    "flatten the extends chain so it stays at or below the "
                    "depth limit (see docs/workflows#extends)"
                ),
            )
        )


class WorkflowExtendsUnresolved(WorkflowValidationError):
    """Raised when an `extends:` ref doesn't resolve to a known workflow."""

    caller_message: ClassVar[str] = (
        "A workflow that this one extends could not be found. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, file: str, line: int, ref: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=f"extends ref {ref!r} does not resolve to a known workflow",
                fix=(
                    f"check the spelling of {ref!r} or add the referenced workflow "
                    "to the bundle (see docs/workflows#extends)"
                ),
            )
        )


class WorkflowExtendsPolicyForbidden(WorkflowValidationError):
    """Raised when a workflow extends another that declares `policy:` rules.

    `policy:` applies ONLY when the extended workflow is invoked
    DIRECTLY; it is not mergeable across the extends chain.
    """

    caller_message: ClassVar[str] = (
        "This workflow inherits from another that has its own permissions. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("extended_ref",)

    def __init__(self, *, file: str, line: int, extended_ref: str) -> None:
        self.file = file
        self.line = line
        self.extended_ref = extended_ref
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"extended workflow {extended_ref!r} declares 'policy:' "
                    "rules which cannot be inherited"
                ),
                fix=(
                    f"remove 'policy:' from {extended_ref!r} or call it directly "
                    "instead of via 'extends:' (see docs/workflows#policy-from)"
                ),
            )
        )


class WorkflowNodeNotDefined(WorkflowValidationError):
    """Raised when a YAML field names a node that doesn't exist in `nodes:`.

    Fires for `edge.from`, `edge.to`, `copy_of:` source,
    `map.apply:` target. DISTINCT from
    [`WorkflowJinjaRefUnknown`][workflows.errors.WorkflowJinjaRefUnknown]
    (which is exclusively for Jinja `nodes.<X>.<Y>` references).
    """

    caller_message: ClassVar[str] = (
        "A workflow step refers to another step that does not exist. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref", "location")

    def __init__(self, *, file: str, line: int, ref: str, location: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        self.location = location
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"{location} references node {ref!r} which is not "
                    "declared in 'nodes:'"
                ),
                fix=(
                    f"add {ref!r} to 'nodes:' or correct the reference in "
                    f"{location} (see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowAmbiguousStart(WorkflowValidationError):
    """Raised when multiple nodes have no incoming edges."""

    caller_message: ClassVar[str] = (
        "The workflow has more than one possible starting step. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("candidates",)

    def __init__(self, *, file: str, line: int, candidates: tuple[str, ...]) -> None:
        self.file = file
        self.line = line
        self.candidates = candidates
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    "multiple nodes have no incoming edges: "
                    + ", ".join(repr(c) for c in candidates)
                ),
                fix=(
                    "make exactly one node the start by adding an incoming "
                    "edge to each of the others (see docs/workflows#fan-in)"
                ),
            )
        )


class WorkflowNoStartNode(WorkflowValidationError):
    """Raised when every node has at least one incoming edge."""

    caller_message: ClassVar[str] = (
        "The workflow has no starting step. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ()

    def __init__(self, *, file: str, line: int) -> None:
        self.file = file
        self.line = line
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem="no node is a start node (every node has incoming edges)",
                fix=(
                    "remove one incoming edge so a single node has none and "
                    "becomes the start (see docs/workflows#fan-in)"
                ),
            )
        )


class WorkflowUnreachableNode(WorkflowValidationError):
    """Raised when a node is declared but can't be reached from the start."""

    caller_message: ClassVar[str] = (
        "A workflow step is defined but cannot be reached from the start. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node",)

    def __init__(self, *, file: str, line: int, node: str) -> None:
        self.file = file
        self.line = line
        self.node = node
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"node {node!r} is declared but has no incoming edge "
                    "from the start node and is not a 'map.apply:' target"
                ),
                fix=(
                    f"either remove {node!r} from 'nodes:' or add an edge "
                    f"that makes it reachable (see docs/workflows#fan-in)"
                ),
            )
        )


class WorkflowBindCollision(WorkflowValidationError):
    """Raised when two edges into the same target bind the same key."""

    caller_message: ClassVar[str] = (
        "A workflow step receives the same input from two different sources. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node", "bind")

    def __init__(
        self,
        *,
        file: str,
        line: int,
        node: str,
        bind: str,
        sources: tuple[str, ...],
    ) -> None:
        self.file = file
        self.line = line
        self.node = node
        self.bind = bind
        self.sources = sources
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"edges from {', '.join(repr(s) for s in sources)} both "
                    f"bind {bind!r} into {node!r}"
                ),
                fix=(
                    f"rename one bind so {node!r} receives distinct keys, or "
                    "drop one of the edges (see docs/workflows#fan-in)"
                ),
            )
        )


class WorkflowBindTypeMismatch(WorkflowValidationError):
    """Raised when a rendered bind value fails strict validation at runtime."""

    caller_message: ClassVar[str] = (
        "A workflow step received the wrong type of value. "
        "Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node", "bind")

    def __init__(
        self,
        *,
        file: str,
        line: int,
        node: str,
        bind: str,
        expected_type: str,
        actual_type: str,
    ) -> None:
        self.file = file
        self.line = line
        self.node = node
        self.bind = bind
        self.expected_type = expected_type
        self.actual_type = actual_type
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"bind {bind!r} into {node!r} expected "
                    f"{expected_type} but received {actual_type}"
                ),
                fix=(
                    f"adjust the upstream node so {bind!r} renders as "
                    f"{expected_type} (see docs/workflows#binds-rhs)"
                ),
            )
        )


class WorkflowJinjaRefUnknown(WorkflowValidationError):
    """Raised when a Jinja `nodes.<X>.<Y>` reference doesn't resolve.

    Carries `reason: str` (typed as `str`, not `Literal`):

    - `"not_predecessor"`: X is not a graph-reachable transitive
      ancestor of the consuming node.
    - `"unknown_field"`: X resolves and its output schema is known,
      but Y is not in that schema.
    - `"not_declared"`: X resolves but its output schema is unknown
      (e.g., authored Python node failed to import).
    """

    caller_message: ClassVar[str] = (
        "A workflow step refers to a value that isn't available at that point. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = (
        "node",
        "reference",
        "reason",
    )

    def __init__(
        self,
        *,
        file: str,
        line: int,
        node: str,
        reference: str,
        reason: str,
    ) -> None:
        self.file = file
        self.line = line
        self.node = node
        self.reference = reference
        self.reason = reason
        if reason == "not_predecessor":
            problem = (
                f"node {node!r} references {reference!r} but the source "
                f"is not a transitive ancestor"
            )
            fix = (
                f"add an edge that makes the source reachable from {node!r} "
                "(see docs/workflows#fan-in)"
            )
        elif reason == "unknown_field":
            problem = (
                f"node {node!r} references {reference!r} but that field "
                f"is not in the source node's output schema"
            )
            fix = (
                "rename the field to one the source declares "
                "(see docs/workflows#node-types)"
            )
        elif reason == "not_declared":
            problem = (
                f"node {node!r} references {reference!r} but the source "
                f"node's output schema is unknown"
            )
            fix = (
                "ensure the source node loads cleanly so its outputs are "
                "discoverable (see docs/workflows#node-types)"
            )
        else:
            problem = f"node {node!r} references {reference!r} (reason: {reason})"
            fix = "see docs/workflows#fan-in for the reference rules"
        super().__init__(
            _yaml_origin_str(self, file=file, line=line, problem=problem, fix=fix)
        )


class WorkflowMapperWorkerNotAddressable(WorkflowValidationError):
    """Raised when code outside a map references one of its worker nodes."""

    caller_message: ClassVar[str] = (
        "A workflow step refers into the middle of a parallel block. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node", "worker")

    def __init__(self, *, file: str, line: int, node: str, worker: str) -> None:
        self.file = file
        self.line = line
        self.node = node
        self.worker = worker
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"node {node!r} references map worker {worker!r} which "
                    "is not addressable from outside its map"
                ),
                fix=(
                    "reference the map's collected output via "
                    "'nodes.<map-name>.results' instead of the worker "
                    f"{worker!r} (see docs/workflows#map)"
                ),
            )
        )


class WorkflowBranchOnNonBranchable(WorkflowValidationError):
    """Raised when an edge carries `branch:` but the source node doesn't branch."""

    caller_message: ClassVar[str] = (
        "A workflow edge has true/false routing on a step that does not branch. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("from_node", "to_node")

    def __init__(
        self,
        *,
        file: str,
        line: int,
        from_node: str,
        to_node: str,
    ) -> None:
        self.file = file
        self.line = line
        self.from_node = from_node
        self.to_node = to_node
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"edge from {from_node!r} to {to_node!r} carries 'branch:' "
                    f"but {from_node!r} is not a branching node"
                ),
                fix=(
                    f"remove the 'branch:' field from the edge or change "
                    f"{from_node!r} to a branching node type "
                    "(see docs/workflows#branch)"
                ),
            )
        )


class WorkflowBranchMissingArm(WorkflowValidationError):
    """Raised when a branch node is missing its `true` or `false` arm."""

    caller_message: ClassVar[str] = (
        "A workflow decision step is missing a true or false arm. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node", "missing_arm")

    def __init__(self, *, file: str, line: int, node: str, missing_arm: str) -> None:
        self.file = file
        self.line = line
        self.node = node
        self.missing_arm = missing_arm
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(f"branch node {node!r} is missing the {missing_arm!r} arm"),
                fix=(
                    f"add an edge from {node!r} with branch: {missing_arm} "
                    "(see docs/workflows#branch)"
                ),
            )
        )


class WorkflowMapperWorkerMissingField(WorkflowValidationError):
    """Raised when a map node expects an output field the worker doesn't declare."""

    caller_message: ClassVar[str] = (
        "A parallel block expects a value the inner step doesn't produce. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("map_node", "field")

    def __init__(
        self,
        *,
        file: str,
        line: int,
        map_node: str,
        worker: str,
        field: str,
    ) -> None:
        self.file = file
        self.line = line
        self.map_node = map_node
        self.worker = worker
        self.field = field
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"map node {map_node!r} expects field {field!r} but worker "
                    f"{worker!r} does not declare it"
                ),
                fix=(
                    f"add {field!r} to the worker {worker!r}'s output schema "
                    "(see docs/workflows#map)"
                ),
            )
        )


class WorkflowCallCycle(WorkflowValidationError):
    """Raised when sub-workflow refs form a cycle."""

    caller_message: ClassVar[str] = (
        "A sub-workflow ends up calling itself in a cycle. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("cycle",)

    def __init__(self, *, file: str, line: int, cycle: tuple[str, ...]) -> None:
        self.file = file
        self.line = line
        self.cycle = cycle
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    "workflow call chain forms a cycle: "
                    + " -> ".join(repr(c) for c in cycle)
                ),
                fix=(
                    "break the cycle by removing one of the sub-workflow refs "
                    "(see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowNodeImportFailed(WorkflowValidationError):
    """Raised when an authored Python `@node` module fails to import."""

    caller_message: ClassVar[str] = (
        "A workflow step's code could not be loaded. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, file: str, line: int, ref: str, reason: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        self.reason = reason
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=f"node module {ref!r} failed to import: {reason}",
                fix=(
                    f"fix the import error in the node module for {ref!r} "
                    "(see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowNodeMissingRun(WorkflowValidationError):
    """Raised when an authored Python `@node` module has no `run` function."""

    caller_message: ClassVar[str] = (
        "A workflow step's code is missing its run function. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, file: str, line: int, ref: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=f"node module {ref!r} does not define a 'run' function",
                fix=(
                    f"add 'def run(...)' to the {ref!r} module "
                    "(see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowNodeNotDecorated(WorkflowValidationError):
    """Raised when a module's `run` function isn't decorated with `@node`."""

    caller_message: ClassVar[str] = (
        "A workflow step's code is not registered as a node. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, file: str, line: int, ref: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"node module {ref!r}'s 'run' is missing the '@node' decorator"
                ),
                fix=(
                    f"decorate the run function in {ref!r} with '@node(...)' "
                    "(see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowNodeSignatureInvalid(WorkflowValidationError):
    """Raised when an authored `@node` `run`'s signature doesn't match the contract."""

    caller_message: ClassVar[str] = (
        "A workflow step's code has the wrong shape. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, file: str, line: int, ref: str, reason: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        self.reason = reason
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=f"node {ref!r} run signature is invalid: {reason}",
                fix=(
                    "adjust the run signature to match the @node API "
                    "(see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowNodeReturnTypeInvalid(WorkflowValidationError):
    """Raised when an authored `@node` `run`'s return annotation isn't allowed."""

    caller_message: ClassVar[str] = (
        "A workflow step's code returns the wrong shape. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, file: str, line: int, ref: str, return_type: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        self.return_type = return_type
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"node {ref!r} declares an unsupported return type {return_type!r}"
                ),
                fix=(
                    "use a BaseModel subclass, bool, or tuple[bool, BaseModel] "
                    "as the return type (see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowInvalidTimeout(WorkflowValidationError):
    """Raised when workflow- or node-level `timeout_seconds` is non-positive."""

    caller_message: ClassVar[str] = (
        "A workflow timeout is set to an invalid value. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("scope", "value")

    def __init__(self, *, file: str, line: int, scope: str, value: float) -> None:
        self.file = file
        self.line = line
        self.scope = scope
        self.value = value
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"timeout_seconds at {scope} is {value} which is not positive"
                ),
                fix=(
                    f"set timeout_seconds at {scope} to a positive number, or "
                    "remove the field entirely (see docs/workflows#timeout)"
                ),
            )
        )


class WorkflowParallelStateConflict(WorkflowValidationError):
    """Raised when two concurrent branches fight over a state key.

    Also raised when a `parallel: true` map transitively writes state.
    Surfaced by the run-start gate when an `auto`-mode workflow has
    two parallel-eligible nodes whose state-write/read sets overlap,
    OR when a `parallel: true` map's worker subgraph reaches a
    `set_state` / `delete_state` node. The fix-path message names
    both colliding nodes and the shared state key so authors can
    serialise the offending pair via an explicit edge.
    """

    caller_message: ClassVar[str] = (
        "The workflow tries to run two nodes in parallel that fight "
        "over the same state key. Fix: add an explicit edge to "
        "serialise them, set concurrency: sequential, or remove the "
        "state write from one of them."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = (
        "node_a",
        "node_b",
        "state_key",
    )

    def __init__(
        self,
        *,
        file: str,
        line: int,
        node_a: str,
        node_b: str,
        state_key: str,
    ) -> None:
        self.file = file
        self.line = line
        self.node_a = node_a
        self.node_b = node_b
        self.state_key = state_key
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"nodes {node_a!r} and {node_b!r} can run in parallel "
                    f"and both touch state key {state_key!r}"
                ),
                fix=(
                    "add an edge serialising the two nodes, set "
                    "'concurrency: sequential', or remove the state "
                    "write from one of them (see docs/workflows#concurrency)"
                ),
            )
        )


class WorkflowInvalidConcurrency(WorkflowValidationError):
    """Raised when `concurrency:` is set but not a positive integer.

    Surfaced by the run-start gate for any `concurrency:` field on a
    map node. Distinct from
    [`WorkflowInvalidTimeout`][workflows.errors.WorkflowInvalidTimeout]
    (which covers `timeout_seconds`).
    """

    caller_message: ClassVar[str] = (
        "The workflow declares a concurrency bound that is not a positive integer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node", "value")

    def __init__(
        self,
        *,
        file: str,
        line: int,
        node: str,
        value: Any,
    ) -> None:
        self.file = file
        self.line = line
        self.node = node
        self.value = value
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"concurrency at node {node!r} is {value!r} which is "
                    "not a positive integer"
                ),
                fix=(
                    f"set concurrency at {node!r} to a positive integer, "
                    "or remove the field entirely "
                    "(see docs/workflows#concurrency)"
                ),
            )
        )


class WorkflowInvalidRef(WorkflowValidationError):
    """Raised when a `type:` value is not a 3-seg ref or is malformed."""

    caller_message: ClassVar[str] = (
        "A workflow step refers to a step type by an outdated short name. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, file: str, line: int, ref: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"value {ref!r} is not a 3-segment catalog ref "
                    "(expected '<bundle>/<plural>/<name>')"
                ),
                fix=(
                    "rewrite the ref as a full 3-segment path "
                    "(see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowBuiltinShadowed(WorkflowValidationError):
    """Raised when user-authored code occupies a reserved built-in catalog slot."""

    caller_message: ClassVar[str] = (
        "A custom step type collides with a built-in one. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, file: str, line: int, ref: str) -> None:
        self.file = file
        self.line = line
        self.ref = ref
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(f"authored node at {ref!r} shadows a reserved built-in slot"),
                fix=(
                    f"rename the authored node so it does not collide with "
                    f"the built-in slot {ref!r} (see docs/workflows#node-types)"
                ),
            )
        )


class WorkflowFromShapeError(WorkflowValidationError):
    """Raised when a policy rule's `from:` value is not a 3-seg ref."""

    caller_message: ClassVar[str] = (
        "A permission rule has a malformed 'from' field. Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("from_ref",)

    def __init__(self, *, file: str, line: int, from_ref: str) -> None:
        self.file = file
        self.line = line
        self.from_ref = from_ref
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"'from' value {from_ref!r} is not a 3-segment ref "
                    "(expected '<bundle>/agents/<name>' or '<bundle>/workflows/<name>')"
                ),
                fix=(
                    "rewrite 'from' as a 3-segment ref to an agent or workflow "
                    "(see docs/workflows#policy-from)"
                ),
            )
        )


class WorkflowFromAssetTypeError(WorkflowValidationError):
    """Raised when a policy rule's `from:` asset type is not agents/workflows."""

    caller_message: ClassVar[str] = (
        "A permission rule's 'from' field points at the wrong kind of asset. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("from_ref", "asset_type")

    def __init__(
        self,
        *,
        file: str,
        line: int,
        from_ref: str,
        asset_type: str,
    ) -> None:
        self.file = file
        self.line = line
        self.from_ref = from_ref
        self.asset_type = asset_type
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"'from' ref {from_ref!r} has asset type {asset_type!r}; "
                    "only 'agents' or 'workflows' are allowed"
                ),
                fix=(
                    "point 'from' at an agent ref or a workflow ref "
                    "(see docs/workflows#policy-from)"
                ),
            )
        )


class WorkflowFromNotFound(WorkflowValidationError):
    """Raised when a policy rule's `from:` ref doesn't resolve in the catalog."""

    caller_message: ClassVar[str] = (
        "A permission rule's 'from' field points at something that doesn't exist. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("from_ref",)

    def __init__(self, *, file: str, line: int, from_ref: str) -> None:
        self.file = file
        self.line = line
        self.from_ref = from_ref
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=f"'from' ref {from_ref!r} does not resolve in the catalog",
                fix=(
                    f"check the spelling of {from_ref!r} or add the referenced "
                    "asset (see docs/workflows#policy-from)"
                ),
            )
        )


# ---------------------------------------------------------------------------
# Validation warnings
# ---------------------------------------------------------------------------


class WorkflowTimeoutShadowsNode(WorkflowValidationWarning):
    """Warning surfaced when a per-node timeout exceeds the workflow timeout."""

    caller_message: ClassVar[str] = (
        "A workflow step's timeout is longer than the whole workflow's. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node",)

    def __init__(
        self,
        *,
        file: str,
        line: int,
        node: str,
        node_timeout: float,
        workflow_timeout: float,
    ) -> None:
        self.file = file
        self.line = line
        self.node = node
        self.node_timeout = node_timeout
        self.workflow_timeout = workflow_timeout
        super().__init__(
            _yaml_origin_str(
                self,
                file=file,
                line=line,
                problem=(
                    f"node {node!r} timeout {node_timeout}s is greater than the "
                    f"workflow timeout {workflow_timeout}s and will never fire"
                ),
                fix=(
                    f"lower {node!r}'s timeout to under {workflow_timeout}s or "
                    "raise the workflow timeout (see docs/workflows#timeout)"
                ),
            )
        )


# ---------------------------------------------------------------------------
# Runtime errors
# ---------------------------------------------------------------------------


class WorkflowRuntimeError(WorkflowError):
    """Base for run-time errors raised by the executor."""

    caller_message: ClassVar[str] = (
        "The workflow ran into a problem while running. "
        "Share the run ID with your engineer."
    )


class WorkflowNotRunnable(WorkflowRuntimeError):
    """Raised when the workflow can't be started at runtime."""

    caller_message: ClassVar[str] = (
        "The workflow can't be started right now. Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, ref: str, reason: str) -> None:
        self.ref = ref
        self.reason = reason
        super().__init__(
            _runtime_str(self, problem=f"workflow {ref!r} is not runnable: {reason}")
        )


class WorkflowInputInvalid(WorkflowRuntimeError):
    """Raised when the workflow's `inputs` fail strict-mode validation."""

    caller_message: ClassVar[str] = (
        "The workflow's inputs are invalid. Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, ref: str, reason: str) -> None:
        self.ref = ref
        self.reason = reason
        super().__init__(
            _runtime_str(self, problem=f"input to {ref!r} is invalid: {reason}")
        )


class BashNonZeroExit(WorkflowRuntimeError):
    """Sentinel for `kolsch/nodes/bash` non-zero exits.

    Raised by
    [`BashRunner`][workflows.builtins.bash.BashRunner] when the
    dispatched bash command finishes with `exit_code != 0`. The
    executor catches this exception at the worklist's runner-invoke
    arm and decides:

    - If the bash node has at least one outgoing edge with
      `branch: not None`: success-of-the-false-arm semantics — the
      captured output flows through the normal completion path and
      the branch dispatcher routes to the `branch: false` edge.
    - Otherwise: re-raise so the existing failure path emits
      `workflow.node.failed` + `workflow.run.failed` per the
      abort-on-failure rule.

    The exception carries the captured `{stdout, stderr, exit_code}`
    dict so the executor can use it as the node's output when the
    branching success path applies.
    """

    caller_message: ClassVar[str] = (
        "A bash workflow step exited non-zero. Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node",)

    def __init__(self, *, node: str, captured: dict[str, Any]) -> None:
        self.node = node
        self.captured = captured
        exit_code = captured.get("exit_code", "?")
        super().__init__(
            _runtime_str(
                self,
                problem=(f"bash node {node!r} exited with exit_code={exit_code}"),
            )
        )


class WorkflowNodeFailed(WorkflowRuntimeError):
    """Raised when a node's runner raises during dispatch."""

    caller_message: ClassVar[str] = (
        "A workflow step failed. Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node", "invocation_id")

    def __init__(
        self,
        *,
        node: str,
        invocation_id: str,
        cause: BaseException | None = None,
    ) -> None:
        self.node = node
        self.invocation_id = invocation_id
        self.cause = cause
        problem = f"node {node!r} (invocation {invocation_id!r}) failed"
        if cause is not None:
            problem = f"{problem}: {cause!s}"
        super().__init__(_runtime_str(self, problem=problem))
        if cause is not None:
            self.__cause__ = cause


class WorkflowSecretUnresolved(WorkflowRuntimeError):
    """Raised when a declared secret isn't available from its provider."""

    caller_message: ClassVar[str] = (
        "A required secret isn't available. Contact your engineer to configure it."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("secret_name",)

    def __init__(self, *, secret_name: str, node: str | None = None) -> None:
        self.secret_name = secret_name
        self.node = node
        loc = f" for node {node!r}" if node else ""
        super().__init__(
            _runtime_str(
                self,
                problem=f"secret {secret_name!r} could not be resolved{loc}",
            )
        )


class WorkflowFileAccessDenied(WorkflowRuntimeError):
    """Raised when a node tries to access a path the policy forbids."""

    caller_message: ClassVar[str] = (
        "A workflow step tried to read or write a file it isn't allowed to. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("path", "node")

    def __init__(self, *, path: str, node: str) -> None:
        self.path = path
        self.node = node
        super().__init__(
            _runtime_str(
                self,
                problem=(
                    f"node {node!r} tried to access {path!r} which is "
                    "outside the allowed paths"
                ),
            )
        )


class WorkflowJinjaSecurityError(WorkflowRuntimeError):
    """Raised when the sandbox blocks an unsafe attribute or callable access."""

    caller_message: ClassVar[str] = (
        "A workflow step tried to do something that isn't allowed. "
        "Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("location", "expression")

    def __init__(
        self,
        *,
        location: str,
        expression: str,
        cause: BaseException | None = None,
    ) -> None:
        self.location = location
        self.expression = expression
        super().__init__(
            _runtime_str(
                self,
                problem=(
                    f"sandbox blocked an unsafe expression at {location!r}: "
                    f"{expression!r}"
                ),
            )
        )
        if cause is not None:
            self.__cause__ = cause


class WorkflowTimeoutExceeded(WorkflowRuntimeError):
    """Raised when the workflow-level deadline hits at the next node boundary."""

    caller_message: ClassVar[str] = (
        "This workflow took longer than expected and was stopped. You can try again."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("workflow_run_id", "limit")

    def __init__(self, *, workflow_run_id: str, limit: float) -> None:
        self.workflow_run_id = workflow_run_id
        self.limit = limit
        super().__init__(
            _runtime_str(
                self,
                problem=(
                    f"workflow run {workflow_run_id!r} exceeded the {limit}s deadline"
                ),
            )
        )


class NodeTimeoutExceeded(WorkflowRuntimeError):
    """Raised when a per-node timeout fires; retry policy applies."""

    caller_message: ClassVar[str] = (
        "A workflow step took longer than expected and was stopped. "
        "Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = (
        "node",
        "invocation_id",
        "limit",
    )

    def __init__(self, *, node: str, invocation_id: str, limit: float) -> None:
        self.node = node
        self.invocation_id = invocation_id
        self.limit = limit
        super().__init__(
            _runtime_str(
                self,
                problem=(
                    f"node {node!r} (invocation {invocation_id!r}) exceeded "
                    f"its {limit}s timeout"
                ),
            )
        )


class AgentRefUnresolved(WorkflowRuntimeError):
    """Raised when the catalog can't resolve an `agent:` ref at runtime.

    The validator already enforces that every `agent:` ref on an
    agent node resolves at validation time; this runtime guard is the
    safety net for validator drift (e.g., an asset deleted between
    validate and run). Surfaces from
    [`AgentRunner`][workflows.builtins.agent.AgentRunner] and remaps
    to
    [`WorkflowNodeFailed`][workflows.errors.WorkflowNodeFailed] at
    the executor boundary.
    """

    caller_message: ClassVar[str] = (
        "A workflow step references an agent that isn't installed. "
        "Contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

    def __init__(self, *, ref: str) -> None:
        self.ref = ref
        super().__init__(
            _runtime_str(self, problem=f"agent ref {ref!r} could not be resolved")
        )


class ChildWorkflowFailed(WorkflowRuntimeError):
    """Raised when a sub-workflow run reaches a non-success terminal.

    Wrapped by
    [`WorkflowSubRunner`][workflows.builtins.workflow_sub.WorkflowSubRunner]
    inside `WorkflowNodeFailed(cause=)` whenever a child run's status
    is `failed` / `aborted` / `timed_out`. The two-field identifying
    surface (`run_id`, `status`) lets operators read the parent's
    `workflow.node.failed` envelope and pivot directly to the child's
    JSONL by id.

    The pause shape (`waiting_for_input`) is NOT a failure — the
    child's pause re-raises `WorkflowPausedForInput` through the
    runner verbatim.
    """

    caller_message: ClassVar[str] = (
        "A sub-workflow step failed. Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("run_id", "status")

    def __init__(self, *, run_id: str, status: str = "failed") -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(
            _runtime_str(
                self,
                problem=(
                    f"child workflow run {run_id!r} terminated with status {status!r}"
                ),
            )
        )


class WorkflowResumeRejected(WorkflowRuntimeError):
    """Raised when resume targets a run not in `waiting_for_input`.

    Surfaced by
    [`IterativeExecutor.resume`][workflows.executor.IterativeExecutor.resume]
    (and the `kolsch workflows resume` CLI) when the targeted run's
    status is anything other than `waiting_for_input`. The status
    gate is the boundary between "this run is asleep on the disk
    waiting for an answer" (resumable) and every other state
    (running, terminal, pending) where a resume is undefined.
    """

    caller_message: ClassVar[str] = (
        "This workflow can't be resumed right now — it isn't paused. "
        "Check the run's status with 'kolsch workflows show'."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("run_id", "status")

    def __init__(self, *, run_id: str, status: str) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(
            _runtime_str(
                self,
                problem=(
                    f"workflow run {run_id!r} cannot be resumed "
                    f"(status={status!r}; expected 'waiting_for_input')"
                ),
            )
        )


class WorkflowRunNotFound(WorkflowRuntimeError):
    """Raised when a target run's `meta.json` or `events.jsonl` is missing.

    Surfaced by
    [`IterativeExecutor.start_from_fork`][workflows.executor.IterativeExecutor.start_from_fork]
    (and the `kolsch workflows fork` / `events` CLI handlers) when
    the targeted run cannot be loaded — either its meta sidecar was
    never created, or its `events.jsonl` has been deleted out from
    under us. Distinct from
    [`WorkflowResumeRejected`][workflows.errors.WorkflowResumeRejected]
    (which fires for a run that exists but isn't in the right
    status).
    """

    caller_message: ClassVar[str] = (
        "The workflow run you're looking for is no longer available. "
        "Share the run ID with your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("run_id",)

    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(
            _runtime_str(
                self,
                problem=(
                    f"workflow run {run_id!r} not found or its events log "
                    "has been deleted"
                ),
            )
        )


class NodeTypeNotImplemented(WorkflowRuntimeError):
    """Raised when dispatch hits a node type whose runtime isn't shipped.

    Used as a not-yet-implemented marker — the dispatcher
    [`RunnerRegistry`][workflows.executor.RunnerRegistry] raises this
    exception with the phase number that ships the runner. The
    `caller_message` names the phase explicitly so end users can tell
    whether to wait or pursue a workaround.
    """

    caller_message: ClassVar[str] = (
        "This kind of workflow step is not yet implemented. "
        "Check the docs for the phase that ships it, or contact your engineer."
    )
    identifying_names_keys: ClassVar[tuple[str, ...]] = (
        "node",
        "type_ref",
        "phase",
    )

    def __init__(self, *, node: str, type_ref: str, phase: str) -> None:
        self.node = node
        self.type_ref = type_ref
        self.phase = phase
        super().__init__(
            _runtime_str(
                self,
                problem=(
                    f"node {node!r} uses type {type_ref!r} whose runtime "
                    f"ships in Phase {phase}"
                ),
            )
        )


# ---------------------------------------------------------------------------
# Control-flow signals — NOT WorkflowError subclasses.
# ---------------------------------------------------------------------------


class WorkflowPausedForInput(Exception):
    """The `user_input` runner's pause signal.

    NOT a `WorkflowError` subclass: pause is normal control flow, not
    a failure. The executor's failure path (which emits
    `workflow.node.failed` + `workflow.run.failed`) does NOT handle
    this — the worklist catches it separately, persists the open
    invocation marker, flips the run status to `waiting_for_input`,
    and exits cleanly. The matching `workflow.node.started` event for
    this invocation is already on disk; the ABSENCE of a
    corresponding completion event IS the pause marker.
    """

    def __init__(
        self,
        *,
        node_name: str,
        invocation_id: str,
        prompt: str,
    ) -> None:
        self.node_name = node_name
        self.invocation_id = invocation_id
        self.prompt = prompt
        super().__init__(
            f"workflow pause for input at node {node_name!r} "
            f"(invocation {invocation_id!r})"
        )


# ---------------------------------------------------------------------------
# Sanity: every concrete class declares the two required ClassVars
# (`caller_message`, `identifying_names_keys`). A trivial assertion at
# import time keeps drift visible — if a future subclass forgets to set
# them, the import fails loudly instead of the bug showing up in
# production rendering.
# ---------------------------------------------------------------------------


def _verify_classvar_coverage() -> None:
    seen: list[type[WorkflowError]] = []

    def walk(cls: type[WorkflowError]) -> None:
        seen.append(cls)
        for sub in cls.__subclasses__():
            walk(sub)

    walk(WorkflowError)
    for cls in seen:
        cm: Any = getattr(cls, "caller_message", None)
        ink: Any = getattr(cls, "identifying_names_keys", None)
        if not isinstance(cm, str) or not cm:
            msg = f"{cls.__name__} is missing a non-empty caller_message ClassVar"
            raise TypeError(msg)
        if not isinstance(ink, tuple):
            msg = f"{cls.__name__} is missing identifying_names_keys ClassVar"
            raise TypeError(msg)


_verify_classvar_coverage()

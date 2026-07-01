"""Workflow YAML parser — Pydantic models and the loader.

SHAPE validation only — DAG / binds / branching / predecessor /
bind-collision / timeout-sign checks land in the validator.

Layout:

- Per-built-in `*NodeDef` for the 15 reserved `kolsch/nodes/<name>`
  types, each paired 1:1 with its `*Inputs(BaseModel)`.
- [`CopyOfNodeDef`][workflows.parser.CopyOfNodeDef] for nodes that
  reuse another node's type via `copy_of:` (semantic resolution at
  the validator).
- [`CatalogNodeDef`][workflows.catalog_node.CatalogNodeDef] (imported
  from `workflows.catalog_node`) for authored Python nodes referenced
  by 3-seg catalog ref.
- [`EdgeDef`][workflows.parser.EdgeDef] (`{from, to, binds, branch,
  when}` — NO `ref:` field; multi-input edges are the v1 fan-in
  mechanism).
- [`GraphDef`][workflows.parser.GraphDef] +
  [`Workflow`][workflows.parser.Workflow] (top-level).

The `type:` discriminator routes raw dicts to the right class via
`_dispatch_node_def`. Built-in 3-seg refs land on the matching
`*NodeDef`; any other 3-seg ref lands on `CatalogNodeDef`; non-3-seg
input raises
[`WorkflowInvalidRef`][workflows.errors.WorkflowInvalidRef].

`load_workflow` reads YAML via the `_yamlio` helper (ruamel.yaml
under the hood) and surfaces parse failures with file path and line
number.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from _yamlio import YAMLError, load
from assets.refs import is_three_segment
from providers.authz import Rule
from schemas.config import IOSpec
from workflows.catalog_node import (
    BUILTIN_TYPE_NAMES,
    CatalogNodeDef,
    builtin_name_for_ref,
)
from workflows.errors import (
    WorkflowInvalidRef,
    WorkflowValidationError,
)

__all__ = [
    "BUILTIN_TYPE_NAMES",
    "AgentInputs",
    "AgentNodeDef",
    "BashInputs",
    "BashNodeDef",
    "BranchInputs",
    "BranchNodeDef",
    "BrowseUrlInputs",
    "BrowseUrlNodeDef",
    "CatalogNodeDef",
    "ComputeInputs",
    "ComputeNodeDef",
    "CopyOfNodeDef",
    "DeleteStateInputs",
    "DeleteStateNodeDef",
    "EdgeDef",
    "FilterInputs",
    "FilterNodeDef",
    "GraphDef",
    "ListFilesInputs",
    "ListFilesNodeDef",
    "MapInputs",
    "MapNodeDef",
    "NodeDef",
    "NoopInputs",
    "NoopNodeDef",
    "ReadFileInputs",
    "ReadFileNodeDef",
    "SetStateInputs",
    "SetStateNodeDef",
    "UserInputInputs",
    "UserInputNodeDef",
    "Workflow",
    "WorkflowNodeDef",
    "WorkflowSubInputs",
    "WriteFileInputs",
    "WriteFileNodeDef",
    "load_workflow",
]


# ---------------------------------------------------------------------------
# Per-built-in Inputs models.
#
# Each `*Inputs(BaseModel)` declares the locked `model_config.extra`
# policy and any strict-typed fields the type table pins. Runtime
# strict-mode validation (the `WorkflowBindTypeMismatch` contract)
# fires at node-fire time. Co-located with their `*NodeDef` so callers
# import both from one module (`from workflows.parser import
# BashNodeDef, BashInputs`).
# ---------------------------------------------------------------------------


class BashInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/bash`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class BrowseUrlInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/browse_url`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class AgentInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/agent`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class WorkflowSubInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/workflow` (sub-workflow node).

    Named to avoid collision with the `workflow` asset model —
    workflow-as-asset is the catalogued kind; this is the
    receiver-side input model for the sub-workflow node.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class NoopInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/noop`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class UserInputInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/user_input`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class ReadFileInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/read_file`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class WriteFileInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/write_file`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class ListFilesInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/list_files`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class SetStateInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/set_state`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class DeleteStateInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/delete_state`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class FilterInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/filter`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    items: list[Any] = Field(default_factory=list)


class MapInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/map`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    over: list[Any] = Field(default_factory=list)


class BranchInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/branch`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class ComputeInputs(BaseModel):
    """Receiver inputs for `kolsch/nodes/compute`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Common base — modifier fields.
# ---------------------------------------------------------------------------


class _BaseNodeDef(BaseModel):
    """Common modifier fields shared by every built-in `*NodeDef`.

    Every node accepts optional `description` (`min_length=1`
    validation), optional positive-ish `timeout_seconds` (the
    validator enforces the sign), an optional `when:` gate modifier
    (YAML literal OR Jinja-marked string), `retry` (int; thin v1
    facade), and `secrets` (declared list of secret-store keys the
    node may read from its provider; resolved at runtime).

    `model_config = ConfigDict(extra="forbid")` is the per-type
    default — unknown keys are caught at parse. Subclasses that
    intentionally accept arbitrary fields override the policy.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = None
    when: Any = None
    retry: int = 0
    secrets: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_keys(cls, data: Any) -> Any:  # noqa: ANN401
        """Reject deprecated YAML keys at parse-time.

        - `instance_of` → fix-path to `copy_of`.
        - `collect` → fix-path explaining fan-in via multi-input edges
          (the field is gone from NodeDef).
        """
        if isinstance(data, dict):
            if "instance_of" in data:
                msg = (
                    "Workflow node uses deprecated key 'instance_of'. "
                    "Fix: rename to 'copy_of' (see docs/workflows#node-types)."
                )
                raise WorkflowValidationError(msg)
            if "collect" in data:
                msg = (
                    "Workflow node uses deprecated key 'collect'. "
                    "Fix: express fan-in by adding additional edges to "
                    "this node in 'graph.edges', or use a 'branch' node "
                    "for OR-semantics (see docs/workflows#fan-in)."
                )
                raise WorkflowValidationError(msg)
        return data


# ---------------------------------------------------------------------------
# The 14 built-in *NodeDef classes.
# ---------------------------------------------------------------------------


class BashNodeDef(_BaseNodeDef):
    """`kolsch/nodes/bash` — shell command."""

    type: Literal["kolsch/nodes/bash"]
    command: str
    cwd: str | None = None
    env: dict[str, Any] | None = None


class BrowseUrlNodeDef(_BaseNodeDef):
    """`kolsch/nodes/browse_url` — fetch a rendered web page.

    Wraps the `browse_url` bundle tool. Type-specific fields are
    Jinja-renderable strings (`url`, `format`, `wait_selector`) plus
    per-value Jinja rendering on `headers` / `cookies` (keys pass
    through as literals — allowing dynamic header names would let a
    Jinja-generated string smuggle unintended headers past review).
    `request_timeout_seconds` is the TOOL-level request timeout —
    named this way deliberately to avoid colliding with
    `_BaseNodeDef.timeout_seconds` (the workflow-executor-level
    timeout).
    """

    type: Literal["kolsch/nodes/browse_url"]
    url: str = Field(description="URL to fetch. Jinja-renderable.")
    format: Literal["text", "html"] = "text"
    wait_selector: str | None = None
    headers: dict[str, str] | None = None
    cookies: dict[str, str] | None = None
    request_timeout_seconds: int = Field(default=30, ge=1, le=120)


class AgentNodeDef(_BaseNodeDef):
    """`kolsch/nodes/agent` — invoke a catalogued agent."""

    type: Literal["kolsch/nodes/agent"]
    agent: str
    # Per-invocation `outputs:` override reuses `IOSpec` from
    # src/schemas/config.py — no new grammar.
    outputs: IOSpec | None = None


class UserInputNodeDef(_BaseNodeDef):
    """`kolsch/nodes/user_input` — pause and await chat reply."""

    type: Literal["kolsch/nodes/user_input"]
    prompt: str


class NoopNodeDef(_BaseNodeDef):
    """`kolsch/nodes/noop` — fan-out placeholder."""

    type: Literal["kolsch/nodes/noop"]


class ReadFileNodeDef(_BaseNodeDef):
    """`kolsch/nodes/read_file` — read file content."""

    type: Literal["kolsch/nodes/read_file"]
    path: str


class WriteFileNodeDef(_BaseNodeDef):
    """`kolsch/nodes/write_file` — write file content."""

    type: Literal["kolsch/nodes/write_file"]
    path: str
    content: str | bytes


class ListFilesNodeDef(_BaseNodeDef):
    """`kolsch/nodes/list_files` — enumerate directory entries."""

    type: Literal["kolsch/nodes/list_files"]
    path: str
    pattern: str | None = None


class SetStateNodeDef(_BaseNodeDef):
    """`kolsch/nodes/set_state` — write workflow state keys.

    Values follow the bare-or-Jinja convention: a YAML literal OR a
    Jinja-marked string.
    """

    type: Literal["kolsch/nodes/set_state"]
    keys: dict[str, Any]


class DeleteStateNodeDef(_BaseNodeDef):
    """`kolsch/nodes/delete_state` — remove workflow state keys."""

    type: Literal["kolsch/nodes/delete_state"]
    keys: list[str]


class FilterNodeDef(_BaseNodeDef):
    """`kolsch/nodes/filter` — predicate-based list reducer."""

    type: Literal["kolsch/nodes/filter"]
    over: Any
    condition: Any
    item_as: str = "item"


class ComputeNodeDef(_BaseNodeDef):
    """`kolsch/nodes/compute` — general-purpose Jinja expression.

    `value:` accepts a YAML literal OR a Jinja-marked string
    `"{{ expr }}"`. Only the latter is evaluated under the sandboxed
    env at runtime; literals pass through as the captured output
    `{"value": <literal>}`.
    """

    type: Literal["kolsch/nodes/compute"]
    value: Any


class WorkflowNodeDef(_BaseNodeDef):
    """`kolsch/nodes/workflow` — sub-workflow invocation."""

    type: Literal["kolsch/nodes/workflow"]
    ref: str


class MapNodeDef(_BaseNodeDef):
    """`kolsch/nodes/map` — fan-out over a list.

    `parallel: false` (default) is the sequential iteration contract.
    `parallel: true` opts into the concurrent worker dispatch;
    `concurrency: int | None` caps the number of in-flight iterations
    (`None` means unbounded). The parser accepts non-positive
    `concurrency:` for shape only — the run-start gate raises
    [`WorkflowInvalidConcurrency`][workflows.errors.WorkflowInvalidConcurrency].
    """

    type: Literal["kolsch/nodes/map"]
    apply: str
    over: Any
    item_as: str
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    parallel: bool = False
    concurrency: int | None = None


class BranchNodeDef(_BaseNodeDef):
    """`kolsch/nodes/branch` — boolean route gate.

    `condition:` accepts a YAML literal OR a Jinja-marked string. A
    literal `true` / `false` is valid and evaluates as-is.
    """

    type: Literal["kolsch/nodes/branch"]
    condition: Any


class CopyOfNodeDef(_BaseNodeDef):
    """A node defined via `copy_of: <other_node_name>` rather than `type:`.

    The full type + config are resolved by the validator from the
    source node; this model carries the override fields as raw extras.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    copy_of: str


# ---------------------------------------------------------------------------
# NodeDef dispatcher.
# ---------------------------------------------------------------------------


_BUILTIN_NODE_CLASSES: dict[str, type[_BaseNodeDef]] = {
    "bash": BashNodeDef,
    "browse_url": BrowseUrlNodeDef,
    "agent": AgentNodeDef,
    "user_input": UserInputNodeDef,
    "noop": NoopNodeDef,
    "read_file": ReadFileNodeDef,
    "write_file": WriteFileNodeDef,
    "list_files": ListFilesNodeDef,
    "set_state": SetStateNodeDef,
    "delete_state": DeleteStateNodeDef,
    "filter": FilterNodeDef,
    "compute": ComputeNodeDef,
    "workflow": WorkflowNodeDef,
    "map": MapNodeDef,
    "branch": BranchNodeDef,
}


NodeDefUnion = Union[  # noqa: UP007 — Annotated unions over Pydantic models
    BashNodeDef,
    BrowseUrlNodeDef,
    AgentNodeDef,
    UserInputNodeDef,
    NoopNodeDef,
    ReadFileNodeDef,
    WriteFileNodeDef,
    ListFilesNodeDef,
    SetStateNodeDef,
    DeleteStateNodeDef,
    FilterNodeDef,
    ComputeNodeDef,
    WorkflowNodeDef,
    MapNodeDef,
    BranchNodeDef,
    CopyOfNodeDef,
    CatalogNodeDef,
]


def _dispatch_node_def(value: Any) -> Any:  # noqa: ANN401
    """Route a raw `nodes:` entry to the matching Pydantic class.

    Called as the `BeforeValidator` on `NodeDef`. The returned value
    is fed to Pydantic's standard validation, so each candidate class
    still gets its `extra="forbid"` policy and field type-checks. The
    dispatcher itself is responsible for:

    - `copy_of:` without `type:` → `CopyOfNodeDef`.
    - `type:` matching a built-in `kolsch/nodes/<name>` slot → that
      slot's `*NodeDef`.
    - `type:` matching any other 3-seg catalog ref → `CatalogNodeDef`.
    - Non-3-seg `type:` (including bare `type: bash`) →
      `WorkflowInvalidRef`.

    Already-constructed Pydantic instances pass through unchanged so
    programmatic construction works alongside YAML loads.
    """
    if isinstance(value, BaseModel):
        return value
    if not isinstance(value, dict):
        msg = (
            "Workflow node entry must be a mapping. "
            "Fix: use a YAML mapping (key: value pairs) for each "
            "entry under 'nodes:'."
        )
        raise WorkflowValidationError(msg)

    # Reject the deprecated `instance_of:` key here at the dispatch
    # site — the BeforeValidator runs ahead of per-class validators,
    # and a YAML carrying ONLY `instance_of:` (no `type:`, no
    # `copy_of:`) would otherwise fall through to the "missing type"
    # error and bury the rename fix-path.
    if "instance_of" in value:
        msg = (
            "Workflow node uses deprecated key 'instance_of'. "
            "Fix: rename to 'copy_of' (see docs/workflows#node-types)."
        )
        raise WorkflowValidationError(msg)

    type_ref = value.get("type")
    copy_of = value.get("copy_of")

    if copy_of is not None and type_ref is None:
        return CopyOfNodeDef.model_validate(value)

    if not isinstance(type_ref, str) or not type_ref:
        msg = (
            "Workflow node is missing 'type:' (and no 'copy_of:' is "
            "set). Fix: add a 'type:' field with a 3-segment catalog "
            "ref such as 'kolsch/nodes/bash' (see docs/workflows#node-types)."
        )
        raise WorkflowValidationError(msg)

    if not is_three_segment(type_ref):
        # Non-3-seg refs are rejected with file:line tracked by the
        # loader. Without YAML origin info we use placeholders;
        # `load_workflow` re-raises with real file/line by inspecting
        # the surrounding YAML.
        raise WorkflowInvalidRef(file="<workflow>", line=0, ref=type_ref)

    builtin_name = builtin_name_for_ref(type_ref)
    if builtin_name is not None:
        return _BUILTIN_NODE_CLASSES[builtin_name].model_validate(value)

    # Any other 3-seg ref → authored Python node (CatalogNodeDef).
    return CatalogNodeDef.model_validate(value)


NodeDef = Annotated[NodeDefUnion, BeforeValidator(_dispatch_node_def)]


# ---------------------------------------------------------------------------
# EdgeDef.
# ---------------------------------------------------------------------------


class EdgeDef(BaseModel):
    """One graph edge.

    Fields are `{from, to, binds, branch, when}`. Fan-in is
    structural — multiple edges sharing a `to:` value. Edge-level
    `condition:` was renamed to `when:`; supplying the old name
    surfaces a fix-path parse error.

    `binds:` RHS follows the bare-or-Jinja convention: a YAML literal
    OR a Jinja-marked string (`"{{ ... }}"`). The runtime evaluates
    only the Jinja-marked form; literals flow as-is to the receiver's
    `inputs.*` namespace.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", populate_by_name=True
    )

    from_: str = Field(alias="from")
    to: str
    binds: dict[str, Any] = Field(default_factory=dict)
    branch: bool | None = None
    when: Any = None

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_keys(cls, data: Any) -> Any:  # noqa: ANN401
        """Reject deprecated edge-level keys with fix-path messages.

        - `condition` → `when` rename.
        - `ref` → dropped (multi-input edges are the v1 fan-in
          mechanism).
        """
        if isinstance(data, dict):
            if "condition" in data:
                msg = (
                    "Workflow edge uses deprecated key 'condition'. "
                    "Fix: rename the edge-level gate to 'when' "
                    "(node-level 'condition' on branch/filter is "
                    "unchanged; see docs/workflows#fan-in)."
                )
                raise WorkflowValidationError(msg)
            if "ref" in data:
                msg = (
                    "Workflow edge uses deprecated key 'ref'. "
                    "Fix: express fan-in by adding another edge into "
                    "the same target node — multi-input edges are the "
                    "v1 fan-in mechanism (see docs/workflows#fan-in)."
                )
                raise WorkflowValidationError(msg)
        return data


# ---------------------------------------------------------------------------
# Workflow + GraphDef.
# ---------------------------------------------------------------------------


class GraphDef(BaseModel):
    """The `graph:` block — a single `edges` field (may be empty).

    Presence of the parent `graph:` key on the YAML is what flips the
    workflow from `node_library` to `workflow`, not the contents of
    `edges`.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    edges: list[EdgeDef] = Field(default_factory=list)


class Workflow(BaseModel):
    """The top-level workflow object.

    `graph: GraphDef | None` — `None` means the `graph:` key was
    absent from the source YAML, which makes this a node library. Any
    present `graph:` value (including empty `edges: []`) makes the
    workflow runnable.

    The top-level YAML field is `policy:` (renamed from
    `permissions:`); the OLD `permissions:` key is rejected at parse
    with a fix-path message.

    `timeout_seconds: int | None` carries the workflow-level
    cooperative deadline. The validator rejects non-positive values
    with `WorkflowInvalidTimeout`; the parser accepts them for shape
    only.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    description: str = ""
    extends: list[str] = Field(default_factory=list)
    policy: list[Rule] = Field(default_factory=list)
    timeout_seconds: int | None = None
    # Workflow-level concurrency mode. `sequential` (the default) is
    # the long-standing behavior preserved byte-for-byte; `auto` opts
    # into the concurrent worklist (the run-start gate enforces safety
    # invariants).
    concurrency: Literal["sequential", "auto"] = "sequential"
    nodes: dict[str, NodeDef] = Field(default_factory=dict)
    graph: GraphDef | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_permissions_key(cls, data: Any) -> Any:  # noqa: ANN401
        """Reject the deprecated `permissions:` key with a fix-path message.

        Silently accepting it would defeat the migration goal.
        """
        if isinstance(data, dict) and "permissions" in data:
            msg = (
                "Workflow top-level uses deprecated key 'permissions'. "
                "Fix: rename to 'policy' (the field shape is unchanged; "
                "see docs/workflows#policy)."
            )
            raise WorkflowValidationError(msg)
        return data

    @property
    def kind(self) -> Literal["workflow", "node_library"]:
        """Runnable iff the `graph:` key was present in the source YAML.

        Empty `edges:` is NOT a library marker — a single-node
        workflow with `graph: { edges: [] }` resolves to `"workflow"`.
        """
        return "workflow" if self.graph is not None else "node_library"


# ---------------------------------------------------------------------------
# Loader.
# ---------------------------------------------------------------------------


def load_workflow(path: Path) -> Workflow:
    """Parse a workflow YAML from disk into a [`Workflow`][workflows.parser.Workflow].

    Reads via `_yamlio.load` (ruamel.yaml under the hood). Surfaces
    YAML parse failures with file path + line number; Pydantic
    validation failures bubble out as
    [`WorkflowValidationError`][workflows.errors.WorkflowValidationError]
    (when raised by a per-field validator) or as Pydantic's
    `ValidationError` (for shape mismatches Pydantic catches
    directly).

    SHAPE validation only — DAG checks, predecessor / bind-collision /
    branching / timeout-sign checks land in the validator.

    Args:
        path: The YAML file to load.

    Returns:
        The parsed workflow.

    Raises:
        WorkflowValidationError: The YAML failed to read, parse, or
            validate.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = (
            f"Workflow file at {path} could not be read: {exc}. "
            "Fix: confirm the file exists and is readable "
            "(see docs/workflows#workflow-files)."
        )
        raise WorkflowValidationError(msg) from exc

    try:
        payload = load(text)
    except YAMLError as exc:
        line = _extract_yaml_line(exc)
        msg = (
            f"Workflow YAML at {path}:{line} failed to parse: {exc}. "
            "Fix: correct the YAML syntax at the line indicated "
            "(see docs/workflows#workflow-files)."
        )
        raise WorkflowValidationError(msg) from exc

    if payload is None:
        msg = (
            f"Workflow file at {path}:1 is empty. "
            "Fix: add at least a 'nodes:' block "
            "(see docs/workflows#workflow-files)."
        )
        raise WorkflowValidationError(msg)
    if not isinstance(payload, dict):
        msg = (
            f"Workflow YAML at {path}:1 must be a top-level mapping; "
            f"got {type(payload).__name__}. "
            "Fix: use 'key: value' pairs at the top level "
            "(see docs/workflows#workflow-files)."
        )
        raise WorkflowValidationError(msg)

    try:
        return Workflow.model_validate(payload)
    except ValidationError as exc:
        # Re-wrap Pydantic shape errors as a single WorkflowValidationError
        # so callers see one exception class for parse-time failures.
        # The original ValidationError remains accessible via __cause__.
        msg = (
            f"Workflow YAML at {path} failed validation: {exc}. "
            "Fix: correct the fields named in the error message "
            "(see docs/workflows#workflow-files)."
        )
        raise WorkflowValidationError(msg) from exc


def _extract_yaml_line(exc: YAMLError) -> int:
    """Best-effort line-number extraction from a ruamel.yaml error.

    ruamel.yaml's error classes (e.g., `ScannerError`) expose a
    `problem_mark` attribute carrying the source position; when
    present we lift the line number off it (1-indexed). Returns `1`
    when the position is not available.
    """
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return 1
    return int(getattr(mark, "line", 0)) + 1

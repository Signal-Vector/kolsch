"""Workflow YAML parser — Phase 3.0 Step 2 (master spec §6).

Pydantic models for the workflow surface + :func:`load_workflow`.
SHAPE validation only — DAG / binds / branching / predecessor /
bind-collision / timeout-sign checks land in the validator (Step 5).

Layout:

- Per-built-in :class:`*NodeDef` for the 14 reserved
  ``kolsch/nodes/<name>`` types, each paired 1:1 with its
  :class:`*Inputs(BaseModel)` (Round 5 H8).
- :class:`CopyOfNodeDef` for nodes that reuse another node's
  type via ``copy_of:`` (semantic resolution at Step 5).
- :class:`CatalogNodeDef` (imported from
  :mod:`workflows.catalog_node`, Round 4 item #2 standalone module)
  for authored Python nodes referenced by 3-seg catalog ref.
- :class:`EdgeDef` (Round 4 item #11: ``{from, to, binds, branch,
  when}`` — NO ``ref:`` field; multi-input edges are the v1 fan-in
  mechanism).
- :class:`GraphDef` + :class:`Workflow` (top-level).

The ``type:`` discriminator routes raw dicts to the right class via
:func:`_parse_node_def`. Built-in 3-seg refs land on the matching
``*NodeDef``; any other 3-seg ref lands on :class:`CatalogNodeDef`;
non-3-seg input raises :class:`WorkflowInvalidRef` (R3 #5).

Loader: :func:`load_workflow` reads YAML via :mod:`_yamlio` (the
Step 0 helper; ruamel.yaml under the hood) and surfaces parse
failures with file path + line number.
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
# Per-built-in Inputs models — Round 5 H8 + master spec §4.0.2.
#
# Each `*Inputs(BaseModel)` declares the locked `model_config.extra`
# policy and any strict-typed fields the spec table pins. Phase 3.0
# ships ONLY the classes; runtime strict-mode validation (the
# `WorkflowBindTypeMismatch` contract) fires at node-fire time in
# Phase 3.1+. Co-located with their `*NodeDef` so callers import both
# from one module (`from workflows.parser import BashNodeDef,
# BashInputs`).
# ---------------------------------------------------------------------------


class BashInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class AgentInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class WorkflowSubInputs(BaseModel):
    """Round 5 H8 — name avoids collision with the ``workflow`` asset
    model (workflow-as-asset is the catalogued kind; this is the
    receiver-side input model for the ``kolsch/nodes/workflow``
    sub-workflow node)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class NoopInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class UserInputInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class ReadFileInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class WriteFileInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class ListFilesInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class SetStateInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class DeleteStateInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class FilterInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    items: list[Any] = Field(default_factory=list)


class MapInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    over: list[Any] = Field(default_factory=list)


class BranchInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class ComputeInputs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Common base — master spec §6.3 modifier fields.
# ---------------------------------------------------------------------------


class _BaseNodeDef(BaseModel):
    """Common modifier fields shared by every built-in ``*NodeDef``.

    Per Step 2 spec line 988 + R3 #2 / R3 #7 — every node accepts
    optional ``description`` (``min_length=1`` validation), optional
    positive-ish ``timeout_seconds`` (Step 5 enforces the sign), an
    optional ``when:`` gate modifier (YAML literal OR Jinja-marked
    string per Round 6 J5/β), ``retry`` (int; v1 thin facade), and
    ``secrets`` (declared list of secret-store keys the node may
    read from its provider; runtime resolution at Phase 3.1+).

    ``model_config = ConfigDict(extra="forbid")`` is the per-type
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

        - ``instance_of`` → fix-path to ``copy_of`` (Round 4 item #?).
        - ``collect`` → fix-path explaining fan-in via multi-input edges
          (Round 4 item #11; the field is gone from NodeDef).
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
# The 14 built-in *NodeDef classes — master spec §4.
# ---------------------------------------------------------------------------


class BashNodeDef(_BaseNodeDef):
    """``kolsch/nodes/bash`` — shell command (spec §4.1.1)."""

    type: Literal["kolsch/nodes/bash"]
    command: str
    cwd: str | None = None
    env: dict[str, Any] | None = None


class AgentNodeDef(_BaseNodeDef):
    """``kolsch/nodes/agent`` — invoke a catalogued agent (spec §4.1.2)."""

    type: Literal["kolsch/nodes/agent"]
    agent: str
    # Per-invocation `outputs:` override reuses `IOSpec` from
    # src/schemas/config.py:58 — no new grammar.
    outputs: IOSpec | None = None


class UserInputNodeDef(_BaseNodeDef):
    """``kolsch/nodes/user_input`` — pause and await chat reply
    (spec §4.1.3)."""

    type: Literal["kolsch/nodes/user_input"]
    prompt: str


class NoopNodeDef(_BaseNodeDef):
    """``kolsch/nodes/noop`` — fan-out placeholder (spec §4.1.4)."""

    type: Literal["kolsch/nodes/noop"]


class ReadFileNodeDef(_BaseNodeDef):
    """``kolsch/nodes/read_file`` — read file content (spec §4.2.1)."""

    type: Literal["kolsch/nodes/read_file"]
    path: str


class WriteFileNodeDef(_BaseNodeDef):
    """``kolsch/nodes/write_file`` — write file content (spec §4.2.2)."""

    type: Literal["kolsch/nodes/write_file"]
    path: str
    content: str | bytes


class ListFilesNodeDef(_BaseNodeDef):
    """``kolsch/nodes/list_files`` — enumerate directory entries
    (spec §4.2.3)."""

    type: Literal["kolsch/nodes/list_files"]
    path: str
    pattern: str | None = None


class SetStateNodeDef(_BaseNodeDef):
    """``kolsch/nodes/set_state`` — write workflow state keys
    (spec §4.3.1). Values follow Round 6 J5/β: bare YAML literal OR
    Jinja-marked string."""

    type: Literal["kolsch/nodes/set_state"]
    keys: dict[str, Any]


class DeleteStateNodeDef(_BaseNodeDef):
    """``kolsch/nodes/delete_state`` — remove workflow state keys
    (spec §4.3.2)."""

    type: Literal["kolsch/nodes/delete_state"]
    keys: list[str]


class FilterNodeDef(_BaseNodeDef):
    """``kolsch/nodes/filter`` — predicate-based list reducer
    (spec §4.4.1)."""

    type: Literal["kolsch/nodes/filter"]
    over: Any
    condition: Any
    item_as: str = "item"


class ComputeNodeDef(_BaseNodeDef):
    """``kolsch/nodes/compute`` — general-purpose Jinja expression
    (spec §4.5.1).

    Round 6 J5/β — ``value:`` accepts a YAML literal OR a Jinja-marked
    string ``"{{ expr }}"``. Only the latter is evaluated under the
    sandboxed env at runtime; literals pass through as the captured
    output ``{"value": <literal>}``.
    """

    type: Literal["kolsch/nodes/compute"]
    value: Any


class WorkflowNodeDef(_BaseNodeDef):
    """``kolsch/nodes/workflow`` — sub-workflow invocation
    (spec §4.6.1)."""

    type: Literal["kolsch/nodes/workflow"]
    ref: str


class MapNodeDef(_BaseNodeDef):
    """``kolsch/nodes/map`` — fan-out over a list (spec §4.6.2)."""

    type: Literal["kolsch/nodes/map"]
    apply: str
    over: Any
    item_as: str
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


class BranchNodeDef(_BaseNodeDef):
    """``kolsch/nodes/branch`` — boolean route gate (spec §4.6.3).

    Round 6 J5/β — ``condition:`` accepts a YAML literal OR a
    Jinja-marked string. A literal ``true`` / ``false`` is valid and
    evaluates as-is.
    """

    type: Literal["kolsch/nodes/branch"]
    condition: Any


class CopyOfNodeDef(_BaseNodeDef):
    """A node defined via ``copy_of: <other_node_name>`` rather than
    ``type:`` (master spec §6.3). The full type + config are resolved
    by Step 5 from the source node; this model carries the override
    fields as raw extras for now.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    copy_of: str


# ---------------------------------------------------------------------------
# NodeDef dispatcher — Round 4 items #2 + #3 + R3 #5.
# ---------------------------------------------------------------------------


_BUILTIN_NODE_CLASSES: dict[str, type[_BaseNodeDef]] = {
    "bash": BashNodeDef,
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
    """Route a raw ``nodes:`` entry to the matching Pydantic class.

    Called as the :class:`BeforeValidator` on :data:`NodeDef`. The
    returned value is fed to Pydantic's standard validation, so each
    candidate class still gets its ``extra="forbid"`` policy and field
    type-checks. The dispatcher itself is responsible for:

    - ``copy_of:`` without ``type:`` → :class:`CopyOfNodeDef`.
    - ``type:`` matching a built-in ``kolsch/nodes/<name>`` slot →
      that slot's ``*NodeDef``.
    - ``type:`` matching any other 3-seg catalog ref → :class:`CatalogNodeDef`.
    - Non-3-seg ``type:`` (including bare ``type: bash``) →
      :class:`WorkflowInvalidRef`.

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
        # R3 #5 — non-3-seg refs are rejected with file:line tracked by
        # the loader. Without YAML origin info we use placeholders;
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
# EdgeDef — Round 4 item #11.
# ---------------------------------------------------------------------------


class EdgeDef(BaseModel):
    """One graph edge.

    Round 4 item #11 multi-input redesign — ``{from, to, binds,
    branch, when}``. The prior ``ref: list[str]`` field is DROPPED;
    fan-in is structural (multiple edges sharing a ``to:`` value).
    Edge-level ``condition:`` was renamed to ``when:``; supplying the
    old name surfaces a fix-path parse error.

    ``binds:`` RHS follows Round 6 J5/β — bare YAML literal OR
    Jinja-marked string (``"{{ ... }}"``). The runtime evaluates
    only the Jinja-marked form; literals flow as-is to the receiver's
    ``inputs.*`` namespace.
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

        - ``condition`` → ``when`` rename.
        - ``ref`` → dropped (multi-input edges are the v1 fan-in mechanism).
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
# Workflow + GraphDef — master spec §6.1, §6.2.
# ---------------------------------------------------------------------------


class GraphDef(BaseModel):
    """The ``graph:`` block — master spec §6.4.

    A single ``edges`` field (may be empty). Presence of the parent
    ``graph:`` key on the YAML is what flips the workflow from
    ``node_library`` to ``workflow`` (§6.2), not the contents of
    ``edges``.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    edges: list[EdgeDef] = Field(default_factory=list)


class Workflow(BaseModel):
    """The top-level workflow object — master spec §6.1.

    ``graph: GraphDef | None`` — ``None`` means the ``graph:`` key was
    absent from the source YAML, which makes this a node library
    (§6.2). Any present ``graph:`` value (including empty
    ``edges: []``) makes the workflow runnable.

    Round 5 H1 — top-level YAML field is ``policy:`` (renamed from
    ``permissions:``); the OLD ``permissions:`` key is rejected at
    parse with a fix-path message. Internal Python identifiers
    (``RuleBasedAuthorizationProvider``, ``providers.authz``) remain
    UNCHANGED at Phase 3.0; the code-side rename ships separately.

    R3 #2 — top-level ``timeout_seconds: int | None`` carries the
    workflow-level cooperative deadline. Step 5 rejects non-positive
    values with ``WorkflowInvalidTimeout``; the parser accepts them
    for shape only.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    description: str = ""
    extends: list[str] = Field(default_factory=list)
    policy: list[Rule] = Field(default_factory=list)
    timeout_seconds: int | None = None
    nodes: dict[str, NodeDef] = Field(default_factory=dict)
    graph: GraphDef | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_permissions_key(cls, data: Any) -> Any:  # noqa: ANN401
        """Round 5 H1 — reject the OLD ``permissions:`` key with a
        fix-path message suggesting the rename. Silently accepting it
        would defeat the migration goal."""
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
        """Master spec §6.2 — runnable iff the ``graph:`` key was
        present in the source YAML.

        Empty ``edges:`` is NOT a library marker (polish-pass Decision
        P3 — a single-node workflow with ``graph: { edges: [] }``
        resolves to ``"workflow"``)."""
        return "workflow" if self.graph is not None else "node_library"


# ---------------------------------------------------------------------------
# Loader.
# ---------------------------------------------------------------------------


def load_workflow(path: Path) -> Workflow:
    """Parse a workflow YAML from disk into a :class:`Workflow`.

    Reads via :func:`_yamlio.load` (ruamel.yaml under the hood, the
    Step 0 helper). Surfaces YAML parse failures with file path +
    line number; Pydantic validation failures bubble out as
    :class:`WorkflowValidationError` (when raised by a per-field
    validator) or as Pydantic's :class:`ValidationError` (for shape
    mismatches Pydantic catches directly).

    Step 2 does SHAPE validation only. DAG checks, predecessor /
    bind-collision / branching / timeout-sign checks land in Step 5.
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

    ruamel.yaml's error classes (e.g.,
    :class:`ruamel.yaml.scanner.ScannerError`) expose a ``problem_mark``
    attribute carrying the source position; when present we lift the
    line number off it (1-indexed). Returns ``1`` when the position is
    not available.
    """
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return 1
    return int(getattr(mark, "line", 0)) + 1

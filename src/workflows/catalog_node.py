"""Generic `CatalogNodeDef` plus the 3-seg `type:` dispatcher.

Standalone module hosting the catalog-ref dispatcher (NOT folded into
`workflows.parser`). The parser imports `BUILTIN_TYPE_NAMES`,
`builtin_name_for_ref`, and `CatalogNodeDef` from here.

The 16 RESERVED catalog slots `kolsch/nodes/<name>` belong to the
built-in node types declared in `workflows.parser`. Authored Python
nodes live at any other `<bundle>/nodes/<name>`; parse-time they land
in [`CatalogNodeDef`][workflows.catalog_node.CatalogNodeDef] carrying
only the common modifier fields. Their type-specific config is
validated against the node's declared input model AFTER catalog
lookup at the run-start gate.

[`introspect_node_module`][workflows.catalog_node.introspect_node_module]
exposes the load-and-introspect pipeline that the run-start gate
already performs internally. The `kolsch/tools/introspect_node` bundle
tool wraps this helper so the `author_workflow` meta-agent can
sanity-check a freshly-written `@node` module BEFORE running the full
workflow validator.
"""

from __future__ import annotations

import importlib.util
import inspect
import typing
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from assets.refs import is_three_segment, parse_three_segment

__all__ = [
    "BUILTIN_TYPE_NAMES",
    "CatalogNodeDef",
    "NodeIntrospectionResult",
    "builtin_name_for_ref",
    "introspect_node_module",
    "is_builtin_ref",
]


# The 16 reserved built-in catalog slots. `kolsch/nodes/<name>` for
# each `<name>` here is owned by the built-in node-type model (e.g.,
# `BashNodeDef`); a user authoring `bundles/kolsch/nodes/bash.py`
# collides with the reserved slot at catalog scan time
# (`WorkflowBuiltinShadowed`).
BUILTIN_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "agent",
        "bash",
        "branch",
        "browse_url",
        "compute",
        "delete_state",
        "filter",
        "jev",
        "list_files",
        "map",
        "noop",
        "read_file",
        "set_state",
        "user_input",
        "workflow",
        "write_file",
    }
)


def is_builtin_ref(type_ref: str) -> bool:
    """Return `True` iff `type_ref` is a reserved built-in 3-seg slot.

    A built-in slot is `kolsch/nodes/<name>` for one of the 16
    built-in names.
    """
    name = builtin_name_for_ref(type_ref)
    return name is not None


def builtin_name_for_ref(type_ref: str) -> str | None:
    """Return the built-in name when `type_ref` is a reserved slot.

    Returns the short name (`"bash"`) for one of the 16 reserved
    `kolsch/nodes/<name>` slots, or `None` for non-3-seg refs,
    non-`kolsch` bundles, and 3-seg refs whose final segment is not in
    `BUILTIN_TYPE_NAMES`.
    """
    if not isinstance(type_ref, str) or not is_three_segment(type_ref):
        return None
    bundle, segment, name = parse_three_segment(type_ref)
    if bundle != "kolsch" or segment != "nodes":
        return None
    if name not in BUILTIN_TYPE_NAMES:
        return None
    return name


class CatalogNodeDef(BaseModel):
    """Parser variant for `type: <bundle>/nodes/<name>` refs.

    Used for refs that are NOT reserved built-in slots — symmetric in
    shape with `BashNodeDef` / `MapNodeDef` / etc. The model carries
    only the common modifier fields; type-specific config is validated
    against the node's declared input model AFTER catalog lookup at
    the run-start gate.

    The `type:` field IS the catalog ref string. There is no `body:`
    wrapper — the ref alone is the discriminator.
    `model_config = ConfigDict(extra="allow")` so type-specific config
    fields land on the model untouched; the run-start gate inspects
    them against the loaded node's Pydantic input model.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    type: str
    description: str | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = None
    when: Any = None
    retry: int = 0
    secrets: list[str] = Field(default_factory=list)
    copy_of: str | None = None


# ---------------------------------------------------------------------------
# Public node-module introspection helper.
# ---------------------------------------------------------------------------


# `run` MUST accept 2 or 3 positional params (Inputs, NodeContext,
# optional Config). Named constants here so the signature check reads
# intent rather than literal magic numbers (ruff PLR2004). Mirrors the
# validator's private constants.
_MIN_RUN_PARAMS = 2
_MAX_RUN_PARAMS = 3
_TUPLE_RETURN_LEN = 2


@dataclass(frozen=True, slots=True)
class NodeIntrospectionResult:
    """Structured outcome of `introspect_node_module`.

    `signature_ok` is `True` iff every check passed: the file
    imported, exposed `run`, `run` carried the `@node` sentinel, the
    signature matched the authored-node contract, and the return
    annotation matched the allowed shapes. Whenever `signature_ok` is
    `False`, `errors` carries one or more single-line diagnostic
    strings.

    `label` is the `@node(label=...)` value when introspection reached
    that far (`None` otherwise — including the case where `@node` was
    applied without a `label` argument).

    `inputs_schema` / `outputs_schema` are JSON-Schema dicts via
    `pydantic.BaseModel.model_json_schema` of the inputs model (param
    1) and the outputs model (the `BaseModel` half of the return
    annotation). Either may be `None` when introspection couldn't
    reach that field.
    """

    signature_ok: bool
    label: str | None = None
    inputs_schema: dict[str, Any] | None = None
    outputs_schema: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


def introspect_node_module(path: Path) -> NodeIntrospectionResult:
    """Load an authored `@node` module and return its signature info.

    Mirrors the run-start gate's
    `workflows.validator._check_catalog_node_defs` pipeline: import
    the file, look up `run`, verify the `@node` sentinel, check the
    signature, check the return annotation. Returns a
    `NodeIntrospectionResult` instead of raising — the tool wrapper
    surfaces the failures as a structured response to the meta-agent.

    Errors are returned as single-line strings rather than
    [`WorkflowValidationError`][workflows.errors.WorkflowValidationError]
    subclasses because the tool's audience (an LLM iterating on a
    freshly-written file) reads plain prose more easily than the
    YAML-origin format.

    The module is loaded under a synthetic `_kolsch_introspect_<id>`
    name so two consecutive introspections of different files (with
    the same stem) don't collide in `sys.modules`. The
    [`RunModuleNamespace`][workflows.module_isolation.RunModuleNamespace]
    discipline only applies to executor-driven runs; the introspection
    tool is pure-read and doesn't need per-run isolation.

    Args:
        path: The on-disk node module to introspect.

    Returns:
        A `NodeIntrospectionResult` describing the outcome.
    """
    if not path.exists() or not path.is_file():
        return NodeIntrospectionResult(
            signature_ok=False,
            errors=[f"FileNotFoundError: node module not found at {path}"],
        )

    module_or_exc = _safe_import_module(path)
    if isinstance(module_or_exc, Exception):
        return NodeIntrospectionResult(
            signature_ok=False,
            errors=[f"{type(module_or_exc).__name__}: {module_or_exc}"],
        )

    module = module_or_exc
    run_fn = getattr(module, "run", None)
    if run_fn is None:
        return NodeIntrospectionResult(
            signature_ok=False,
            errors=["MissingRun: module does not define a top-level 'run' function"],
        )

    node_def = getattr(run_fn, "_kolsch_node", None)
    if node_def is None:
        return NodeIntrospectionResult(
            signature_ok=False,
            errors=["MissingDecorator: 'run' is not decorated with @node"],
        )

    sig_error = _check_signature(run_fn)
    if sig_error is not None:
        # When the signature itself is broken we still surface the
        # `label` because it's known from the decorator and useful for
        # the meta-agent's prose.
        return NodeIntrospectionResult(
            signature_ok=False,
            label=getattr(node_def, "label", None),
            errors=[sig_error],
        )

    inputs_schema, outputs_schema, return_error = _schemas_from_run(run_fn)
    if return_error is not None:
        return NodeIntrospectionResult(
            signature_ok=False,
            label=getattr(node_def, "label", None),
            inputs_schema=inputs_schema,
            errors=[return_error],
        )

    return NodeIntrospectionResult(
        signature_ok=True,
        label=getattr(node_def, "label", None),
        inputs_schema=inputs_schema,
        outputs_schema=outputs_schema,
        errors=[],
    )


def _safe_import_module(path: Path) -> ModuleType | Exception:
    """Import `path` returning the loaded module or the captured exception."""
    try:
        spec = importlib.util.spec_from_file_location(
            f"_kolsch_introspect_{abs(hash(str(path.resolve())))}_{path.stem}",
            path,
        )
        if spec is None or spec.loader is None:
            return ImportError(f"could not load spec for {path}")
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


# PLR0911 acknowledged — each early-return carries a distinct
# diagnostic string. Collapsing into one exit would lose the
# per-failure detail the meta-agent needs to iterate.
def _check_signature(run_fn: Any) -> str | None:
    """Verify `run`'s signature matches the authored-node contract.

    Returns `None` on success or a one-line diagnostic string. Mirrors
    `workflows.validator._check_run_signature` but returns plain
    strings instead of typed errors.
    """
    # Local import to avoid the validator → catalog_node → context →
    # validator cycle that would otherwise form.
    from workflows.context import NodeContext  # noqa: PLC0415

    try:
        sig = inspect.signature(run_fn)
        hints = typing.get_type_hints(run_fn)
    except (TypeError, NameError) as exc:
        return f"InvalidSignature: unreadable signature: {exc}"
    params = list(sig.parameters.values())
    if len(params) < _MIN_RUN_PARAMS or len(params) > _MAX_RUN_PARAMS:
        return (
            f"InvalidSignature: expected {_MIN_RUN_PARAMS} or "
            f"{_MAX_RUN_PARAMS} parameters, got {len(params)}"
        )
    for p in params:
        if p.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ):
            return f"InvalidSignature: parameter {p.name!r} must be positional"
    inputs_ann = hints.get(params[0].name)
    if not isinstance(inputs_ann, type) or not issubclass(inputs_ann, BaseModel):
        return (
            f"InvalidSignature: param 1 ({params[0].name!r}) must be "
            "annotated with a pydantic.BaseModel subclass"
        )
    ctx_ann = hints.get(params[1].name)
    if ctx_ann is not NodeContext:
        return (
            f"InvalidSignature: param 2 ({params[1].name!r}) must be "
            "annotated exactly as NodeContext"
        )
    if len(params) == _MAX_RUN_PARAMS:
        config_ann = hints.get(params[2].name)
        if not isinstance(config_ann, type) or not issubclass(config_ann, BaseModel):
            return (
                f"InvalidSignature: optional param 3 ({params[2].name!r}) "
                "must be a pydantic.BaseModel subclass"
            )
    return None


def _schemas_from_run(
    run_fn: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Extract `inputs_schema` / `outputs_schema` plus a return-shape error.

    Returns `(inputs_schema, outputs_schema, error)`. `error` is
    `None` on success; on failure `outputs_schema` is `None` (the
    inputs schema was already validated by `_check_signature`, so
    it's safe to surface).
    """
    try:
        hints = typing.get_type_hints(run_fn)
    except (TypeError, NameError):
        return None, None, "InvalidReturnType: could not read return annotation"

    sig = inspect.signature(run_fn)
    params = list(sig.parameters.values())
    inputs_ann = hints.get(params[0].name)
    inputs_schema: dict[str, Any] | None = None
    if isinstance(inputs_ann, type) and issubclass(inputs_ann, BaseModel):
        inputs_schema = inputs_ann.model_json_schema()

    ret_ann = hints.get("return")
    if ret_ann is None:
        return (
            inputs_schema,
            None,
            ("InvalidReturnType: 'run' has no return annotation"),
        )
    if isinstance(ret_ann, type) and issubclass(ret_ann, BaseModel):
        return inputs_schema, ret_ann.model_json_schema(), None
    if ret_ann is bool:
        return inputs_schema, None, None
    origin = typing.get_origin(ret_ann)
    args = typing.get_args(ret_ann)
    if (
        origin is tuple
        and len(args) == _TUPLE_RETURN_LEN
        and args[0] is bool
        and isinstance(args[1], type)
        and issubclass(args[1], BaseModel)
    ):
        return inputs_schema, args[1].model_json_schema(), None
    return (
        inputs_schema,
        None,
        (
            f"InvalidReturnType: return annotation {ret_ann!r} is not one of "
            "BaseModel, bool, or tuple[bool, BaseModel]"
        ),
    )

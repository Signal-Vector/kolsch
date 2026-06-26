"""Phase 3.0 Step 1 — workflows package scaffolding tests.

Exercises every Step 1 acceptance criterion that touches the
``src/workflows/`` package: exception hierarchy + ClassVar
conventions, ``@node`` decorator shape checks, :class:`NodeContext`
dataclass + ``for_test`` classmethod, the typed prefixed IDs, the
sandboxed Jinja singleton, and the :func:`display_label` 3-step
fallback.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import uuid
from typing import TYPE_CHECKING, Any

import pytest

import workflows
from workflows import (
    InvocationId,
    NodeContext,
    NodeDefinition,
    NodeTimeoutExceeded,
    ToolCallId,
    TraceId,
    WorkflowAmbiguousStart,
    WorkflowBindCollision,
    WorkflowBindTypeMismatch,
    WorkflowBranchMissingArm,
    WorkflowBranchOnNonBranchable,
    WorkflowBuiltinShadowed,
    WorkflowCallCycle,
    WorkflowDuplicateEdge,
    WorkflowDuplicateNode,
    WorkflowError,
    WorkflowExtendsCycle,
    WorkflowExtendsDepthExceeded,
    WorkflowExtendsPolicyForbidden,
    WorkflowExtendsUnresolved,
    WorkflowFileAccessDenied,
    WorkflowFromAssetTypeError,
    WorkflowFromNotFound,
    WorkflowFromShapeError,
    WorkflowInputInvalid,
    WorkflowInvalidRef,
    WorkflowInvalidTimeout,
    WorkflowJinjaRefUnknown,
    WorkflowJinjaSecurityError,
    WorkflowMapperWorkerMissingField,
    WorkflowMapperWorkerNotAddressable,
    WorkflowNodeFailed,
    WorkflowNodeImportFailed,
    WorkflowNodeMissingRun,
    WorkflowNodeNotDecorated,
    WorkflowNodeNotDefined,
    WorkflowNodeReturnTypeInvalid,
    WorkflowNodeSignatureInvalid,
    WorkflowNoStartNode,
    WorkflowNotRunnable,
    WorkflowRunId,
    WorkflowRuntimeError,
    WorkflowSecretUnresolved,
    WorkflowTimeoutExceeded,
    WorkflowTimeoutShadowsNode,
    WorkflowUnreachableNode,
    WorkflowValidationError,
    WorkflowValidationWarning,
    display_label,
    get_node_definition,
    node,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

JINJA_OPEN = "{" + "{"
JINJA_BLOCK = "{" + "%"


def _yaml_origin_classes() -> list[type[WorkflowError]]:
    """Return every concrete YAML-origin (validation) error class."""
    out: list[type[WorkflowError]] = []
    stack: list[type[WorkflowError]] = [WorkflowValidationError]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub.__name__ != "WorkflowValidationError":
                out.append(sub)
            stack.append(sub)
    # WorkflowValidationWarning subclasses are also YAML-origin
    for sub in WorkflowValidationWarning.__subclasses__():
        out.append(sub)
    return out


# ---------------------------------------------------------------------------
# 1.1 — all classes importable; REMOVED classes are NOT importable
# ---------------------------------------------------------------------------


def test_every_class_in_master_spec_12_1_is_importable() -> None:
    # If any of these imports failed at module load, the module would
    # fail to import. Re-assert they are bound to classes (not None).
    classes = [
        WorkflowError,
        WorkflowValidationError,
        WorkflowValidationWarning,
        WorkflowRuntimeError,
        WorkflowDuplicateNode,
        WorkflowDuplicateEdge,
        WorkflowExtendsCycle,
        WorkflowExtendsDepthExceeded,
        WorkflowExtendsUnresolved,
        WorkflowExtendsPolicyForbidden,
        WorkflowNodeNotDefined,
        WorkflowAmbiguousStart,
        WorkflowNoStartNode,
        WorkflowUnreachableNode,
        WorkflowBindCollision,
        WorkflowBindTypeMismatch,
        WorkflowJinjaRefUnknown,
        WorkflowMapperWorkerNotAddressable,
        WorkflowBranchOnNonBranchable,
        WorkflowBranchMissingArm,
        WorkflowMapperWorkerMissingField,
        WorkflowCallCycle,
        WorkflowNodeImportFailed,
        WorkflowNodeMissingRun,
        WorkflowNodeNotDecorated,
        WorkflowNodeSignatureInvalid,
        WorkflowNodeReturnTypeInvalid,
        WorkflowInvalidTimeout,
        WorkflowInvalidRef,
        WorkflowBuiltinShadowed,
        WorkflowFromShapeError,
        WorkflowFromAssetTypeError,
        WorkflowFromNotFound,
        WorkflowJinjaSecurityError,
        WorkflowTimeoutShadowsNode,
        WorkflowNotRunnable,
        WorkflowInputInvalid,
        WorkflowNodeFailed,
        WorkflowSecretUnresolved,
        WorkflowFileAccessDenied,
        WorkflowTimeoutExceeded,
        NodeTimeoutExceeded,
    ]
    for cls in classes:
        assert isinstance(cls, type), cls
        assert issubclass(cls, WorkflowError)


@pytest.mark.parametrize(
    "name",
    [
        "WorkflowStateKeyCollision",
        "WorkflowMultiInputForbidden",  # R4 #11 removed
        "WorkflowFromShapeWarning",  # R4 #13 removed (promoted to hard errors)
        "WorkflowBindMustBeJinja",  # R6 J5/β removed
        "WorkflowNodeRefUnknown",  # R3 #9 renamed
        "WorkflowBranchDeadSide",  # R3 #9 renamed
        "WorkflowUnknownNodeRef",  # renamed to WorkflowNodeNotDefined
    ],
)
def test_removed_or_renamed_classes_not_importable(name: str) -> None:
    assert not hasattr(workflows, name), (
        f"{name} should NOT be importable from kolsch.workflows"
    )


# ---------------------------------------------------------------------------
# 1.2 — every exception STRICT single-line; YAML-origin embeds file:line + Fix:
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _yaml_origin_classes())
def test_yaml_origin_str_is_strict_single_line(cls: type[WorkflowError]) -> None:  # noqa: PLR0912
    # Build a minimal-args instance via reflection — pull init kwargs
    # from the dataclass-ish signature.
    sig = inspect.signature(cls)
    kwargs: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if pname == "file":
            kwargs[pname] = "workflows/x.yaml"
        elif pname == "line":
            kwargs[pname] = 42
        elif pname == "depth":
            kwargs[pname] = 17
        elif pname == "limit":
            kwargs[pname] = 16
        elif pname == "value":
            kwargs[pname] = -1
        elif pname == "node_timeout":
            kwargs[pname] = 30.0
        elif pname == "workflow_timeout":
            kwargs[pname] = 10.0
        elif pname == "scope":
            kwargs[pname] = "workflow"
        elif pname == "cycle":
            kwargs[pname] = ("a", "b", "a")
        elif pname == "candidates":
            kwargs[pname] = ("a", "b")
        elif pname == "sources":
            kwargs[pname] = ("from1", "from2")
        elif pname == "missing_arm":
            kwargs[pname] = "true"
        elif pname == "reason":
            kwargs[pname] = "not_predecessor"
        elif pname == "reference":
            kwargs[pname] = "nodes.a.x"
        elif pname == "from_ref":
            kwargs[pname] = "acme/bot"
        elif pname == "asset_type":
            kwargs[pname] = "tools"
        elif pname == "return_type":
            kwargs[pname] = "str"
        elif pname == "expected_type":
            kwargs[pname] = "int"
        elif pname == "actual_type":
            kwargs[pname] = "str"
        elif pname == "field":
            kwargs[pname] = "score"
        elif pname == "worker":
            kwargs[pname] = "worker"
        elif pname == "map_node":
            kwargs[pname] = "map_n"
        elif pname == "extended_ref":
            kwargs[pname] = "acme/workflows/parent"
        elif pname == "location":
            kwargs[pname] = "edge.from"
        elif pname == "from_node":
            kwargs[pname] = "fetch"
        elif pname == "to_node":
            kwargs[pname] = "merge"
        elif pname == "bind":
            kwargs[pname] = "url"
        elif pname == "node":
            kwargs[pname] = "summarize"
        elif pname == "ref":
            kwargs[pname] = "acme/nodes/parse"
        # Fallback: provide a string for any unmapped required kw
        elif param.default is inspect.Parameter.empty:
            kwargs[pname] = "x"
    exc = cls(**kwargs)
    rendered = str(exc)
    # AC 1.2 — strict single line
    assert "\n" not in rendered, rendered
    # YAML-origin format requirements
    assert f"{cls.__name__}:" in rendered
    assert "Fix: " in rendered
    # file:line token
    assert re.search(r":\d+", rendered), rendered
    # STRICT-no-code in Fix portion
    assert JINJA_OPEN not in rendered, rendered
    assert JINJA_BLOCK not in rendered, rendered
    assert "`" not in rendered, rendered


@pytest.mark.parametrize(
    ("cls", "kwargs"),
    [
        (WorkflowNodeFailed, {"node": "x", "invocation_id": "inv_1"}),
        (
            WorkflowSecretUnresolved,
            {"secret_name": "API_KEY", "node": "fetch"},
        ),
        (WorkflowFileAccessDenied, {"path": "/etc/passwd", "node": "rd"}),
        (
            WorkflowTimeoutExceeded,
            {"workflow_run_id": "wrk_1", "limit": 30.0},
        ),
        (
            NodeTimeoutExceeded,
            {"node": "x", "invocation_id": "inv_1", "limit": 5.0},
        ),
        (WorkflowNotRunnable, {"ref": "acme/workflows/x", "reason": "y"}),
        (WorkflowInputInvalid, {"ref": "acme/workflows/x", "reason": "y"}),
    ],
)
def test_runtime_str_is_single_line(
    cls: type[WorkflowError], kwargs: dict[str, Any]
) -> None:
    exc = cls(**kwargs)
    rendered = str(exc)
    assert "\n" not in rendered, rendered
    assert cls.__name__ in rendered


# ---------------------------------------------------------------------------
# 1.3 — wrapped causes via __cause__
# ---------------------------------------------------------------------------


def test_workflow_node_failed_wraps_cause() -> None:
    cause = ValueError("bad input")
    exc = WorkflowNodeFailed(node="x", invocation_id="inv_1", cause=cause)
    assert exc.__cause__ is cause


def test_raise_from_chains_cause() -> None:
    cause = RuntimeError("upstream failure")
    with pytest.raises(WorkflowInputInvalid) as exc_info:
        raise WorkflowInputInvalid(ref="acme/wf/x", reason="y") from cause
    assert exc_info.value.__cause__ is cause


# ---------------------------------------------------------------------------
# 1.4 / 1.5 / 1.6 — @node decorator behavior
# ---------------------------------------------------------------------------


def test_node_decorator_carries_secrets_needed() -> None:
    @node(secrets_needed=["api_key", "db_url"])
    def run(inputs: Any, ctx: NodeContext) -> Any:
        return None

    defn = get_node_definition(run)
    assert isinstance(defn, NodeDefinition)
    assert defn.secrets_needed == ["api_key", "db_url"]
    assert defn.label is None


def test_node_decorator_bare_defaults_to_empty_secrets() -> None:
    @node()
    def run(inputs: Any, ctx: NodeContext) -> Any:
        return None

    defn = get_node_definition(run)
    assert defn.secrets_needed == []


def test_node_decorator_label() -> None:
    @node(label="Run a thing")
    def run(inputs: Any, ctx: NodeContext) -> Any:
        return None

    assert get_node_definition(run).label == "Run a thing"


def test_node_decorator_rejects_non_callable() -> None:
    decorator = node(secrets_needed=[])
    with pytest.raises(TypeError):
        decorator("not a callable")


def test_node_decorator_rejects_bad_secrets_needed() -> None:
    with pytest.raises(TypeError):
        node(secrets_needed="api_key")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1.7 — NodeContext shape + frozen + slots + emit method
# ---------------------------------------------------------------------------


def test_node_context_is_frozen_slots_dataclass() -> None:
    assert dataclasses.is_dataclass(NodeContext)
    # slots: presence of __slots__ on the class
    assert hasattr(NodeContext, "__slots__")
    ctx = NodeContext.for_test()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.bundle = "evil"  # type: ignore[misc]


def test_node_context_for_test_defaults() -> None:
    ctx = NodeContext.for_test()
    assert ctx.workflow_run_id == "wrk_test"
    assert ctx.invocation_id == "inv_test"
    assert ctx.bundle == "test"
    assert ctx.workspace_root.exists()
    assert ctx.secrets == {}
    assert ctx.state == {}


def test_node_context_for_test_overrides() -> None:
    captured: list[tuple[str, str, Mapping[str, Any]]] = []

    def cap(bundle: str, subkind: str, payload: Mapping[str, Any]) -> None:
        captured.append((bundle, subkind, payload))

    ctx = NodeContext.for_test(
        bundle="acme",
        state={"threshold": 0.8},
        secrets={"api_key": "tk"},
        emit_fn=cap,
    )
    assert ctx.bundle == "acme"
    assert ctx.state["threshold"] == 0.8
    assert ctx.secrets["api_key"] == "tk"
    ctx.emit("custom.metric", {"k": 1})
    assert captured == [("acme", "custom.metric", {"k": 1})]


def test_node_context_emit_signature() -> None:
    sig = inspect.signature(NodeContext.emit)
    params = list(sig.parameters.values())
    # (self, subkind, payload) — annotations are string forms because
    # the dataclass module has ``from __future__ import annotations``.
    assert [p.name for p in params] == ["self", "subkind", "payload"]
    assert "str" in str(params[1].annotation)
    assert "Mapping" in str(params[2].annotation)


def test_node_context_emit_fn_excluded_from_repr() -> None:
    ctx = NodeContext.for_test(bundle="acme")
    assert "emit_fn" not in repr(ctx)


# ---------------------------------------------------------------------------
# 1.8 — WorkflowJinjaRefUnknown reason discriminator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "token"),
    [
        ("not_predecessor", "transitive ancestor"),
        ("unknown_field", "not in the source node's output schema"),
        ("not_declared", "output schema is unknown"),
    ],
)
def test_jinja_ref_unknown_reason_in_message(reason: str, token: str) -> None:
    exc = WorkflowJinjaRefUnknown(
        file="x.yaml",
        line=1,
        node="b",
        reference="nodes.a.x",
        reason=reason,
    )
    assert token in str(exc)
    assert exc.reason == reason


# ---------------------------------------------------------------------------
# 1.13 + 1.14 — caller_message + identifying_names_keys ClassVars
# ---------------------------------------------------------------------------


def _all_workflow_error_subclasses() -> list[type[WorkflowError]]:
    out: list[type[WorkflowError]] = [WorkflowError]
    stack = [WorkflowError]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            out.append(sub)
            stack.append(sub)
    return out


@pytest.mark.parametrize("cls", _all_workflow_error_subclasses())
def test_caller_message_classvar_style(cls: type[WorkflowError]) -> None:
    cm = cls.caller_message
    assert isinstance(cm, str) and cm
    assert cls.__name__ not in cm, f"{cls.__name__} mentions itself in caller_message"
    assert JINJA_OPEN not in cm
    assert JINJA_BLOCK not in cm
    assert "`" not in cm
    assert not re.search(r"\w+\.\w+:\d+", cm), (
        f"{cls.__name__} caller_message has file:line token"
    )
    # 1-3 sentences — count end punctuation
    sentence_count = len(re.findall(r"[.!?]", cm))
    assert 1 <= sentence_count <= 3, f"{cls.__name__} caller_message has {sentence_count} sentences"


_ABSTRACT_BASES: tuple[type[WorkflowError], ...] = (
    WorkflowError,
    WorkflowValidationError,
    WorkflowValidationWarning,
    WorkflowRuntimeError,
)


@pytest.mark.parametrize("cls", _all_workflow_error_subclasses())
def test_identifying_names_keys_resolve_to_init_fields(
    cls: type[WorkflowError],
) -> None:
    ink = cls.identifying_names_keys
    assert isinstance(ink, tuple)
    if cls in _ABSTRACT_BASES:
        # Abstract bases declare ClassVars but don't ship a concrete
        # __init__ — subclasses do. Empty tuple is the contract.
        assert ink == ()
        return
    sig = inspect.signature(cls)
    for key in ink:
        assert key in sig.parameters, (
            f"{cls.__name__}.identifying_names_keys lists {key!r} "
            f"but it's not an __init__ parameter"
        )


# ---------------------------------------------------------------------------
# 1.15 — Typed prefixed IDs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "prefix"),
    [
        (TraceId, "tr_"),
        (WorkflowRunId, "wrk_"),
        (InvocationId, "inv_"),
        (ToolCallId, "tc_"),
    ],
)
def test_typed_prefixed_ids(cls: type, prefix: str) -> None:
    valid = f"{prefix}{uuid.uuid4()}"
    instance = cls(valid)
    assert isinstance(instance, str)
    assert instance.startswith(prefix)
    with pytest.raises(ValueError):
        cls("wrong_prefix")


# ---------------------------------------------------------------------------
# 1.17 — Explicit __all__ + parity
# ---------------------------------------------------------------------------


def test_workflows_all_is_list_and_non_empty() -> None:
    assert isinstance(workflows.__all__, list)
    assert workflows.__all__
    # No duplicates
    assert len(workflows.__all__) == len(set(workflows.__all__))


def test_workflows_all_parity_with_module_namespace() -> None:
    # Exclude submodule re-exports (importable via dotted path) and the
    # ``annotations`` shim from ``from __future__ import annotations``.
    submodules = {
        "builtins",
        "catalog",
        "catalog_node",
        "context",
        "decorator",
        "errors",
        "events",
        "executor",
        "ids",
        "jinja_env",
        "labels",
        "module_isolation",
        "orphan_sweep",
        "parser",
        "run",
        "run_context",
        "run_store",
        "validator",
    }
    skip = submodules | {"annotations"}
    public_names = {
        name
        for name in dir(workflows)
        if not name.startswith("_") and name not in skip
    }
    declared = set(workflows.__all__)
    missing = public_names - declared
    extra = declared - public_names
    assert not missing, f"Public names not in __all__: {sorted(missing)}"
    assert not extra, f"__all__ names not bound: {sorted(extra)}"


# ---------------------------------------------------------------------------
# 1.16 — core/principals removed
# ---------------------------------------------------------------------------


def test_no_core_principals_refs_in_src() -> None:
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent / "src"
    result = subprocess.run(
        ["grep", "-rn", "core/principals", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", result.stdout


# ---------------------------------------------------------------------------
# 1.21 — display_label 3-step fallback
# ---------------------------------------------------------------------------


class _FakeNodeDef:
    description: str | None = None
    __name__ = "FakeNodeDef"


def test_display_label_per_instance_description_wins() -> None:
    fake = _FakeNodeDef()
    fake.description = "Custom per-instance label"
    assert display_label(fake, type_label="Type label") == "Custom per-instance label"


def test_display_label_type_label_wins_when_no_description() -> None:
    fake = _FakeNodeDef()
    assert display_label(fake, type_label="Shell") == "Shell"


def test_display_label_cleans_class_name_when_no_other_source() -> None:
    class BashNodeDef:
        description: str | None = None

    assert display_label(BashNodeDef()) == "bash"


def test_display_label_cleans_function_name() -> None:
    def my_authored_node() -> None:
        pass

    assert display_label(my_authored_node) == "my authored node"

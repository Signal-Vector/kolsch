"""Workflows package — public API.

The foundation layer: the `@node` decorator,
[`NodeContext`][workflows.context.NodeContext] + its `for_test`
classmethod, the [`display_label`][workflows.labels.display_label]
helper, the full exception hierarchy, the typed prefixed IDs, and the
sandboxed Jinja singleton. The parser, validator, and executor live in
their own submodules.

Every public name appears in `__all__`. Names imported here but absent
from `__all__` are a lint failure; names in `__all__` not imported
here are likewise a failure. The discipline keeps the API surface
explicit and `ruff`-checkable (RUF022).
"""

from __future__ import annotations

from workflows.context import NodeContext
from workflows.decorator import NodeDefinition, get_node_definition, node
from workflows.errors import (
    AgentRefUnresolved,
    NodeTimeoutExceeded,
    NodeTypeNotImplemented,
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
    WorkflowRuntimeError,
    WorkflowSecretUnresolved,
    WorkflowTimeoutExceeded,
    WorkflowTimeoutShadowsNode,
    WorkflowUnreachableNode,
    WorkflowValidationError,
    WorkflowValidationWarning,
)
from workflows.ids import InvocationId, ToolCallId, TraceId, WorkflowRunId
from workflows.labels import display_label

# The validator surface (`validate_workflow` / `validate_workflow_or_raise`
# / `ValidationResult`) lives at `workflows.validator` and is
# re-exported here lazily via `__getattr__` (PEP 562) to dodge the
# import cycle: `workflows.validator` pulls in `workflows.parser` which
# pulls in `providers.authz` which pulls in `workflows.errors`. Loading
# the validator at package-init time would attempt that cycle before
# the package itself is fully populated; deferring to first attribute
# access breaks the cycle cleanly.

_LAZY_VALIDATOR_NAMES: frozenset[str] = frozenset(
    {"ValidationResult", "validate_workflow", "validate_workflow_or_raise"}
)


def __getattr__(name: str) -> object:  # PEP 562 module-level hook
    if name in _LAZY_VALIDATOR_NAMES:
        from workflows.validator import (  # noqa: PLC0415
            ValidationResult,
            validate_workflow,
            validate_workflow_or_raise,
        )

        attrs = {
            "ValidationResult": ValidationResult,
            "validate_workflow": validate_workflow,
            "validate_workflow_or_raise": validate_workflow_or_raise,
        }
        # Cache on the module for subsequent fast access.
        globals().update(attrs)
        return attrs[name]
    msg = f"module 'workflows' has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:  # PEP 562 module-level hook
    """Surface lazy validator names to `dir` and introspection.

    Without this, `dir(workflows)` skips the validator names because
    PEP 562's `__getattr__` is only consulted on actual attribute
    access. The parity test in
    `tests/component/test_workflow_scaffolding.py` relies on `dir`
    matching `__all__`.
    """
    return sorted(set(globals().keys()) | _LAZY_VALIDATOR_NAMES)


__all__: list[str] = [
    "AgentRefUnresolved",
    "InvocationId",
    # decorator + context
    "NodeContext",
    "NodeDefinition",
    "NodeTimeoutExceeded",
    "NodeTypeNotImplemented",
    "ToolCallId",
    "TraceId",
    "ValidationResult",
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
    "WorkflowRunId",
    "WorkflowRuntimeError",
    "WorkflowSecretUnresolved",
    "WorkflowTimeoutExceeded",
    "WorkflowTimeoutShadowsNode",
    "WorkflowUnreachableNode",
    "WorkflowValidationError",
    "WorkflowValidationWarning",
    "display_label",
    "get_node_definition",
    "node",
    "validate_workflow",
    "validate_workflow_or_raise",
]

"""Workflows package — public API (master spec §5 + §12).

Phase 3.0 Step 1 ships the FOUNDATION layer only: the
``@node`` decorator, the :class:`NodeContext` dataclass + its
``for_test`` classmethod, the :func:`display_label` helper, the
full exception hierarchy, the typed prefixed IDs, and the
sandboxed Jinja singleton. No parser, no validator, no executor.

Per R4 #5: every public name is listed in :data:`__all__`. Names
imported here but absent from ``__all__`` are a lint failure; names
in ``__all__`` not imported here are likewise a failure. The
discipline keeps the API surface explicit + ruff-checkable
(``RUF022``).
"""

from __future__ import annotations

from workflows.context import NodeContext
from workflows.decorator import NodeDefinition, get_node_definition, node
from workflows.errors import (
    NodeTimeoutExceeded,
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

__all__: list[str] = [
    "InvocationId",
    # decorator + context
    "NodeContext",
    "NodeDefinition",
    "NodeTimeoutExceeded",
    "ToolCallId",
    "TraceId",
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
]

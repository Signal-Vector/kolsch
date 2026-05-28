"""Errors — PlatformError hierarchy and the registered error catalog.

Re-exports every concrete subclass so callers can write::

    from errors import ToolUnauthorized, SecretNotFound, Level

Per the Error Model spec, every error raised across module boundaries
is a PlatformError subclass whose ``code`` is registered in :data:`CATALOG`
(Part 4) and whose ``level`` is intrinsic to the type (E-I3).
"""

from errors.authz import AuthzMalformedRule, AuthzRegexInvalid
from errors.base import (
    PlatformError,
    WrappedError,
    iter_error_classes,
    registered_subclasses,
)
from errors.bus import BusPublishFailed, BusUnavailable
from errors.catalog import CATALOG, get_catalog_level, list_catalog_codes
from errors.engine import (
    AgentUnauthorized,
    AssemblyProcedureFailed,
    CapabilityMismatch,
    ExchangeCancelled,
    ExchangeTimeout,
    ModelMalformedResponse,
    ModelRetryExhausted,
    ModelUpstreamError,
    PlatformInternalError,
)
from errors.file_handling import (
    FileAccessDenied,
    FileAccessPathNormalizationFailed,
    FileAccessPolicyLoadFailed,
)
from errors.kernel import (
    ConfigInvalid,
    ConfigMergeFailed,
    ConfigUnknownVariant,
    ConfigVersionMismatch,
)
from errors.level import Level
from errors.memory import (
    MemoryCompactionFailed,
    MemoryInvalidTag,
    MemoryItemNotFound,
    MemorySessionUnbound,
    MemoryWriteDisabled,
)
from errors.sandbox import (
    SandboxExecutionDenied,
    SandboxPolicyInvalid,
    SandboxResourceExceeded,
)
from errors.secrets import SecretNotFound, SecretReadFailed
from errors.skills import (
    SkillForcedNotEnabled,
    SkillFrontmatterInvalid,
    SkillMalformed,
    SkillNotFound,
)
from errors.store import (
    SessionNotFound,
    StoreCorruptLog,
    StoreLockTimeout,
    StoreWriteFailed,
    ThreadNotFound,
)
from errors.tools import (
    McpProtocolError,
    McpServerUnreachable,
    McpUpstreamError,
    ToolExecutionFailed,
    ToolInvalidArguments,
    ToolNotFound,
    ToolSetupFailed,
    ToolTimeout,
    ToolUnauthorized,
)

__all__ = [
    "CATALOG",
    "AgentUnauthorized",
    "AssemblyProcedureFailed",
    "AuthzMalformedRule",
    "AuthzRegexInvalid",
    "BusPublishFailed",
    "BusUnavailable",
    "CapabilityMismatch",
    "ConfigInvalid",
    "ConfigMergeFailed",
    "ConfigUnknownVariant",
    "ConfigVersionMismatch",
    "ExchangeCancelled",
    "ExchangeTimeout",
    "FileAccessDenied",
    "FileAccessPathNormalizationFailed",
    "FileAccessPolicyLoadFailed",
    "Level",
    "McpProtocolError",
    "McpServerUnreachable",
    "McpUpstreamError",
    "MemoryCompactionFailed",
    "MemoryInvalidTag",
    "MemoryItemNotFound",
    "MemorySessionUnbound",
    "MemoryWriteDisabled",
    "ModelMalformedResponse",
    "ModelRetryExhausted",
    "ModelUpstreamError",
    "PlatformError",
    "PlatformInternalError",
    "SandboxExecutionDenied",
    "SandboxPolicyInvalid",
    "SandboxResourceExceeded",
    "SecretNotFound",
    "SecretReadFailed",
    "SessionNotFound",
    "SkillForcedNotEnabled",
    "SkillFrontmatterInvalid",
    "SkillMalformed",
    "SkillNotFound",
    "StoreCorruptLog",
    "StoreLockTimeout",
    "StoreWriteFailed",
    "ThreadNotFound",
    "ToolExecutionFailed",
    "ToolInvalidArguments",
    "ToolNotFound",
    "ToolSetupFailed",
    "ToolTimeout",
    "ToolUnauthorized",
    "WrappedError",
    "get_catalog_level",
    "iter_error_classes",
    "list_catalog_codes",
    "registered_subclasses",
]

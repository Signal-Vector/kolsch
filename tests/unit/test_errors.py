"""Unit tests for the Error Model implementation.

Maps to the catalog cases TC-ERR-01..05, plus the conformance invariants E-I1..E-I7 from the spec.

The Error Model is fully internal: no I/O, no async paths. Everything
here is a unit test.
"""

from typing import Any

import pytest

import errors
from errors import (
    CATALOG,
    AgentUnauthorized,
    AssemblyProcedureFailed,
    AssetsAmbiguousShorthand,
    AssetsBundleNameMismatch,
    AssetsDuplicateBundleName,
    AssetsDuplicateName,
    AssetsInvalidRef,
    AssetsNotFound,
    AssetsPromptLoadFailed,
    AuthzBareToolRef,
    AuthzDenied,
    AuthzMalformedRule,
    AuthzPatternInvalid,
    AuthzPromptUnattended,
    AuthzProtectedRule,
    AuthzRegexInvalid,
    AuthzRuleInvalid,
    AuthzRuleNotFound,
    AuthzToolRefUnknown,
    BusPublishFailed,
    BusUnavailable,
    CapabilityMismatch,
    ConfigAgentInheritanceCycle,
    ConfigContextFileUnprefixed,
    ConfigDeprecatedRefForm,
    ConfigDeprecatedRegexRule,
    ConfigDeprecatedRuleForm,
    ConfigExtendsTemplateWithoutInheritance,
    ConfigFallbackDuplicate,
    ConfigInvalid,
    ConfigMergeFailed,
    ConfigParentAgentNotFound,
    ConfigRequiredBlockMissing,
    ConfigTemplateRenderFailed,
    ConfigUnknownAlias,
    ConfigUnknownTool,
    ConfigUnknownVariant,
    ConfigVersionMismatch,
    ContextFileNotFound,
    ContextFileTooLarge,
    ExchangeCancelled,
    ExchangeTimeout,
    FileAccessDenied,
    FileAccessInvalidFileRef,
    FileAccessPathNormalizationFailed,
    FileAccessPolicyLoadFailed,
    FileLineTooLong,
    FileNotFound,
    FileTooLarge,
    Level,
    LlmAllModelsFailed,
    LlmClientInitFailed,
    LlmConnectorDuplicate,
    LlmConnectorUnknown,
    LlmDiscoveryFailed,
    LlmProviderNotConfigured,
    LlmSecretMissing,
    LlmValidationFailed,
    McpProtocolError,
    McpServerUnreachable,
    McpUpstreamError,
    MemoryCompactionFailed,
    MemoryInvalidTag,
    MemoryItemNotFound,
    MemorySessionUnbound,
    MemoryWriteDisabled,
    ModelAuthenticationFailed,
    ModelBadRequest,
    ModelContextOverflow,
    ModelMalformedResponse,
    ModelRateLimited,
    ModelRetryExhausted,
    ModelServerError,
    ModelTimeout,
    ModelUpstreamError,
    PlatformError,
    PlatformInternalError,
    SandboxExecutionDenied,
    SandboxPolicyInvalid,
    SandboxResourceExceeded,
    SecretNotFound,
    SecretReadFailed,
    SessionNotFound,
    SetupAborted,
    SetupStarterExists,
    SkillForcedNotEnabled,
    SkillFrontmatterInvalid,
    SkillMalformed,
    SkillNotFound,
    StoreCorruptLog,
    StoreLockTimeout,
    StoreWriteFailed,
    ThreadNotFound,
    ToolExecutionFailed,
    ToolInvalidArguments,
    ToolNotFound,
    ToolPermissionsNoContext,
    ToolSetupFailed,
    ToolTimeout,
    ToolUnauthorized,
    WorkspaceNotInitialized,
    WorkspacePermissionDenied,
    WrappedError,
    iter_error_classes,
    list_catalog_codes,
)

# ---------------------------------------------------------------------------
# Construction map — minimal valid args for every concrete subclass.
# Maintained alongside the class definitions; if a new class lands without
# an entry here, TC-ERR-01 fails loudly with a useful message.
# ---------------------------------------------------------------------------

CONSTRUCTOR_ARGS: dict[type[PlatformError], dict[str, Any]] = {
    # 4.1 Kernel / Resolver
    ConfigInvalid: {"validation_errors": []},
    ConfigUnknownVariant: {"subsystem": "memory", "variant": "imaginary"},
    ConfigVersionMismatch: {"expected": "0.1", "actual": "0.2"},
    ConfigMergeFailed: {"layer_index": 1, "key_path": "/model"},
    ConfigTemplateRenderFailed: {
        "template": "bundles/kolsch/agents/helper.yaml",
        "reason": "'workspce' is undefined",
    },
    ConfigContextFileUnprefixed: {"path": "plain.md"},
    ConfigDeprecatedRefForm: {
        "deprecated_form": "kolsch/tools/bash",
        "canonical_form": "kolsch/bash",
    },
    ConfigAgentInheritanceCycle: {"cycle": ["a.yaml", "b.yaml", "a.yaml"]},
    ConfigParentAgentNotFound: {
        "path": "kolsch:agents/missing.yaml",
        "resolved": "/tmp/missing.yaml",
    },
    ConfigRequiredBlockMissing: {
        "block_name": "role",
        "agent_path": "bundles/x/agents/child.yaml",
    },
    ConfigExtendsTemplateWithoutInheritance: {
        "agent_path": "bundles/x/agents/child.yaml"
    },
    ContextFileNotFound: {"path": "workspace:x.md", "resolved": "/tmp/x.md"},
    ContextFileTooLarge: {
        "path": "workspace:x.md",
        "size_bytes": 100_000,
        "max_bytes": 65_536,
    },
    # 4.2 Store
    SessionNotFound: {"session_id": "ses_x"},
    ThreadNotFound: {"thread_id": "th_x"},
    StoreLockTimeout: {"session_id": "ses_x", "timeout_seconds": 5.0},
    StoreWriteFailed: {"session_id": "ses_x", "reason": "disk full"},
    StoreCorruptLog: {
        "session_id": "ses_x",
        "line_number": 42,
        "parse_error": "expected ','",
    },
    # 4.3 Bus
    BusPublishFailed: {"topic": "tool.*", "transport_error": "ECONNRESET"},
    BusUnavailable: {"reason": "redis down"},
    # 4.4 Agent Engine
    AgentUnauthorized: {
        "agent_id": "ag_x",
        "action": "agent.execute.ag_x",
        "principal": "user.local",
    },
    CapabilityMismatch: {"required": ["streaming"], "available": []},
    ModelRetryExhausted: {"attempts": []},
    ModelMalformedResponse: {"raw_response": "...", "parse_error": "boom"},
    ModelUpstreamError: {
        "provider": "anthropic",
        "http_status": 503,
        "response": "Service Unavailable",
    },
    ExchangeCancelled: {"cancelled_at_event_id": "ev_x"},
    ExchangeTimeout: {"timeout_seconds": 60, "elapsed_seconds": 60.5},
    AssemblyProcedureFailed: {"assembly_version": "0.1", "failed_step": "step3"},
    PlatformInternalError: {"original_type": "ValueError", "args": ["bad"]},
    # 4.5 Tools
    ToolUnauthorized: {
        "tool_name": "bash",
        "action": "tool.bash",
        "principal": "agent.ag_x",
    },
    ToolNotFound: {"tool_name": "frobnicate", "agent_id": "ag_x"},
    ToolPermissionsNoContext: {"tool": "bash"},
    ToolSetupFailed: {"tool_name": "web_search", "reason": "secret missing"},
    ToolInvalidArguments: {"tool_name": "read", "validation_errors": []},
    ToolExecutionFailed: {"tool_name": "read", "error_class": "FileNotFoundError"},
    ToolTimeout: {"tool_name": "long_running", "timeout_seconds": 30},
    McpServerUnreachable: {
        "server_name": "github",
        "transport_error": "ECONNREFUSED",
    },
    McpUpstreamError: {
        "server_name": "github",
        "tool": "list_repos",
        "response": "500",
    },
    ConfigUnknownTool: {
        "ref": "kolsch/tools/ghost",
        "available_refs": ["kolsch/tools/read_file"],
    },
    McpProtocolError: {
        "server_name": "github",
        "expected": "tool_list",
        "actual": "unexpected",
    },
    # 4.6 Memory
    MemoryWriteDisabled: {"agent_id": "ag_x", "reason": "no write_tag"},
    MemoryItemNotFound: {"item_id": "mem_x", "session_id": "ses_x"},
    MemorySessionUnbound: {"session_id": "ses_x"},
    MemoryCompactionFailed: {
        "events_count": 10,
        "target_tokens": 1000,
        "cause": "model error",
    },
    MemoryInvalidTag: {"tag": "research/*", "reason": "wildcard not permitted"},
    # 4.7 Secrets
    SecretNotFound: {"name": "MY_KEY"},
    SecretReadFailed: {"path": "/etc/secrets", "os_error": "EACCES"},
    # 4.8 Sandbox
    SandboxPolicyInvalid: {"policy": {}, "reason": "missing fields"},
    SandboxExecutionDenied: {"reason": "policy denies"},
    SandboxResourceExceeded: {"resource": "memory", "limit": 100, "observed": 200},
    # 4.9 Authz
    AuthzMalformedRule: {"rule_index": 0, "error": "missing 'who'"},
    AuthzRegexInvalid: {
        "rule_index": 1,
        "field": "what",
        "regex": "[",
        "error": "unbalanced",
    },
    AuthzBareToolRef: {
        "rule_index": 2,
        "bare_name": "bash",
        "candidates": ["kolsch/bash"],
    },
    AuthzPatternInvalid: {
        "rule_index": 3,
        "pattern": "^find .*$",
        "forbidden_chars": ["^", "$"],
    },
    AuthzToolRefUnknown: {
        "rule_index": 4,
        "tool_ref": "missing/tool",
    },
    ConfigDeprecatedRegexRule: {
        "rule_index": 5,
        "what": r"^find .*$",
        "reason": "regex doesn't match any migration heuristic",
    },
    ConfigDeprecatedRuleForm: {
        "rule_index": 6,
        "what": "kolsch/bash",
        "reason": (
            "2-seg form `<bundle>/<name>` is deprecated; "
            "use the 3-seg catalog ref `<bundle>/tools/<name>` instead"
        ),
    },
    # 4.10 Skills
    SkillNotFound: {"ref": "core/skills/missing"},
    SkillMalformed: {"path": "/p/SKILL.md", "parse_error": "no frontmatter"},
    SkillFrontmatterInvalid: {"path": "/p/SKILL.md", "validation_errors": []},
    SkillForcedNotEnabled: {"ref": "core/skills/x", "agent_id": "ag_x"},
    # 4.11 File Handling
    FileAccessDenied: {"path": "/etc/passwd", "reason": "default deny"},
    FileAccessInvalidFileRef: {"value": "", "reason": "empty"},
    FileAccessPathNormalizationFailed: {"path": "/bad", "os_error": "ENOENT"},
    FileAccessPolicyLoadFailed: {
        "config_section": "file_access",
        "error": "malformed",
    },
    # Asset Management (catalog extension)
    AssetsDuplicateBundleName: {"name": "acme", "paths": ["bundles/a", "bundles/b"]},
    AssetsDuplicateName: {
        "bundle": "acme",
        "name": "butler",
        "refs": ["acme/agents/butler", "acme/tools/butler"],
    },
    AssetsBundleNameMismatch: {"directory": "foo", "manifest_name": "bar"},
    AssetsNotFound: {"ref": "acme/tools/missing"},
    AssetsInvalidRef: {"ref": "no slashes"},
    AssetsAmbiguousShorthand: {
        "shorthand": "web-search",
        "candidates": ["acme/tools/web-search", "scholarly/tools/web-search"],
    },
    AssetsPromptLoadFailed: {"ref": "acme/prompts/role", "reason": "missing"},
    # 4.12 Generic
    WrappedError: {
        "message": "ValueError: bad",
        "where": "test.scaffold",
        "detail": {"original_type": "builtins.ValueError", "args": ["bad"]},
    },
    # 4.13 LLM Configuration
    LlmProviderNotConfigured: {
        "provider": "anthropic",
        "configured_providers": ["opencode"],
    },
    LlmSecretMissing: {"provider": "opencode", "secret_name": "OPENCODE_API_KEY"},
    LlmClientInitFailed: {
        "provider": "opencode",
        "model": "gpt-oss-120b",
        "reason": "boom",
    },
    ConfigUnknownAlias: {"alias": "mystery", "available_aliases": ["smart"]},
    LlmConnectorUnknown: {
        "type_name": "exotic",
        "registered_types": ["opencode", "openrouter"],
    },
    LlmDiscoveryFailed: {"provider": "opencode", "reason": "connection refused"},
    LlmValidationFailed: {"provider": "opencode", "reason": "401"},
    LlmConnectorDuplicate: {"type_name": "opencode"},
    # 4.14 Workspace
    WorkspaceNotInitialized: {"path": "/var/ws/k", "env_var_set": False},
    WorkspacePermissionDenied: {"path": "/var/ws/k", "operation": "mkdir"},
    SetupAborted: {"step": "C", "reason": "ctrl-c"},
    SetupStarterExists: {"path": "/var/ws/k/bundles/starter/agents/helper.yaml"},
    # 4.15 Model Fallback
    ModelRateLimited: {"provider": "openrouter", "model": "anthropic/x"},
    ModelServerError: {
        "provider": "openrouter",
        "model": "anthropic/x",
        "status_code": 503,
    },
    ModelTimeout: {
        "provider": "openrouter",
        "model": "anthropic/x",
        "timeout_seconds": 30.0,
    },
    ModelContextOverflow: {
        "provider": "openrouter",
        "model": "anthropic/x",
        "requested_tokens": 200_000,
    },
    ModelAuthenticationFailed: {
        "provider": "openrouter",
        "model": "anthropic/x",
        "status_code": 401,
    },
    ModelBadRequest: {
        "provider": "openrouter",
        "model": "anthropic/x",
        "status_code": 400,
        "reason": "malformed",
    },
    LlmAllModelsFailed: {
        "primary": {"provider": "anthropic", "model": "opus"},
        "attempts": [
            {
                "model": {"provider": "anthropic", "model": "opus"},
                "error_code": "model.rate_limited",
                "detail": {},
            },
        ],
        "reason": "exhausted",
    },
    ConfigFallbackDuplicate: {
        "agent": "ag_x",
        "model": {"provider": "openrouter", "model": "anthropic/x"},
        "positions": [1, 3],
    },
    # 4.16 Tools & Permissions
    AuthzDenied: {
        "tool": "bash",
        "target": "rm -rf /",
        "rule_id": "rl_abcdef12",
    },
    AuthzPromptUnattended: {
        "tool": "bash",
        "target": "rg foo",
        "rule_id": "rl_bash_any",
        "caller": "kolsch run",
    },
    AuthzRuleInvalid: {
        "position": -1,
        "reason": "invalid regex in `what`",
    },
    AuthzRuleNotFound: {
        "rule_id": "rl_nope",
        "available_ids": ["rl_default_deny"],
    },
    AuthzProtectedRule: {
        "rule_id": "rl_default_deny",
        "reason": "default-deny tail is removal-protected",
    },
    FileTooLarge: {
        "path": "/workspace/big.bin",
        "size_bytes": 2 * 1024 * 1024,
        "max_bytes": 1 * 1024 * 1024,
    },
    FileNotFound: {"path": "/workspace/missing.txt"},
    FileLineTooLong: {
        "path": "/workspace/binary.txt",
        "line_number": 0,
        "line_byte_length": 2 * 1024 * 1024,
        "max_bytes": 1 * 1024 * 1024,
    },
}


def _construct(cls: type[PlatformError]) -> PlatformError:
    """Build a minimally-valid instance of `cls` for envelope assertions."""
    if cls not in CONSTRUCTOR_ARGS:
        pytest.fail(
            f"add CONSTRUCTOR_ARGS entry for {cls.__name__} (code={cls.code!r})"
        )
    return cls(**CONSTRUCTOR_ARGS[cls])


# ---------------------------------------------------------------------------
# TC-ERR-01 — every subclass serializes to the canonical envelope
# ---------------------------------------------------------------------------


def test_every_subclass_serializes_to_canonical_envelope() -> None:
    classes = list(iter_error_classes())
    assert classes, "iter_error_classes must yield at least one subclass"
    for cls in classes:
        instance = _construct(cls)
        envelope = instance.to_dict()
        assert set(envelope.keys()) == {
            "type",
            "message",
            "where",
            "detail",
            "cause",
        }, f"{cls.__name__} envelope has extra/missing keys: {set(envelope)}"
        assert envelope["type"] == cls.code
        assert envelope["where"] == instance.where
        assert isinstance(envelope["message"], str) and envelope["message"]
        assert isinstance(envelope["detail"], dict)


# ---------------------------------------------------------------------------
# TC-ERR-02 — cause chain serializes recursively (3 deep)
# ---------------------------------------------------------------------------


def test_cause_chain_serializes_recursively_three_deep() -> None:
    try:
        try:
            try:
                raise SecretNotFound(name="C_KEY")
            except SecretNotFound as c:
                raise ToolSetupFailed(
                    tool_name="web_search", reason="secret missing"
                ) from c
        except ToolSetupFailed as b:
            raise AgentUnauthorized(
                agent_id="ag_x",
                action="agent.execute.ag_x",
                principal="user.local",
            ) from b
    except AgentUnauthorized as a:
        envelope = a.to_dict()

    assert envelope["type"] == "agent.unauthorized"
    assert envelope["cause"] is not None
    assert envelope["cause"]["type"] == "tool.setup_failed"
    assert envelope["cause"]["cause"] is not None
    assert envelope["cause"]["cause"]["type"] == "secret.not_found"
    assert envelope["cause"]["cause"]["cause"] is None


# ---------------------------------------------------------------------------
# TC-ERR-03 — non-platform exception wrapped
# ---------------------------------------------------------------------------


def test_wrapped_error_from_stdlib_exception() -> None:
    exc = FileNotFoundError("data/x.md")

    wrapped = WrappedError.from_exception(exc, where="tools.builtin.read")
    envelope = wrapped.to_dict()

    assert envelope["type"] == "platform.wrapped"
    assert envelope["where"] == "tools.builtin.read"
    assert envelope["detail"]["original_type"] == "builtins.FileNotFoundError"
    assert envelope["detail"]["args"] == ["data/x.md"]
    assert envelope["cause"] is None
    # Message carries the original type + str(exc) for at-a-glance reading.
    assert "FileNotFoundError" in envelope["message"]
    assert "data/x.md" in envelope["message"]


def test_non_platform_cause_is_wrapped_in_envelope() -> None:
    """A non-PlatformError sitting in __cause__ must serialize as a
    platform.wrapped envelope (E-I5), not leak as the raw exception."""
    try:
        try:
            raise ValueError("explode")
        except ValueError as v:
            raise SecretNotFound(name="MY_KEY") from v
    except SecretNotFound as sn:
        envelope = sn.to_dict()

    assert envelope["cause"] is not None
    assert envelope["cause"]["type"] == "platform.wrapped"
    assert envelope["cause"]["detail"]["original_type"] == "builtins.ValueError"


# ---------------------------------------------------------------------------
# TC-ERR-04 — every emitted type is in the catalog
# ---------------------------------------------------------------------------


def test_every_class_code_is_registered_in_catalog() -> None:
    catalog_codes = list_catalog_codes()
    for cls in iter_error_classes():
        assert cls.code in catalog_codes, (
            f"{cls.__name__} declares code {cls.code!r} which is not in the "
            f"error catalog (Part 4). Add it to CATALOG or remove the class."
        )


def test_catalog_has_no_orphan_codes() -> None:
    """Inverse of the class->catalog check: every catalogued code must
    have a concrete subclass.

    The class->catalog direction is already enforced at import time by
    ``PlatformError.__init_subclass__``. The reverse direction catches
    a different drift: a CATALOG entry added without a corresponding
    implementation — the spec would claim a code exists but nothing in
    the platform can emit it, which is a normative-registry violation
    in spirit (E-I2).

    Restrict the comparison to subclasses defined inside the ``errors.*``
    namespace so ad-hoc test-local subclasses (which auto-register via
    ``__init_subclass__``) don't satisfy the reverse direction by
    accident.
    """
    catalog_codes = list_catalog_codes()
    class_codes = {
        cls.code for cls in iter_error_classes() if cls.__module__.startswith("errors.")
    }
    orphans = catalog_codes - class_codes
    assert not orphans, (
        f"Catalog codes without a concrete subclass: {sorted(orphans)}. "
        f"Either implement the class or remove the entry from CATALOG."
    )


def test_catalog_codes_have_dotted_format() -> None:
    """E-I2 plus a soft check: codes follow `<scope>.<specific>` convention."""
    for code in list_catalog_codes():
        assert "." in code, f"catalog code {code!r} must be dotted"
        scope, _, specific = code.partition(".")
        assert scope and specific, f"catalog code {code!r} has empty parts"


# ---------------------------------------------------------------------------
# TC-ERR-05 — level is intrinsic to type
# ---------------------------------------------------------------------------


def test_class_level_matches_catalog() -> None:
    for cls in iter_error_classes():
        expected = CATALOG[cls.code]
        assert cls.level == expected, (
            f"{cls.__name__}.level={cls.level!r} disagrees with catalog "
            f"{expected!r} for code {cls.code!r}"
        )


def test_no_subclass_overrides_level_per_instance() -> None:
    """E-I3 — level is on the class, not the instance. Constructing the
    same class twice with different args must yield the same level."""
    a = SecretNotFound(name="A")
    b = SecretNotFound(name="B")
    assert a.level is b.level is Level.FATAL


# ---------------------------------------------------------------------------
# Spec invariants (handbook §10.10 references E-I1..E-I7)
# ---------------------------------------------------------------------------


def test_where_must_be_non_empty() -> None:
    """E-I6 — empty `where` is a contract violation."""
    with pytest.raises(ValueError):
        ToolUnauthorized(
            tool_name="bash",
            action="tool.bash",
            principal="agent.ag_x",
            where="",
        )


def test_detail_keys_match_catalog_shape_for_known_types() -> None:
    """E-I7 — spot-check the documented detail shapes are populated."""
    err = ToolUnauthorized(
        tool_name="bash",
        action="tool.bash",
        principal="agent.ag_x",
    )
    assert set(err.detail) >= {"tool_name", "action", "principal", "matched_rule"}

    err2 = SecretNotFound(name="MY_KEY")
    assert err2.detail == {"name": "MY_KEY"}


def test_level_enum_is_str_subclass() -> None:
    """The Level enum must serialize naturally as strings."""
    assert Level.DIAGNOSTIC.value == "diagnostic"
    assert Level.RECOVERABLE.value == "recoverable"
    assert Level.FATAL.value == "fatal"
    assert isinstance(Level.FATAL, str)


def test_wrapped_error_recorded_for_basemodel_subclass() -> None:
    """from_exception must accept any Exception subclass, including a
    custom domain exception, and produce a canonical envelope."""

    class MyDomainError(Exception):
        pass

    e = MyDomainError("nope", 42)
    wrapped = WrappedError.from_exception(e, where="some.where")
    envelope = wrapped.to_dict()

    assert envelope["type"] == "platform.wrapped"
    assert envelope["detail"]["original_type"].endswith(".MyDomainError")
    assert envelope["detail"]["args"] == ["nope", 42]


def test_construction_with_empty_detail_yields_empty_dict() -> None:
    """`detail=None` at construction must materialize as `{}` in to_dict."""
    # Use WrappedError's base-class constructor directly: it inherits
    # PlatformError.__init__ which allows the detail kwarg to default
    # to None. Avoid an ad-hoc test-only subclass so `__init_subclass__`
    # doesn't pollute the global error registry.
    err = WrappedError(message="x", where="t.x")
    assert err.to_dict()["detail"] == {}


def test_errors_module_reexports_subclasses() -> None:
    """The top-level `errors` package re-exports every concrete subclass
    so callers can write `from errors import ToolUnauthorized`.

    Only classes whose module lives under the `errors.` namespace are
    checked — third-party or test-local subclasses defined elsewhere are
    not the package's responsibility to re-export.
    """
    for cls in iter_error_classes():
        if not cls.__module__.startswith("errors."):
            continue
        assert hasattr(errors, cls.__name__), (
            f"errors package does not re-export {cls.__name__}"
        )

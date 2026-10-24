"""The four swappable provider subsystems: memory, secrets, sandbox, authz.

Secrets, sandbox, and authz are **platform-level** providers
configured in `platform.yaml` and applied uniformly to every agent;
see the [secrets-sandbox-authz
contract](../explanation/secrets-sandbox-authz-contract.md).
Memory is agent-level and follows the [memory provider
contract](../explanation/memory-provider-contract.md).

Each provider exposes a Protocol (the contract), one v1 variant, and
a small registry seam so future variants slot in without core changes.
"""

from providers.authz import (
    DEFAULT_DENY_RULE_ID,
    KOLSCH_TEST_BUILD_ENV_VAR,
    AllowAllTestAuthorizationProvider,
    AuthorizationProvider,
    AuthorizationResult,
    AuthzEffect,
    DefaultDenyTailStatus,
    PermissionLayer,
    Rule,
    RuleBasedAuthorizationConfig,
    RuleBasedAuthorizationProvider,
    RulePersistRequest,
    derive_rule_id,
    describe_default_deny_tail_problem,
    inspect_default_deny_tail,
    make_authorization_provider,
    platform_rules_from_config,
    register_authorization_variant,
)
from providers.compaction import (
    ContextCompactionProvider,
    ReferenceSummaryCompactionProvider,
    make_compaction_provider,
    register_compaction_variant,
    validate_reference_summary_config,
)
from providers.memory import (
    CompactionResult,
    FilterExpression,
    MemoryContext,
    MemoryItem,
    RecentBufferMemoryContext,
    RecentBufferMemoryProvider,
    RetrievalQuery,
    RetrievalResult,
    RetrievedItem,
    make_memory_provider,
    register_memory_variant,
    validate_recent_buffer_config,
)
from providers.sandbox import (
    NativeSandboxProvider,
    SandboxProvider,
    make_sandbox_provider,
    register_sandbox_variant,
)
from providers.secrets import (
    DotenvSecretsProvider,
    SecretsProvider,
    make_secrets_provider,
    register_secrets_variant,
)

__all__ = [
    "DEFAULT_DENY_RULE_ID",
    "KOLSCH_TEST_BUILD_ENV_VAR",
    "AllowAllTestAuthorizationProvider",
    "AuthorizationProvider",
    "AuthorizationResult",
    "AuthzEffect",
    "CompactionResult",
    "ContextCompactionProvider",
    "DefaultDenyTailStatus",
    "DotenvSecretsProvider",
    "FilterExpression",
    "MemoryContext",
    "MemoryItem",
    "NativeSandboxProvider",
    "PermissionLayer",
    "RecentBufferMemoryContext",
    "RecentBufferMemoryProvider",
    "ReferenceSummaryCompactionProvider",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievedItem",
    "Rule",
    "RuleBasedAuthorizationConfig",
    "RuleBasedAuthorizationProvider",
    "RulePersistRequest",
    "SandboxProvider",
    "SecretsProvider",
    "derive_rule_id",
    "describe_default_deny_tail_problem",
    "inspect_default_deny_tail",
    "make_authorization_provider",
    "make_compaction_provider",
    "make_memory_provider",
    "make_sandbox_provider",
    "make_secrets_provider",
    "platform_rules_from_config",
    "register_authorization_variant",
    "register_compaction_variant",
    "register_memory_variant",
    "register_sandbox_variant",
    "register_secrets_variant",
    "validate_recent_buffer_config",
    "validate_reference_summary_config",
]

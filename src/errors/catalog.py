"""The Error Type Catalog — every catalogued code mapped to its level.

A normative registry: subsystems use these codes and never invent
ad-hoc strings. New error types are added by amending this map and the
corresponding class file. See the
[error model](../explanation/error-model.md) for the contract
this registry implements.

Iteration helpers live in `errors.base` so the catalog has no
import-time dependency on the class hierarchy.
"""

from errors.level import Level

CATALOG: dict[str, Level] = {
    # ----- Kernel / Resolver -------------------------------------------
    "config.invalid": Level.FATAL,
    "config.unknown_variant": Level.FATAL,
    "config.version_mismatch": Level.FATAL,
    "config.merge_failed": Level.FATAL,
    # Jinja render failure surfaced by templating.render.render_prompt.
    "config.template_render_failed": Level.FATAL,
    # Agent declares a context_file or extends: path without the
    # kolsch: / workspace: prefix.
    "config.context_file_unprefixed": Level.FATAL,
    # Config envelope ref uses the legacy 3-segment form or an
    # explicit workspace: prefix; Recoverable warning, not a load
    # failure.
    "config.deprecated_ref_form": Level.RECOVERABLE,
    # Context file resolution errors.
    "context.file_not_found": Level.FATAL,
    "context.file_too_large": Level.FATAL,
    "context.overflow_uncompactable": Level.FATAL,
    # Agent inheritance errors.
    "config.agent_inheritance_cycle": Level.FATAL,
    "config.parent_agent_not_found": Level.FATAL,
    "config.required_block_missing": Level.FATAL,
    "config.extends_template_without_inheritance": Level.FATAL,
    # Agent prompt template references `{{ inputs.* }}` — `inputs` is
    # not in the agent-template scope; declare io.inputs + describe
    # the field in prose instead.
    "config.inputs_in_agent_template": Level.FATAL,
    # Tool's permissions API invoked outside the engine's dispatcher
    # binding.
    "tool.permissions_no_context": Level.FATAL,
    # ----- Session/Run Store -------------------------------------------
    "session.not_found": Level.FATAL,
    "thread.not_found": Level.FATAL,
    "store.lock_timeout": Level.FATAL,
    "store.write_failed": Level.FATAL,
    "store.corrupt_log": Level.FATAL,
    # ----- Asset Management --------------------------------------------
    # Asset Management's contract surfaces these codes in its Module
    # API Reference; registered here so PlatformError subclasses in
    # `errors.assets` can register cleanly.
    "assets.duplicate_bundle_name": Level.FATAL,
    "assets.duplicate_name": Level.FATAL,
    "assets.bundle_name_mismatch": Level.FATAL,
    "assets.not_found": Level.FATAL,
    "assets.invalid_ref": Level.FATAL,
    "assets.ambiguous_shorthand": Level.FATAL,
    "assets.prompt_load_failed": Level.FATAL,
    # ----- Event Bus ---------------------------------------------------
    # Bus errors are Diagnostic because the bus is best-effort — losing
    # a bus message must not fail an exchange.
    "bus.publish_failed": Level.DIAGNOSTIC,
    "bus.unavailable": Level.DIAGNOSTIC,
    # ----- Agent Engine ------------------------------------------------
    "agent.unauthorized": Level.FATAL,
    "agent.capability_mismatch": Level.FATAL,
    "model.retry_exhausted": Level.FATAL,
    "model.malformed_response": Level.RECOVERABLE,
    "model.upstream_error": Level.RECOVERABLE,
    "exchange.cancelled": Level.FATAL,
    "exchange.timeout": Level.FATAL,
    "assembly.procedure_failed": Level.FATAL,
    # One code, one class: PlatformInternalError lives in engine.py
    # since the engine boundary is where it is most often raised.
    "platform.internal_error": Level.FATAL,
    # ----- Tools -------------------------------------------------------
    "tool.unauthorized": Level.DIAGNOSTIC,
    "tool.not_found": Level.FATAL,
    "tool.setup_failed": Level.FATAL,
    "tool.invalid_arguments": Level.DIAGNOSTIC,
    "tool.execution_failed": Level.DIAGNOSTIC,
    "tool.timeout": Level.DIAGNOSTIC,
    "mcp.server_unreachable": Level.DIAGNOSTIC,
    "mcp.upstream_error": Level.DIAGNOSTIC,
    "mcp.protocol_error": Level.DIAGNOSTIC,
    # ----- MCP tool bridge (spec-120) ----------------------------------
    # The ToolBridgeServer + `kolsch mcp bridge` trust boundary.
    # Auth/transport refusals are Recoverable (only the external
    # client's session is refused; the exchange continues); security
    # -invariant violations and wire-name catalog corruption are Fatal.
    "bridge.auth_failed": Level.RECOVERABLE,
    "bridge.not_bound": Level.RECOVERABLE,
    "bridge.socket_unavailable": Level.RECOVERABLE,
    "bridge.socket_insecure": Level.FATAL,
    "bridge.frame_too_large": Level.RECOVERABLE,
    "bridge.frame_malformed": Level.RECOVERABLE,
    "bridge.malformed_request": Level.RECOVERABLE,
    "bridge.request_refused": Level.RECOVERABLE,
    "bridge.name_collision": Level.FATAL,
    # Raised by engine pre-flight when a declared tool ref doesn't
    # resolve in the asset catalog. Named `config.*` rather than
    # `tool.*` because it's a config validity failure — the agent's
    # tools.config.declared lists a ref that doesn't exist.
    "config.unknown_tool": Level.FATAL,
    # ----- Memory ------------------------------------------------------
    "memory.write_disabled": Level.DIAGNOSTIC,
    "memory.item_not_found": Level.DIAGNOSTIC,
    "memory.session_unbound": Level.FATAL,
    "memory.compaction_failed": Level.RECOVERABLE,
    "memory.invalid_tag": Level.FATAL,
    # ----- Secrets -----------------------------------------------------
    "secret.not_found": Level.FATAL,
    "secret.read_failed": Level.FATAL,
    # ----- Sandbox -----------------------------------------------------
    "sandbox.policy_invalid": Level.FATAL,
    "sandbox.execution_denied": Level.DIAGNOSTIC,
    "sandbox.resource_exceeded": Level.DIAGNOSTIC,
    # ----- Authorization -----------------------------------------------
    "authz.malformed_rule": Level.FATAL,
    "authz.regex_invalid": Level.FATAL,
    # ----- Skills ------------------------------------------------------
    "skill.not_found": Level.FATAL,
    "skill.malformed": Level.FATAL,
    "skill.frontmatter_invalid": Level.FATAL,
    "skill.forced_not_enabled": Level.FATAL,
    # ----- File Handling -----------------------------------------------
    "file_access.denied": Level.DIAGNOSTIC,
    "file_access.path_normalization_failed": Level.DIAGNOSTIC,
    "file_access.policy_load_failed": Level.FATAL,
    # Needed at the file_ref input boundary so the validator raises a
    # PlatformError rather than a bare ValueError.
    "file_access.invalid_file_ref": Level.DIAGNOSTIC,
    # ----- Generic platform --------------------------------------------
    # platform.wrapped's default instance is Diagnostic; when a wrapped
    # exception must surface at Fatal severity, raise
    # `PlatformInternalError` instead.
    "platform.wrapped": Level.DIAGNOSTIC,
    # ----- LLM Configuration -------------------------------------------
    "llm.provider_not_configured": Level.FATAL,
    "llm.secret_missing": Level.FATAL,
    "llm.client_init_failed": Level.FATAL,
    "config.unknown_alias": Level.FATAL,
    "llm.connector_unknown": Level.FATAL,
    "llm.discovery_failed": Level.RECOVERABLE,
    "llm.validation_failed": Level.FATAL,
    # Surfaced by registry.register() when a connector type registers
    # twice; the registry would otherwise silently last-write-wins.
    "llm.connector_duplicate": Level.FATAL,
    # ----- Workspace ---------------------------------------------------
    "workspace.not_initialized": Level.FATAL,
    "workspace.permission_denied": Level.FATAL,
    "setup.aborted": Level.RECOVERABLE,
    "setup.starter_exists": Level.RECOVERABLE,
    # ----- Model Fallback ----------------------------------------------
    # Four Recoverable codes — drive the fallback loop.
    "model.rate_limited": Level.RECOVERABLE,
    "model.server_error": Level.RECOVERABLE,
    "model.timeout": Level.RECOVERABLE,
    "model.context_overflow": Level.RECOVERABLE,
    # Two Fatal model.* codes — abort without consulting the chain.
    "model.authentication_failed": Level.FATAL,
    "model.bad_request": Level.FATAL,
    # Engine fallback-loop terminus.
    "llm.all_models_failed": Level.FATAL,
    # Layer 1 validator warning surface — Diagnostic since the Error
    # Model doesn't yet have a Warning tier.
    "config.fallback_duplicate": Level.DIAGNOSTIC,
    # ----- Tools & Permissions -----------------------------------------
    # Five authz.* codes for the prompt-effect dispatch and the live-
    # edit CLI; two file.* codes for the builtin file tools
    # (file_access.denied above covers the access-denied surface).
    "authz.denied": Level.RECOVERABLE,
    "authz.prompt_unattended": Level.RECOVERABLE,
    "authz.rule_invalid": Level.FATAL,
    "authz.rule_not_found": Level.FATAL,
    "authz.protected_rule": Level.FATAL,
    # Permission-system schema migration: human-readable rule form.
    "authz.bare_tool_ref": Level.FATAL,
    "authz.pattern_invalid": Level.FATAL,
    "authz.tool_ref_unknown": Level.FATAL,
    "config.deprecated_regex_rule": Level.RECOVERABLE,
    # 2-seg rule form deprecation: the bundle-scoped action namespace
    # migration uses the 3-seg catalog ref form.
    "config.deprecated_rule_form": Level.RECOVERABLE,
    "file.too_large": Level.RECOVERABLE,
    "file.not_found": Level.RECOVERABLE,
    # read_file raises this when a single line exceeds max_bytes
    # (typically a binary file masquerading as text). Distinct from
    # file.too_large, which is no longer raised by read_file under the
    # line-based shape.
    "file.line_too_long": Level.RECOVERABLE,
}


def list_catalog_codes() -> set[str]:
    """Return the set of every registered catalog code."""
    return set(CATALOG.keys())


def get_catalog_level(code: str) -> Level | None:
    """Return the registered level for a code, or `None` if unknown."""
    return CATALOG.get(code)

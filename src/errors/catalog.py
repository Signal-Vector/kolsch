"""The Error Type Catalog — spec §4.1-4.12.

A normative registry mapping every catalogued error code to its
assigned level. New error types are added by amending this map and
the corresponding class file. Implementations MUST use these codes
and MUST NOT invent ad-hoc strings (E-I2).

Iteration helpers live in :mod:`errors.base` so the catalog has no
import-time dependency on the class hierarchy.
"""

from errors.level import Level

CATALOG: dict[str, Level] = {
    # ----- §4.1 Kernel / Resolver --------------------------------------
    "config.invalid": Level.FATAL,
    "config.unknown_variant": Level.FATAL,
    "config.version_mismatch": Level.FATAL,
    "config.merge_failed": Level.FATAL,
    # ----- §4.2 Session/Run Store --------------------------------------
    "session.not_found": Level.FATAL,
    "thread.not_found": Level.FATAL,
    "store.lock_timeout": Level.FATAL,
    "store.write_failed": Level.FATAL,
    "store.corrupt_log": Level.FATAL,
    # ----- Asset Management (extension to spec §4.1) -------------------
    # Asset Management's contract surfaces these codes in its Module
    # API Reference §2 method raises but the error catalog (§4) does
    # not yet enumerate them. Added here so PlatformError subclasses
    # in `errors.assets` can register cleanly; flag for spec amendment.
    "assets.duplicate_bundle_name": Level.FATAL,
    "assets.bundle_name_mismatch": Level.FATAL,
    "assets.reserved_bundle_name": Level.FATAL,
    "assets.not_found": Level.FATAL,
    "assets.invalid_ref": Level.FATAL,
    "assets.ambiguous_shorthand": Level.FATAL,
    "assets.prompt_load_failed": Level.FATAL,
    # ----- §4.3 Event Bus ----------------------------------------------
    # Bus errors are Diagnostic because the bus is best-effort — losing
    # a bus message must not fail an exchange.
    "bus.publish_failed": Level.DIAGNOSTIC,
    "bus.unavailable": Level.DIAGNOSTIC,
    # ----- §4.4 Agent Engine -------------------------------------------
    "agent.unauthorized": Level.FATAL,
    "agent.capability_mismatch": Level.FATAL,
    "model.retry_exhausted": Level.FATAL,
    "model.malformed_response": Level.RECOVERABLE,
    "model.upstream_error": Level.RECOVERABLE,
    "exchange.cancelled": Level.FATAL,
    "exchange.timeout": Level.FATAL,
    "assembly.procedure_failed": Level.FATAL,
    # platform.internal_error appears in both §4.4 and §4.12 — one code,
    # one class (PlatformInternalError, defined in engine.py since the
    # engine boundary is where it is most often raised).
    "platform.internal_error": Level.FATAL,
    # ----- §4.5 Tools --------------------------------------------------
    "tool.unauthorized": Level.DIAGNOSTIC,
    "tool.not_found": Level.FATAL,
    "tool.setup_failed": Level.FATAL,
    "tool.invalid_arguments": Level.DIAGNOSTIC,
    "tool.execution_failed": Level.DIAGNOSTIC,
    "tool.timeout": Level.DIAGNOSTIC,
    "mcp.server_unreachable": Level.DIAGNOSTIC,
    "mcp.upstream_error": Level.DIAGNOSTIC,
    "mcp.protocol_error": Level.DIAGNOSTIC,
    # Bug-engine-tools-wiring-gap.md: raised by engine pre-flight
    # when a declared tool ref doesn't resolve in the asset catalog.
    # Naming `config.*` rather than `tool.*` because it's a config
    # validity failure — the agent's tools.config.declared lists a
    # ref that doesn't exist.
    "config.unknown_tool": Level.FATAL,
    # ----- §4.6 Memory -------------------------------------------------
    "memory.write_disabled": Level.DIAGNOSTIC,
    "memory.item_not_found": Level.DIAGNOSTIC,
    "memory.session_unbound": Level.FATAL,
    "memory.compaction_failed": Level.RECOVERABLE,
    "memory.invalid_tag": Level.FATAL,
    # ----- §4.7 Secrets ------------------------------------------------
    "secret.not_found": Level.FATAL,
    "secret.read_failed": Level.FATAL,
    # ----- §4.8 Sandbox ------------------------------------------------
    "sandbox.policy_invalid": Level.FATAL,
    "sandbox.execution_denied": Level.DIAGNOSTIC,
    "sandbox.resource_exceeded": Level.DIAGNOSTIC,
    # ----- §4.9 Authorization ------------------------------------------
    "authz.malformed_rule": Level.FATAL,
    "authz.regex_invalid": Level.FATAL,
    # ----- §4.10 Skills ------------------------------------------------
    "skill.not_found": Level.FATAL,
    "skill.malformed": Level.FATAL,
    "skill.frontmatter_invalid": Level.FATAL,
    "skill.forced_not_enabled": Level.FATAL,
    # ----- §4.11 File Handling -----------------------------------------
    "file_access.denied": Level.DIAGNOSTIC,
    "file_access.path_normalization_failed": Level.DIAGNOSTIC,
    "file_access.policy_load_failed": Level.FATAL,
    # `invalid_file_ref` is not in the spec §4.11 catalog but is
    # needed at the file_ref input boundary (Fh-I8) so the validator
    # raises a PlatformError rather than a bare ValueError (handbook
    # hard rule). Flag for spec amendment alongside the asset error
    # codes also added downstream.
    "file_access.invalid_file_ref": Level.DIAGNOSTIC,
    # ----- §4.12 Generic platform --------------------------------------
    # platform.wrapped's level is documented as "(varies)"; the
    # default-instance level is DIAGNOSTIC, matching the spec §3.4
    # reference implementation. When a wrapped exception must surface
    # at Fatal severity, raise `PlatformInternalError` instead.
    "platform.wrapped": Level.DIAGNOSTIC,
    # ----- §4.13 LLM Configuration (Phase 1.1) -------------------------
    "llm.provider_not_configured": Level.FATAL,
    "llm.secret_missing": Level.FATAL,
    "llm.client_init_failed": Level.FATAL,
    "config.unknown_alias": Level.FATAL,
    "llm.connector_unknown": Level.FATAL,
    "llm.discovery_failed": Level.RECOVERABLE,
    "llm.validation_failed": Level.FATAL,
    # `llm.connector_duplicate` is NOT in the §4.13 spec table but is
    # surfaced by registry.register() when a connector type registers
    # twice (TC-LLM-09). Added so the registry can raise a typed
    # PlatformError; flag for spec amendment alongside the other
    # post-spec catalog additions above.
    "llm.connector_duplicate": Level.FATAL,
    # ----- §4.14 Workspace (Phase 1.2, UX-R6) --------------------------
    "workspace.not_initialized": Level.FATAL,
    "workspace.permission_denied": Level.FATAL,
    "setup.aborted": Level.RECOVERABLE,
    "setup.starter_exists": Level.RECOVERABLE,
    # ----- §4.15 Model Fallback (Phase 1.3, FB-R5) ---------------------
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
    # Layer 1 validator warning surface — Diagnostic in the v1.3
    # taxonomy since the Error Model doesn't yet have a Warning tier
    # (flagged in Phase 1.3 Step 19 review for follow-up).
    "config.fallback_duplicate": Level.DIAGNOSTIC,
    # ----- §4.16 Tools & Permissions (Phase 2.0, TOOL-R5) --------------
    # Five authz.* codes for the prompt-effect dispatch and the live-
    # edit CLI; two new file.* codes for the Phase 2.0 builtin tools
    # (file_access.denied from §4.11 covers the access-denied surface
    # for both Phase 1 and Phase 2.0 builtins).
    "authz.denied": Level.RECOVERABLE,
    "authz.prompt_unattended": Level.RECOVERABLE,
    "authz.rule_invalid": Level.FATAL,
    "authz.rule_not_found": Level.FATAL,
    "authz.protected_rule": Level.FATAL,
    "file.too_large": Level.RECOVERABLE,
    "file.not_found": Level.RECOVERABLE,
}


def list_catalog_codes() -> set[str]:
    """Return the set of every registered catalog code."""
    return set(CATALOG.keys())


def get_catalog_level(code: str) -> Level | None:
    """Return the registered level for a code, or ``None`` if unknown."""
    return CATALOG.get(code)

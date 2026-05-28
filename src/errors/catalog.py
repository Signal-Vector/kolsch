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
    # ----- §4.12 Generic platform --------------------------------------
    # platform.wrapped's level is documented as "(varies)"; the
    # default-instance level is DIAGNOSTIC, matching the spec §3.4
    # reference implementation. When a wrapped exception must surface
    # at Fatal severity, raise `PlatformInternalError` instead.
    "platform.wrapped": Level.DIAGNOSTIC,
}


def list_catalog_codes() -> set[str]:
    """Return the set of every registered catalog code."""
    return set(CATALOG.keys())


def get_catalog_level(code: str) -> Level | None:
    """Return the registered level for a code, or ``None`` if unknown."""
    return CATALOG.get(code)

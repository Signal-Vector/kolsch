"""Layer 3 ConfigValidator for the ``standard`` tools variant — spec §3.4.

The kernel dispatches this during Layer 3 config validation. It
checks the structural shape of the ``tools.config`` body:

- Each ``builtin`` name is a known builtin (spec §4 enumeration).
- Each MCP server ``name`` is unique within the agent's config.
- Each ``mcp[].env_inject`` value names a secret in
  ``secrets_needed`` for the same server.

Phase 1 simplifications:
- Declared-tool ref resolution is NOT cross-checked here (would require
  the asset catalog at validation time; the Tools subsystem's runtime
  registration raises if a ref doesn't resolve).
- ``mcp[].secrets_needed`` existence is NOT checked (would require the
  Secrets provider at validation time; the registration step calls
  ``secrets.exists`` per spec §6.1 step 3).
"""

from __future__ import annotations

from typing import Any

from kernel import ValidationError, ValidationResult
from tools.builtins import builtin_names


def validate_standard_config(body: dict[str, Any]) -> ValidationResult:
    """The Layer 3 validator the kernel dispatches for
    ``tools.variant=standard``."""
    errors: list[ValidationError] = []

    builtin_list = body.get("builtin", []) or []
    if not isinstance(builtin_list, list):
        errors.append(
            ValidationError(
                layer=3,
                source="tools.standard",
                path="/builtin",
                code="not_a_list",
                message=(f"builtin must be a list; got {type(builtin_list).__name__}"),
            )
        )
        builtin_list = []

    known = builtin_names()
    for index, name in enumerate(builtin_list):
        if not isinstance(name, str):
            errors.append(
                ValidationError(
                    layer=3,
                    source="tools.standard",
                    path=f"/builtin/{index}",
                    code="not_a_string",
                    message=(
                        f"builtin entries must be strings; got {type(name).__name__}"
                    ),
                )
            )
            continue
        if name not in known:
            errors.append(
                ValidationError(
                    layer=3,
                    source="tools.standard",
                    path=f"/builtin/{index}",
                    code="unknown_builtin",
                    message=(
                        f"unknown builtin tool {name!r}; available: {sorted(known)}"
                    ),
                )
            )

    declared = body.get("declared", []) or []
    if not isinstance(declared, list):
        errors.append(
            ValidationError(
                layer=3,
                source="tools.standard",
                path="/declared",
                code="not_a_list",
                message=(f"declared must be a list; got {type(declared).__name__}"),
            )
        )
        declared = []

    for index, entry in enumerate(declared):
        if not isinstance(entry, dict) or "ref" not in entry:
            errors.append(
                ValidationError(
                    layer=3,
                    source="tools.standard",
                    path=f"/declared/{index}",
                    code="malformed_entry",
                    message="declared entries must be mappings with a 'ref' key",
                )
            )

    mcp = body.get("mcp", []) or []
    if not isinstance(mcp, list):
        errors.append(
            ValidationError(
                layer=3,
                source="tools.standard",
                path="/mcp",
                code="not_a_list",
                message=f"mcp must be a list; got {type(mcp).__name__}",
            )
        )
        mcp = []

    seen_names: set[str] = set()
    for index, entry in enumerate(mcp):
        if not isinstance(entry, dict):
            errors.append(
                ValidationError(
                    layer=3,
                    source="tools.standard",
                    path=f"/mcp/{index}",
                    code="malformed_entry",
                    message="mcp entries must be mappings",
                )
            )
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            errors.append(
                ValidationError(
                    layer=3,
                    source="tools.standard",
                    path=f"/mcp/{index}/name",
                    code="missing_name",
                    message="mcp entry missing 'name'",
                )
            )
            continue
        if name in seen_names:
            errors.append(
                ValidationError(
                    layer=3,
                    source="tools.standard",
                    path=f"/mcp/{index}/name",
                    code="duplicate_mcp_name",
                    message=(
                        f"mcp server name {name!r} appears more than once; "
                        "names must be unique within an agent's config"
                    ),
                )
            )
        seen_names.add(name)

        secrets_needed = entry.get("secrets_needed", []) or []
        env_inject = entry.get("env_inject", {}) or {}
        if isinstance(env_inject, dict) and isinstance(secrets_needed, list):
            secret_set = set(secrets_needed)
            for env_name, secret_name in env_inject.items():
                if secret_name not in secret_set:
                    errors.append(
                        ValidationError(
                            layer=3,
                            source="tools.standard",
                            path=f"/mcp/{index}/env_inject/{env_name}",
                            code="unknown_secret_in_env_inject",
                            message=(
                                f"env_inject references secret "
                                f"{secret_name!r} which is not in "
                                f"secrets_needed {sorted(secret_set)}"
                            ),
                        )
                    )

    return ValidationResult(ok=not errors, errors=errors)

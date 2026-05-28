"""Layer 1 structural validators — spec §2, §5.

Two pure functions:

- :func:`validate_partial` runs the partial Pydantic schema
  (:class:`PartialAgentConfig`); every field optional.
- :func:`validate_resolved` runs the full schema
  (:class:`schemas.ResolvedAgentConfig`); required fields enforced.

Pydantic's ``ValidationError`` exposes structured ``loc`` tuples; we
translate them into JSON Pointer paths (spec Open Item §2) and
:class:`ValidationError` findings tagged with the kernel ``source``.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from kernel.findings import ValidationError, ValidationResult
from kernel.partials import PartialAgentConfig
from schemas.config import ResolvedAgentConfig

_SOURCE_KERNEL = "kernel"


def _loc_to_json_pointer(loc: tuple[int | str, ...]) -> str:
    """Translate a Pydantic ``loc`` tuple into a JSON Pointer (RFC 6901).

    RFC 6901 escapes ``~`` as ``~0`` and ``/`` as ``~1``. Integer locs
    (list indices) appear as bare integers per the RFC.
    """
    if not loc:
        return ""
    parts: list[str] = []
    for token in loc:
        s = str(token)
        s = s.replace("~", "~0").replace("/", "~1")
        parts.append(s)
    return "/" + "/".join(parts)


def _from_pydantic(
    exc: PydanticValidationError,
    *,
    layer: int,
    source: str,
) -> list[ValidationError]:
    """Convert Pydantic's structured errors into kernel findings."""
    findings: list[ValidationError] = []
    for err in exc.errors():
        code = str(err.get("type", "validation_error"))
        message = str(err.get("msg", ""))
        loc = err.get("loc", ())
        path = _loc_to_json_pointer(tuple(loc))
        findings.append(
            ValidationError(
                layer=layer,  # type: ignore[arg-type]
                source=source,
                path=path,
                code=code,
                message=message,
            )
        )
    return findings


def validate_partial(
    partial: dict[str, Any],
    *,
    source: str = _SOURCE_KERNEL,
) -> ValidationResult:
    """Validate a partial against the schema (all fields optional).

    Spec §5: this is Layer 1's per-partial validation. Returns
    :class:`ValidationResult`; never raises a :class:`PlatformError`.
    """
    try:
        PartialAgentConfig.model_validate(partial)
    except PydanticValidationError as exc:
        return ValidationResult(
            ok=False,
            errors=_from_pydantic(exc, layer=1, source=source),
        )
    return ValidationResult(ok=True, errors=[])


def validate_resolved(
    resolved: dict[str, Any],
    *,
    source: str = _SOURCE_KERNEL,
) -> ValidationResult:
    """Validate a resolved config (required fields enforced) — Layer 1.

    Distinct from :func:`validate_partial` because the resolved config
    must satisfy the full :class:`schemas.ResolvedAgentConfig` schema —
    missing required fields are errors here.
    """
    try:
        ResolvedAgentConfig.model_validate(resolved)
    except PydanticValidationError as exc:
        return ValidationResult(
            ok=False,
            errors=_from_pydantic(exc, layer=1, source=source),
        )
    return ValidationResult(ok=True, errors=[])

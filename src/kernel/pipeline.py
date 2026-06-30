"""`resolve_and_validate` — the orchestrated config-resolution pipeline.

Drives the full Layer 1 -> resolve -> Layer 1 -> Layer 3 -> Layer 2
flow:

1. Layer 1 on each partial. Per-partial Layer 1 fails fast — a
   malformed partial is rejected before merging is attempted. Errors
   across partials are still aggregated within this step so the user
   sees every malformed partial at once.
2. Same-version check. All partials with a `schema_version` field
   must agree.
3. Merge via [`resolve`][kernel.resolver.resolve].
4. Layer 1 on the resolved dict (required fields enforced).
5. Layer 3 — for each entry in `resolved.subsystems`, look up
   `subsystem_validators[name][variant]` and call it on the entry's
   `config` body. Unknown variants produce `unknown_variant`
   findings. Validators that raise are contained as
   `validator_internal_error`.
6. Layer 2 — call each `system_validator` with the resolved dict.
   Same containment for raises.
7. Return — construct and return `ResolvedAgentConfig`.

On any errors anywhere in steps 1, 2, 4-6 the pipeline raises
`ConfigInvalid` with the aggregated findings (as plain dicts) inside
`detail.validation_errors`.
"""

from __future__ import annotations

from typing import Any

from errors import ConfigInvalid
from kernel.findings import ConfigValidator, ValidationError, ValidationResult
from kernel.resolver import resolve
from kernel.validation import validate_partial, validate_resolved
from schemas.config import ResolvedAgentConfig

_WHERE_PIPELINE = "kernel.resolver.validate"


def _raise(errors: list[ValidationError]) -> None:
    """Convert aggregated findings into a `ConfigInvalid` raise."""
    payload = [e.model_dump(mode="json") for e in errors]
    raise ConfigInvalid(validation_errors=payload, where=_WHERE_PIPELINE)


def _safe_call(
    validator: ConfigValidator,
    value: dict[str, Any],
    *,
    layer: int,
    source: str,
    path: str,
) -> ValidationResult:
    """Run `validator(value)`; contain raises as findings.

    A validator that raises is misbehaving — the kernel turns the
    failure into a `validator_internal_error` finding so other
    validators still run and the user sees every issue at once.
    """
    try:
        return validator(value)
    except Exception as exc:  # noqa: BLE001 — containing validator bugs
        return ValidationResult(
            ok=False,
            errors=[
                ValidationError(
                    layer=layer,  # type: ignore[arg-type]
                    source=source,
                    path=path,
                    code="validator_internal_error",
                    message=(f"validator raised {type(exc).__name__}: {exc}"),
                )
            ],
        )


def _check_schema_versions(
    partials: list[dict[str, Any]],
) -> list[ValidationError]:
    """Every partial that carries a `schema_version` must agree."""
    declared = [
        (i, p["schema_version"])
        for i, p in enumerate(partials)
        if "schema_version" in p and p["schema_version"] is not None
    ]
    # Nothing to check when fewer than two partials carry a version.
    minimum_for_mismatch = 2
    if len(declared) < minimum_for_mismatch:
        return []
    first_version = declared[0][1]
    return [
        ValidationError(
            layer=1,
            source=f"partial[{i}]",
            path="/schema_version",
            code="schema_version_mismatch",
            message=(
                f"schema_version {version!r} does not match the first "
                f"declared version {first_version!r}; v1 does not migrate "
                f"between versions"
            ),
        )
        for i, version in declared[1:]
        if version != first_version
    ]


def resolve_and_validate(
    partials: list[dict[str, Any]],
    subsystem_validators: dict[str, dict[str, ConfigValidator]],
    system_validators: list[ConfigValidator] | None = None,
) -> ResolvedAgentConfig:
    """Run the full Layer 1 -> resolve -> Layer 1 -> Layer 3 -> Layer 2 pipeline.

    See the module docstring for the step-by-step.

    Args:
        partials: Ordered list of partial configs.
        subsystem_validators: `{subsystem_name: {variant: validator}}`.
            Looked up per resolved-subsystem entry; missing keys
            produce `unknown_variant` findings.
        system_validators: Optional Layer 2 validators, each invoked
            on the full resolved dict.

    Returns:
        The validated `ResolvedAgentConfig`.

    Raises:
        ConfigInvalid: Any error found at any layer (aggregated across
            layers, with the per-partial Layer 1 step failing fast
            before the merge is attempted).
    """
    system_validators = system_validators or []

    # --- Step 1: Layer 1 on each partial (fails fast — no merge) -------
    partial_errors: list[ValidationError] = []
    for index, partial in enumerate(partials):
        result = validate_partial(partial, source=f"partial[{index}]")
        if not result.ok:
            partial_errors.extend(result.errors)

    # --- Step 2: same-version check ------------------------------------
    partial_errors.extend(_check_schema_versions(partials))

    if partial_errors:
        _raise(partial_errors)

    # --- Step 3: merge --------------------------------------------------
    resolved = resolve(partials)

    # --- Step 4: Layer 1 on resolved -----------------------------------
    aggregated: list[ValidationError] = []
    result = validate_resolved(resolved)
    if not result.ok:
        aggregated.extend(result.errors)
        # The resolved dict failed structural validation. Without a
        # well-formed shape Layer 2 + Layer 3 dispatches would be
        # nonsensical (e.g., subsystems may not be a dict). Raise now
        # with the Layer 1 findings; the user fixes those, re-runs, and
        # then sees any Layer 2 / Layer 3 problems on the next pass.
        _raise(aggregated)

    # --- Step 5: Layer 3 — subsystem variant dispatch ------------------
    subsystems = resolved.get("subsystems") or {}
    for name, entry in subsystems.items():
        variant = entry.get("variant")
        config_body = entry.get("config") or {}
        variants_for_subsystem = subsystem_validators.get(name) or {}
        validator = variants_for_subsystem.get(variant)
        path = f"/subsystems/{name}"
        if validator is None:
            aggregated.append(
                ValidationError(
                    layer=3,
                    source=f"kernel.dispatch.{name}",
                    path=f"{path}/variant",
                    code="unknown_variant",
                    message=(
                        f"subsystem {name!r} declares unknown variant "
                        f"{variant!r}; no validator registered"
                    ),
                )
            )
            continue
        result = _safe_call(
            validator,
            config_body,
            layer=3,
            source=f"{name}.{variant}",
            path=f"{path}/config",
        )
        if not result.ok:
            aggregated.extend(result.errors)

    # --- Step 6: Layer 2 — system validators ---------------------------
    for index, validator in enumerate(system_validators):
        result = _safe_call(
            validator,
            resolved,
            layer=2,
            source=f"system[{index}]",
            path="",
        )
        if not result.ok:
            aggregated.extend(result.errors)

    if aggregated:
        _raise(aggregated)

    # --- Step 7: construct and return ----------------------------------
    # The resolved dict already passed validate_resolved; this second
    # construction is the cheapest way to surface the typed model.
    return ResolvedAgentConfig.model_validate(resolved)

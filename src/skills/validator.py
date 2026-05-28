"""Layer 3 ConfigValidator factory for the ``standard`` skills variant.

Spec §4.2: the standard variant's validator checks that

- ``enabled`` is a list of mappings;
- each entry has a string ``ref``;
- each ``ref`` resolves to a discovered skill in the catalog;
- no ``ref`` appears twice.

The validator dispatched by the kernel must honour the
:class:`kernel.ConfigValidator` signature — sync,
``Callable[[dict], ValidationResult]``. To carry the catalog
dependency we expose a *factory* that closes over an
:class:`AssetManagement` and returns the closure the kernel calls.

The closure does no I/O (it reads a snapshot built at
``AssetManagement.initialize`` time), so it remains pure enough for
the kernel's purity expectation (Kernel §4.3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kernel import ValidationError, ValidationResult

if TYPE_CHECKING:
    from assets import AssetManagement
    from kernel import ConfigValidator


_SOURCE = "skills.standard"


def make_standard_validator(asset_management: AssetManagement) -> ConfigValidator:
    """Build the Layer 3 validator dispatched for ``skills.variant=standard``.

    The returned callable matches :data:`kernel.ConfigValidator`
    exactly: takes the ``config`` body (the dict inside ``{variant,
    config}`` per Schemas §A.7) and returns a
    :class:`kernel.ValidationResult`.
    """

    def validate(body: dict[str, Any]) -> ValidationResult:
        errors: list[ValidationError] = []

        enabled = body.get("enabled", []) or []
        if not isinstance(enabled, list):
            errors.append(
                ValidationError(
                    layer=3,
                    source=_SOURCE,
                    path="/enabled",
                    code="not_a_list",
                    message=(f"enabled must be a list; got {type(enabled).__name__}"),
                ),
            )
            return ValidationResult(ok=False, errors=errors)

        # Snapshot the catalogued skill refs once per call. Reads off
        # the immutable post-initialize catalog (Am-I7).
        cataloged = {a.ref for a in asset_management.list_assets("skill")}

        seen: set[str] = set()
        for index, entry in enumerate(enabled):
            if not isinstance(entry, dict) or "ref" not in entry:
                errors.append(
                    ValidationError(
                        layer=3,
                        source=_SOURCE,
                        path=f"/enabled/{index}",
                        code="malformed_entry",
                        message=("enabled entries must be mappings with a 'ref' key"),
                    ),
                )
                continue
            ref = entry["ref"]
            if not isinstance(ref, str):
                errors.append(
                    ValidationError(
                        layer=3,
                        source=_SOURCE,
                        path=f"/enabled/{index}/ref",
                        code="not_a_string",
                        message=f"ref must be a string; got {type(ref).__name__}",
                    ),
                )
                continue
            if ref in seen:
                errors.append(
                    ValidationError(
                        layer=3,
                        source=_SOURCE,
                        path=f"/enabled/{index}/ref",
                        code="duplicate_ref",
                        message=(
                            f"skill ref {ref!r} appears more than once; "
                            "each enabled skill must be listed once"
                        ),
                    ),
                )
                continue
            seen.add(ref)
            if ref not in cataloged:
                errors.append(
                    ValidationError(
                        layer=3,
                        source=_SOURCE,
                        path=f"/enabled/{index}/ref",
                        code="ref_not_found",
                        message=(f"enabled skill ref {ref!r} not in catalog"),
                    ),
                )

        return ValidationResult(ok=not errors, errors=errors)

    return validate

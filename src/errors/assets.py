"""Asset Management errors — Module API Reference §2 raises.

These codes are referenced by the Asset Management contract but the
error catalog (spec §4) does not yet enumerate them. Registered in
:mod:`errors.catalog` and surfaced here as concrete PlatformError
subclasses so callers can raise + match on them like any other.
"""

from errors.base import PlatformError
from errors.level import Level


class AssetsDuplicateBundleName(PlatformError):
    """Two bundles share the same name. Spec Am-I2 — fatal."""

    code = "assets.duplicate_bundle_name"
    level = Level.FATAL

    def __init__(
        self,
        name: str,
        paths: list[str],
        *,
        where: str = "assets.catalog.build",
    ) -> None:
        super().__init__(
            message=f"Duplicate bundle name {name!r} found at {paths}",
            where=where,
            detail={"name": name, "paths": list(paths)},
        )


class AssetsDuplicateName(PlatformError):
    """Two assets in the same bundle share a name across asset types.

    Under the new 2-segment ref convention
    (qol-prefix-defaults-and-unified-refs.md), ``<bundle>/<name>``
    must resolve to a unique catalog entry. A bundle that ships
    both ``tools/butler.py`` and ``agents/butler.yaml`` cannot
    disambiguate which one ``<bundle>/butler`` means; surfaces
    at catalog scan time so the failure is loud and early.
    """

    code = "assets.duplicate_name"
    level = Level.FATAL

    def __init__(
        self,
        bundle: str,
        name: str,
        refs: list[str],
        *,
        where: str = "assets.catalog.build",
    ) -> None:
        super().__init__(
            message=(
                f"Bundle {bundle!r} has multiple assets named {name!r}: "
                f"{sorted(refs)}"
            ),
            where=where,
            detail={"bundle": bundle, "name": name, "refs": sorted(refs)},
        )


class AssetsBundleNameMismatch(PlatformError):
    """A bundle's manifest name disagrees with its directory. Am-I1."""

    code = "assets.bundle_name_mismatch"
    level = Level.FATAL

    def __init__(
        self,
        directory: str,
        manifest_name: str,
        *,
        where: str = "assets.catalog.build",
    ) -> None:
        super().__init__(
            message=(
                f"Bundle directory {directory!r} declares "
                f"name {manifest_name!r} in bundle.yaml"
            ),
            where=where,
            detail={"directory": directory, "manifest_name": manifest_name},
        )


class AssetsReservedBundleName(PlatformError):
    """A bundle uses the reserved ``core`` name. Am-I3."""

    code = "assets.reserved_bundle_name"
    level = Level.FATAL

    def __init__(
        self,
        name: str,
        *,
        where: str = "assets.catalog.build",
    ) -> None:
        super().__init__(
            message=f"Bundle name {name!r} is reserved by the platform",
            where=where,
            detail={"name": name},
        )


class AssetsNotFound(PlatformError):
    """A ref or shorthand does not resolve to any catalog entry."""

    code = "assets.not_found"
    level = Level.FATAL

    def __init__(
        self,
        ref: str,
        *,
        asset_type: str | None = None,
        where: str = "assets.resolve",
    ) -> None:
        super().__init__(
            message=(
                f"Asset {ref!r}"
                + (f" of type {asset_type!r}" if asset_type else "")
                + " not found in catalog"
            ),
            where=where,
            detail={"ref": ref, "asset_type": asset_type},
        )


class AssetsInvalidRef(PlatformError):
    """A ref string does not parse as ``<bundle>/<asset-type>/<name>``."""

    code = "assets.invalid_ref"
    level = Level.FATAL

    def __init__(
        self,
        ref: str,
        *,
        where: str = "assets.resolve",
    ) -> None:
        super().__init__(
            message=f"Malformed asset ref {ref!r}",
            where=where,
            detail={"ref": ref},
        )


class AssetsAmbiguousShorthand(PlatformError):
    """A shorthand matches more than one asset. Am-I6.

    The detail lists every candidate ref so the caller's error
    message can quote them verbatim (Module API Reference §2.1).
    """

    code = "assets.ambiguous_shorthand"
    level = Level.FATAL

    def __init__(
        self,
        shorthand: str,
        candidates: list[str],
        *,
        asset_type: str | None = None,
        where: str = "assets.resolve_ref",
    ) -> None:
        super().__init__(
            message=(f"Shorthand {shorthand!r} is ambiguous; candidates: {candidates}"),
            where=where,
            detail={
                "shorthand": shorthand,
                "candidates": list(candidates),
                "asset_type": asset_type,
            },
        )


class AssetsPromptLoadFailed(PlatformError):
    """A prompt ref could not be loaded from disk during pre-resolution."""

    code = "assets.prompt_load_failed"
    level = Level.FATAL

    def __init__(
        self,
        ref: str,
        reason: str,
        *,
        where: str = "assets.assembler.pre_resolve",
    ) -> None:
        super().__init__(
            message=f"Failed to load prompt {ref!r}: {reason}",
            where=where,
            detail={"ref": ref, "reason": reason},
        )

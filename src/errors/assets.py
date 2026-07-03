"""Asset Management errors.

These codes are referenced by the Asset Management contract,
registered in `errors.catalog`, and surfaced here as concrete
`PlatformError` subclasses so callers can raise and match on them
like any other catalogued error. See the
[error model](../explanation/error-model.md) for the
classification contract.
"""

from errors.base import PlatformError
from errors.level import Level


class AssetsDuplicateBundleName(PlatformError):
    """Raised when two bundles share the same name."""

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
    """Raised when two assets in the same bundle share a name across asset types.

    Under the 2-segment ref convention, `<bundle>/<name>` must resolve
    to a unique catalog entry. A bundle that ships both
    `tools/butler.py` and `agents/butler.yaml` cannot disambiguate
    which one `<bundle>/butler` means; the duplicate surfaces at
    catalog scan time so the failure is loud and early.
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
                f"Bundle {bundle!r} has multiple assets named {name!r}: {sorted(refs)}"
            ),
            where=where,
            detail={"bundle": bundle, "name": name, "refs": sorted(refs)},
        )


class AssetsBundleNameMismatch(PlatformError):
    """Raised when a bundle's manifest name disagrees with its directory."""

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


class AssetsNotFound(PlatformError):
    """Raised when a ref or shorthand does not resolve to any catalog entry."""

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
    """Raised when a ref string does not parse as `<bundle>/<asset-type>/<name>`."""

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
    """Raised when a shorthand matches more than one asset.

    The envelope's `detail.candidates` lists every matching ref so
    the caller's error message can quote them verbatim.
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
    """Raised when a prompt ref could not be loaded from disk during pre-resolution."""

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

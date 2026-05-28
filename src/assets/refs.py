"""Asset ref parsing + shorthand resolution — spec §5.

Full refs are ``<bundle>/<asset-type>/<asset-name>``. Shorthand refs
(spec §5.2) are bare names that resolve when exactly one asset of
the requested type matches; ambiguous shorthand raises explicitly
listing all candidates (Am-I6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from errors import AssetsAmbiguousShorthand, AssetsInvalidRef, AssetsNotFound

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog


# A namespaced ref has exactly three slash-separated segments.
_REF_SEGMENT_COUNT = 3


def parse_ref(ref: str) -> tuple[str, str, str]:
    """Split a full ref into ``(bundle, asset_type, name)``.

    Raises:
        AssetsInvalidRef: ``ref`` does not have exactly three
            non-empty slash-separated segments.
    """
    if "/" not in ref:
        raise AssetsInvalidRef(ref=ref)
    parts = ref.split("/")
    if len(parts) != _REF_SEGMENT_COUNT or not all(parts):
        raise AssetsInvalidRef(ref=ref)
    bundle, asset_type, name = parts
    return bundle, asset_type, name


def resolve_shorthand(
    catalog: AssetCatalog,
    ref: str,
    asset_type: str,
) -> str:
    """Resolve a full or shorthand ref to a full ref against ``catalog``.

    Per spec §5.2:
    - ``/`` present → treat as full ref; verify it exists.
    - ``/`` absent → search catalog for assets of ``asset_type`` whose
      final ref segment matches; exactly-one returns the full ref;
      multiple raises with all candidates (Am-I6); none raises
      ``assets.not_found``.

    Shorthand is supported for ``tool`` and ``skill`` only per spec
    §5.2; for ``agent`` and ``prompt`` the caller must supply the
    full ref (this function does not enforce that constraint — it
    surfaces the same not-found error if shorthand is used for an
    unsupported type and happens not to match).
    """
    if "/" in ref:
        if ref in catalog.assets:
            return ref
        raise AssetsNotFound(ref=ref, asset_type=asset_type)

    candidates = [
        entry.ref
        for entry in catalog.assets.values()
        if entry.asset_type == asset_type and entry.ref.rsplit("/", 1)[-1] == ref
    ]
    if not candidates:
        raise AssetsNotFound(ref=ref, asset_type=asset_type)
    if len(candidates) > 1:
        raise AssetsAmbiguousShorthand(
            shorthand=ref,
            candidates=candidates,
            asset_type=asset_type,
        )
    return candidates[0]

"""Asset ref parsing + shorthand resolution — spec §5 +
qol-prefix-defaults-and-unified-refs.md Step 2.

Two complementary functions:

- :func:`parse_ref` — pure shape parser. Handles 2-segment
  ``<bundle>/<name>`` (canonical) and 3-segment
  ``<bundle>/<asset_type>/<name>`` (legacy; emits a deprecation
  warning). No catalog access.
- :func:`resolve_shorthand` — catalog-aware resolver. Handles
  1-segment shorthand AND the 2 / 3-segment forms by mapping
  them to the canonical internal catalog key.

Grammar (per Step 1 writeup §2):

- ``<bundle>/<asset_type>/<name>`` — 3-seg legacy → deprecation
  warning + still resolves.
- ``<bundle>/<name>`` — 2-seg canonical → asset_type inferred
  from the field where the ref appears.
- ``<name>`` — 1-seg shorthand → catalog scan for the
  field-supplied asset type (exactly-one or
  AssetsAmbiguousShorthand).
- ``<prefix>:<path>`` — prefixed file refs are NOT handled here;
  they belong to :mod:`templating.context_files`.

The catalog's internal storage still keys on the 3-seg form per
spec Part 6 (out of scope: refactor catalog data model). This
module is the translation layer between the new ergonomic input
shapes and the existing internal storage.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable

from errors import (
    AssetsAmbiguousShorthand,
    AssetsInvalidRef,
    AssetsNotFound,
    ConfigDeprecatedRefForm,
)

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog


# Asset-name grammar — qol-prefix-defaults-and-unified-refs.md §1.
# Lowercase alphanumeric + `_-`; must start with a letter or digit.
# Matches the existing bundle-name rule from `assets.manifest`.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Singular asset type → plural directory segment (matches
# `assets.catalog._scan_*` which writes refs as
# `<bundle>/<plural>/<name>`).
_TYPE_TO_SEGMENT: dict[str, str] = {
    "tool": "tools",
    "skill": "skills",
    "agent": "agents",
    "prompt": "prompts",
}
_SEGMENT_TO_TYPE: dict[str, str] = {v: k for k, v in _TYPE_TO_SEGMENT.items()}


def _is_valid_name(value: str) -> bool:
    return isinstance(value, str) and bool(_NAME_PATTERN.match(value))


def parse_ref(
    ref: str,
    *,
    field_asset_type: str | None = None,
    emit_warning: Callable[[ConfigDeprecatedRefForm], None] | None = None,
) -> tuple[str, str, str]:
    """Parse a ref into ``(bundle, asset_type, name)``.

    Accepts the 2-segment canonical form (``<bundle>/<name>``)
    and the 3-segment legacy form (``<bundle>/<asset_type>/<name>``).

    Args:
        ref: the ref string to parse.
        field_asset_type: singular asset type ('tool', 'skill',
            'agent', 'prompt') inferred from where the ref
            appears. Required for 2-segment input; for 3-segment
            input, used as a cross-check against the middle
            segment.
        emit_warning: optional sink invoked with a
            :class:`ConfigDeprecatedRefForm` when a legacy
            3-segment ref is parsed.

    Returns:
        ``(bundle, asset_type, name)`` where ``asset_type`` is
        the *singular* form ('tool', not 'tools').

    Raises:
        AssetsInvalidRef: malformed segment count, prefix sigil,
            invalid name characters, unknown middle segment, or
            2-seg input without ``field_asset_type``.
    """
    if not isinstance(ref, str) or not ref:
        raise AssetsInvalidRef(ref=ref)
    # Prefix sigil is the file-path namespace; parse_ref handles
    # catalog refs only. Catalog refs use `/` exclusively.
    if ":" in ref:
        raise AssetsInvalidRef(ref=ref)

    parts = ref.split("/")
    if len(parts) == 3:
        bundle, segment, name = parts
        if not (_is_valid_name(bundle) and _is_valid_name(name)):
            raise AssetsInvalidRef(ref=ref)
        if segment not in _SEGMENT_TO_TYPE:
            raise AssetsInvalidRef(ref=ref)
        asset_type = _SEGMENT_TO_TYPE[segment]
        if field_asset_type is not None and field_asset_type != asset_type:
            raise AssetsInvalidRef(ref=ref)
        if emit_warning is not None:
            emit_warning(
                ConfigDeprecatedRefForm(
                    deprecated_form=ref,
                    canonical_form=f"{bundle}/{name}",
                )
            )
        return bundle, asset_type, name

    if len(parts) == 2:
        bundle, name = parts
        if not (_is_valid_name(bundle) and _is_valid_name(name)):
            raise AssetsInvalidRef(ref=ref)
        if field_asset_type is None or field_asset_type not in _TYPE_TO_SEGMENT:
            # 2-seg form needs the field context to know which
            # asset type the ref targets.
            raise AssetsInvalidRef(ref=ref)
        return bundle, field_asset_type, name

    # 1-segment is the catalog-aware path (use resolve_shorthand);
    # 4+ segments are invalid.
    raise AssetsInvalidRef(ref=ref)


def resolve_shorthand(
    catalog: AssetCatalog,
    ref: str,
    asset_type: str,
    *,
    emit_warning: Callable[[ConfigDeprecatedRefForm], None] | None = None,
) -> str:
    """Resolve any of the three catalog-ref input forms to the
    canonical internal ref string.

    Forms accepted:

    - **3-segment** legacy ``<bundle>/<asset_type>/<name>`` →
      passthrough lookup; emits a deprecation warning.
    - **2-segment** canonical ``<bundle>/<name>`` → looked up
      against the catalog by composing
      ``<bundle>/<plural>/<name>``.
    - **1-segment** shorthand ``<name>`` → catalog scan for
      entries of ``asset_type`` whose final segment matches.

    Args:
        catalog: built asset catalog.
        ref: the ref string.
        asset_type: singular asset type the field demands.
        emit_warning: optional sink for
            :class:`ConfigDeprecatedRefForm`.

    Returns:
        The canonical 3-seg ref string used as the catalog key.

    Raises:
        AssetsInvalidRef: malformed input.
        AssetsNotFound: no catalog entry matches.
        AssetsAmbiguousShorthand: multiple 1-segment candidates.
    """
    if not isinstance(ref, str) or not ref or ":" in ref:
        raise AssetsInvalidRef(ref=ref)
    if asset_type not in _TYPE_TO_SEGMENT:
        raise AssetsInvalidRef(ref=ref)
    segment = _TYPE_TO_SEGMENT[asset_type]
    parts = ref.split("/")

    if len(parts) == 3:
        # Legacy form — middle segment is part of the catalog key
        # already. Validate shape, look up, warn.
        bundle, middle, name = parts
        if not (_is_valid_name(bundle) and _is_valid_name(name)):
            raise AssetsInvalidRef(ref=ref)
        if middle != segment:
            raise AssetsNotFound(ref=ref, asset_type=asset_type)
        if ref not in catalog.assets:
            raise AssetsNotFound(ref=ref, asset_type=asset_type)
        if emit_warning is not None:
            emit_warning(
                ConfigDeprecatedRefForm(
                    deprecated_form=ref,
                    canonical_form=f"{bundle}/{name}",
                )
            )
        return ref

    if len(parts) == 2:
        bundle, name = parts
        if not (_is_valid_name(bundle) and _is_valid_name(name)):
            raise AssetsInvalidRef(ref=ref)
        canonical = f"{bundle}/{segment}/{name}"
        entry = catalog.assets.get(canonical)
        if entry is None or entry.asset_type != asset_type:
            raise AssetsNotFound(ref=ref, asset_type=asset_type)
        return canonical

    if len(parts) == 1:
        name = parts[0]
        if not _is_valid_name(name):
            raise AssetsInvalidRef(ref=ref)
        candidates = [
            entry.ref
            for entry in catalog.assets.values()
            if entry.asset_type == asset_type
            and entry.ref.rsplit("/", 1)[-1] == name
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

    raise AssetsInvalidRef(ref=ref)

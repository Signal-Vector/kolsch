"""Asset ref grammar — Phase 3.0 Step 1 (R3 #5 lock).

A catalog ref is the canonical 3-segment string
``<bundle>/<plural>/<name>`` where the middle segment is a known
asset-type plural (``tools`` / ``skills`` / ``agents`` /
``prompts`` / ``workflows`` / ``nodes``). Phase 3.0 Step 1B
removed the 1-segment shorthand and 2-segment-with-field-context
forms — every catalog ref now flows through the same parser,
builder, and predicate.

Public surface:

- :func:`build_ref` — construct a canonical ref from its parts.
  The SOLE writer of catalog-key strings; inline f-strings
  ``f"{bundle}/{plural}/{name}"`` are forbidden under AC 1.11.
- :func:`parse_three_segment` — shape-check + split a ref into
  ``(bundle, plural_segment, name)``. Returns the **plural**
  middle segment so callers can read straight off
  :attr:`AssetCatalog.assets` without an intermediate translation.
- :func:`is_three_segment` — boolean predicate over
  :func:`parse_three_segment`. Never raises.

Prefixed file refs (``kolsch:`` / ``workspace:``) belong to
:mod:`templating.context_files` — this module does not handle them.
"""

from __future__ import annotations

import re

# Asset-name grammar — qol-prefix-defaults-and-unified-refs.md §1.
# Lowercase alphanumeric + `_-`; must start with a letter or digit.
# Matches the existing bundle-name rule from `assets.manifest`.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Singular asset type → plural directory segment (matches
# `assets.catalog._scan_*` which writes refs as
# `<bundle>/<plural>/<name>`).
#
# Phase 3.0 Step 1 — R3 #5 lock: 3-seg is now canonical for ALL
# asset types. ``workflow``/``node`` join the original four so
# refs to workflow YAMLs and authored Python node modules go
# through the same builder/parser/predicate trio (:func:`build_ref`,
# :func:`parse_three_segment`, :func:`is_three_segment`) as
# tools/agents/skills/prompts.
_TYPE_TO_SEGMENT: dict[str, str] = {
    "tool": "tools",
    "skill": "skills",
    "agent": "agents",
    "prompt": "prompts",
    "workflow": "workflows",
    "node": "nodes",
}
_SEGMENT_TO_TYPE: dict[str, str] = {v: k for k, v in _TYPE_TO_SEGMENT.items()}


def build_ref(bundle: str, asset_type: str, name: str) -> str:
    """The ONE canonical 3-segment ref builder.

    Phase 3.0 Step 1 (R3 #5 lock) — single source of truth for
    catalog-entry ref construction. ALL inline f-strings of the form
    ``f"{bundle}/{plural}/{name}"`` route through here.

    Args:
        bundle: bundle name (any non-empty string; catalog scanners
            and authz validators apply :func:`_is_valid_name`
            independently).
        asset_type: singular asset type — one of
            ``tool``/``skill``/``agent``/``prompt``/``workflow``/``node``.
            Unknown values raise ``ValueError`` so a miscast doesn't
            silently land in the catalog as ``foo/Unknown/bar``.
        name: the asset's final-segment name.

    Returns:
        The canonical 3-segment ref string
        ``<bundle>/<plural>/<name>``.
    """
    try:
        segment = _TYPE_TO_SEGMENT[asset_type]
    except KeyError as exc:
        msg = (
            f"build_ref: unknown asset_type {asset_type!r}; "
            f"expected one of {sorted(_TYPE_TO_SEGMENT)}"
        )
        raise ValueError(msg) from exc
    return f"{bundle}/{segment}/{name}"


def parse_three_segment(ref: str) -> tuple[str, str, str]:
    """Shape-check + split a 3-segment ref into its components.

    Phase 3.0 Step 1 (R3 #5 lock) — the SOLE ref shape-parser.
    Returns the **plural** middle segment as-is so callers can route
    into :attr:`AssetCatalog.assets` or a downstream
    ``_SEGMENT_TO_TYPE`` reverse-map without an intermediate
    translation.

    Args:
        ref: the catalog ref string to parse.

    Returns:
        ``(bundle, plural_segment, name)`` where ``plural_segment``
        is the literal middle segment (``tools``/``agents``/
        ``workflows``/etc.).

    Raises:
        ValueError: ``ref`` is not a 3-segment string with each
            segment matching :data:`_NAME_PATTERN` for bundle+name
            and a known plural for the middle segment.
    """
    if not isinstance(ref, str) or not ref or ":" in ref:
        msg = f"parse_three_segment: not a 3-segment ref: {ref!r}"
        raise ValueError(msg)
    parts = ref.split("/")
    if len(parts) != 3:
        msg = f"parse_three_segment: not a 3-segment ref: {ref!r}"
        raise ValueError(msg)
    bundle, segment, name = parts
    if not (_is_valid_name(bundle) and _is_valid_name(name)):
        msg = f"parse_three_segment: malformed bundle/name in {ref!r}"
        raise ValueError(msg)
    if segment not in _SEGMENT_TO_TYPE:
        msg = (
            f"parse_three_segment: unknown plural segment {segment!r} "
            f"in {ref!r}; expected one of {sorted(_SEGMENT_TO_TYPE)}"
        )
        raise ValueError(msg)
    return bundle, segment, name


def is_three_segment(ref: str) -> bool:
    """Cheap predicate over :func:`parse_three_segment`.

    Returns ``True`` iff ``ref`` parses; never raises.
    """
    try:
        parse_three_segment(ref)
    except ValueError:
        return False
    return True


def _is_valid_name(value: str) -> bool:
    return isinstance(value, str) and bool(_NAME_PATTERN.match(value))

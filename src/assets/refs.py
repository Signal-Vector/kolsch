"""Asset ref grammar — the canonical 3-segment ref builder and parser.

A catalog ref is the canonical 3-segment string
`<bundle>/<plural>/<name>` where the middle segment is a known
asset-type plural (`tools` / `skills` / `agents` / `prompts` /
`workflows` / `nodes`). Every catalog ref flows through the same
parser, builder, and predicate.

- [`build_ref`][assets.refs.build_ref] — construct a canonical ref
  from its parts. The SOLE writer of catalog-key strings; inline
  f-strings of the form `f"{bundle}/{plural}/{name}"` are forbidden.
- [`parse_three_segment`][assets.refs.parse_three_segment] —
  shape-check and split a ref into `(bundle, plural_segment, name)`.
  Returns the **plural** middle segment so callers can read straight
  off `AssetCatalog.assets` without an intermediate translation.
- [`is_three_segment`][assets.refs.is_three_segment] — boolean
  predicate over `parse_three_segment`. Never raises.

Prefixed file refs (`kolsch:` / `workspace:`) belong to
`templating.context_files`; this module does not handle them.
"""

from __future__ import annotations

import re

# Asset-name grammar — lowercase alphanumeric plus `_-`; must start
# with a letter or digit. Matches the existing bundle-name rule from
# `assets.manifest`.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Singular asset type → plural directory segment (matches the
# ref-writing call sites in `assets.catalog._scan_*` and
# `workflows.catalog`).
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
    """Build a canonical 3-segment catalog ref from its parts.

    The single source of truth for catalog-entry ref construction;
    every inline f-string of the form
    `f"{bundle}/{plural}/{name}"` routes through here.

    Args:
        bundle: Bundle name. Any non-empty string; catalog scanners
            and authz validators apply `_is_valid_name` independently.
        asset_type: Singular asset type — one of `tool` / `skill` /
            `agent` / `prompt` / `workflow` / `node`. Unknown values
            raise `ValueError` so a miscast does not silently land in
            the catalog as `foo/Unknown/bar`.
        name: The asset's final-segment name.

    Returns:
        The canonical 3-segment ref string `<bundle>/<plural>/<name>`.

    Raises:
        ValueError: `asset_type` is not a recognised singular type.
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
    """Shape-check and split a 3-segment ref into its components.

    The single ref shape-parser. Returns the **plural** middle
    segment as-is so callers can route into `AssetCatalog.assets` or
    a downstream `_SEGMENT_TO_TYPE` reverse-map without an
    intermediate translation.

    Args:
        ref: The catalog ref string to parse.

    Returns:
        `(bundle, plural_segment, name)` where `plural_segment` is
        the literal middle segment (`tools` / `agents` /
        `workflows` / etc.).

    Raises:
        ValueError: `ref` is not a 3-segment string with each
            segment matching `_NAME_PATTERN` for bundle+name and a
            known plural for the middle segment.
    """
    if not isinstance(ref, str) or not ref or ":" in ref:
        msg = f"parse_three_segment: not a 3-segment ref: {ref!r}"
        raise ValueError(msg)
    parts = ref.split("/")
    if len(parts) != 3:  # noqa: PLR2004 — function is literally `parse_three_segment`
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
    """Return `True` when `ref` parses as a 3-segment catalog ref.

    Cheap predicate over
    [`parse_three_segment`][assets.refs.parse_three_segment]; never
    raises.
    """
    try:
        parse_three_segment(ref)
    except ValueError:
        return False
    return True


def _is_valid_name(value: str) -> bool:
    return isinstance(value, str) and bool(_NAME_PATTERN.match(value))

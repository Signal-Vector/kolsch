"""Unit tests for the new ref grammar — qol-prefix-defaults-
and-unified-refs.md Step 2.

Covers:

- ``parse_ref`` accepts 2-segment + 3-segment input; rejects
  1-segment (resolve_shorthand's domain), 4+-segment, and
  invalid name characters.
- ``resolve_shorthand`` covers all three catalog-ref input
  forms (1 / 2 / 3-segment) against a stub catalog.
- The legacy 3-segment form fires
  :class:`ConfigDeprecatedRefForm` through the ``emit_warning``
  sink.
"""

from __future__ import annotations

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from assets.refs import parse_ref, resolve_shorthand
from errors import (
    AssetsAmbiguousShorthand,
    AssetsInvalidRef,
    AssetsNotFound,
    ConfigDeprecatedRefForm,
)


# ---------------------------------------------------------------------------
# Stub catalog.
# ---------------------------------------------------------------------------


def _catalog_with_tool(bundle: str, name: str) -> AssetCatalog:
    ref = f"{bundle}/tools/{name}"
    entry = AssetEntry(
        ref=ref,
        asset_type="tool",
        bundle=bundle,
        path=f"/tmp/{ref}.py",
        metadata={},
    )
    return AssetCatalog(bundles={}, assets={ref: entry})


# ---------------------------------------------------------------------------
# parse_ref.
# ---------------------------------------------------------------------------


def test_parse_ref_3_segment_legacy_emits_deprecation_warning() -> None:
    warnings: list[ConfigDeprecatedRefForm] = []
    bundle, asset_type, name = parse_ref(
        "kolsch/tools/bash",
        emit_warning=warnings.append,
    )
    assert (bundle, asset_type, name) == ("kolsch", "tool", "bash")
    assert len(warnings) == 1
    assert warnings[0].detail["deprecated_form"] == "kolsch/tools/bash"
    assert warnings[0].detail["canonical_form"] == "kolsch/bash"


def test_parse_ref_3_segment_no_warning_without_sink() -> None:
    """When ``emit_warning`` is not provided, the legacy form
    still parses cleanly — the sink is opt-in."""
    bundle, asset_type, name = parse_ref("kolsch/tools/bash")
    assert (bundle, asset_type, name) == ("kolsch", "tool", "bash")


def test_parse_ref_3_segment_with_matching_field_asset_type_succeeds() -> None:
    bundle, asset_type, name = parse_ref(
        "kolsch/tools/bash", field_asset_type="tool"
    )
    assert (bundle, asset_type, name) == ("kolsch", "tool", "bash")


def test_parse_ref_3_segment_with_mismatched_field_asset_type_raises() -> None:
    # field says "agent" but middle segment is "tools".
    with pytest.raises(AssetsInvalidRef):
        parse_ref("kolsch/tools/bash", field_asset_type="agent")


def test_parse_ref_2_segment_with_field_asset_type() -> None:
    bundle, asset_type, name = parse_ref(
        "kolsch/bash", field_asset_type="tool"
    )
    assert (bundle, asset_type, name) == ("kolsch", "tool", "bash")


def test_parse_ref_2_segment_without_field_asset_type_raises() -> None:
    # 2-seg requires the field context to disambiguate.
    with pytest.raises(AssetsInvalidRef):
        parse_ref("kolsch/bash")


def test_parse_ref_2_segment_emits_no_deprecation() -> None:
    warnings: list[ConfigDeprecatedRefForm] = []
    parse_ref("kolsch/bash", field_asset_type="tool", emit_warning=warnings.append)
    assert warnings == []


def test_parse_ref_1_segment_raises_invalid_ref() -> None:
    # 1-segment shorthand is resolve_shorthand's domain.
    with pytest.raises(AssetsInvalidRef):
        parse_ref("bash", field_asset_type="tool")


def test_parse_ref_4_segments_raises_invalid_ref() -> None:
    with pytest.raises(AssetsInvalidRef):
        parse_ref("a/b/c/d", field_asset_type="tool")


@pytest.mark.parametrize(
    "ref",
    [
        "Kolsch/bash",  # uppercase bundle
        "kolsch/Bash",  # uppercase name
        "kolsch/ba sh",  # space
        "kolsch/ba.sh",  # dot
        "_kolsch/bash",  # leading underscore (must start with letter/digit)
        "kolsch/-bash",  # leading hyphen on name
    ],
)
def test_parse_ref_rejects_invalid_name_chars(ref: str) -> None:
    with pytest.raises(AssetsInvalidRef):
        parse_ref(ref, field_asset_type="tool")


def test_parse_ref_rejects_prefixed_form() -> None:
    # `:` is the file-path sigil; parse_ref handles catalog refs only.
    with pytest.raises(AssetsInvalidRef):
        parse_ref("kolsch:bash", field_asset_type="tool")


def test_parse_ref_rejects_empty_string() -> None:
    with pytest.raises(AssetsInvalidRef):
        parse_ref("", field_asset_type="tool")


# ---------------------------------------------------------------------------
# resolve_shorthand — catalog-aware resolver.
# ---------------------------------------------------------------------------


def test_resolve_shorthand_1_segment_returns_full_ref() -> None:
    catalog = _catalog_with_tool("kolsch", "bash")
    assert resolve_shorthand(catalog, "bash", "tool") == "kolsch/tools/bash"


def test_resolve_shorthand_2_segment_returns_full_ref() -> None:
    catalog = _catalog_with_tool("kolsch", "bash")
    assert resolve_shorthand(catalog, "kolsch/bash", "tool") == "kolsch/tools/bash"


def test_resolve_shorthand_3_segment_legacy_emits_deprecation() -> None:
    catalog = _catalog_with_tool("kolsch", "bash")
    warnings: list[ConfigDeprecatedRefForm] = []
    canonical = resolve_shorthand(
        catalog, "kolsch/tools/bash", "tool", emit_warning=warnings.append
    )
    assert canonical == "kolsch/tools/bash"
    assert len(warnings) == 1


def test_resolve_shorthand_2_segment_unknown_raises_not_found() -> None:
    catalog = _catalog_with_tool("kolsch", "bash")
    with pytest.raises(AssetsNotFound):
        resolve_shorthand(catalog, "kolsch/missing", "tool")


def test_resolve_shorthand_1_segment_ambiguous_raises() -> None:
    # Two different bundles ship a tool named "bash".
    entries = {
        "kolsch/tools/bash": AssetEntry(
            ref="kolsch/tools/bash",
            asset_type="tool",
            bundle="kolsch",
            path="/tmp/k.py",
            metadata={},
        ),
        "andre/tools/bash": AssetEntry(
            ref="andre/tools/bash",
            asset_type="tool",
            bundle="andre",
            path="/tmp/a.py",
            metadata={},
        ),
    }
    catalog = AssetCatalog(bundles={}, assets=entries)
    with pytest.raises(AssetsAmbiguousShorthand):
        resolve_shorthand(catalog, "bash", "tool")


def test_resolve_shorthand_rejects_invalid_asset_type() -> None:
    catalog = _catalog_with_tool("kolsch", "bash")
    with pytest.raises(AssetsInvalidRef):
        resolve_shorthand(catalog, "kolsch/bash", "made_up_type")


def test_resolve_shorthand_rejects_prefixed_input() -> None:
    catalog = _catalog_with_tool("kolsch", "bash")
    with pytest.raises(AssetsInvalidRef):
        resolve_shorthand(catalog, "kolsch:bash", "tool")

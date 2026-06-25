"""Phase 3.0 Step 1 — refs unification tests (R3 #5 + Round 5 H3).

Covers ``build_ref`` / ``parse_three_segment`` / ``is_three_segment``
+ ``AssetCatalog.lookup`` + the chokepoint discipline. The full
codemod that removes 1-seg ``resolve_shorthand`` callers lives in
``process/qol-refs-1seg-removal.md`` and ships in a follow-up
commit; Step 1 lands the chokepoint API + the inline-f-string
cleanup in ``catalog.py`` / ``authz.py`` / ``observers.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from assets.refs import build_ref, is_three_segment, parse_three_segment

# ---------------------------------------------------------------------------
# 1.9 — build_ref / parse_three_segment / is_three_segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bundle", "asset_type", "name", "expected"),
    [
        ("acme", "tool", "bash", "acme/tools/bash"),
        ("acme", "agent", "bot", "acme/agents/bot"),
        ("acme", "skill", "k", "acme/skills/k"),
        ("acme", "prompt", "p", "acme/prompts/p"),
        ("acme", "workflow", "triage", "acme/workflows/triage"),
        ("acme", "node", "echo", "acme/nodes/echo"),
    ],
)
def test_build_ref_all_four_asset_types(
    bundle: str, asset_type: str, name: str, expected: str
) -> None:
    assert build_ref(bundle, asset_type, name) == expected


def test_build_ref_rejects_unknown_asset_type() -> None:
    with pytest.raises(ValueError, match="unknown asset_type"):
        build_ref("acme", "widget", "x")


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("acme/tools/bash", ("acme", "tools", "bash")),
        ("acme/agents/bot", ("acme", "agents", "bot")),
        ("acme/workflows/triage", ("acme", "workflows", "triage")),
        ("acme/nodes/echo", ("acme", "nodes", "echo")),
    ],
)
def test_parse_three_segment_returns_plural_middle(
    ref: str, expected: tuple[str, str, str]
) -> None:
    assert parse_three_segment(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "acme/tools",  # 2 segments
        "tools/bash",  # 2 segments
        "acme/widget/x",  # unknown middle segment
        "acme/tools/bash/extra",  # 4 segments
        "",  # empty
        "acme:tools/bash",  # colon (file-path namespace)
        "Acme/tools/bash",  # uppercase bundle
        "acme/tools/Bash",  # uppercase name
    ],
)
def test_parse_three_segment_raises_on_bad_shape(ref: str) -> None:
    with pytest.raises(ValueError):
        parse_three_segment(ref)


def test_is_three_segment_predicate() -> None:
    assert is_three_segment("acme/tools/bash") is True
    assert is_three_segment("acme/bash") is False
    assert is_three_segment("") is False
    assert is_three_segment("acme/widget/x") is False


# ---------------------------------------------------------------------------
# 1.10 — AssetCatalog.lookup chokepoint
# ---------------------------------------------------------------------------


def _seed_catalog() -> AssetCatalog:
    cat = AssetCatalog()
    cat.assets["acme/agents/bot"] = AssetEntry(
        ref="acme/agents/bot",
        asset_type="agent",
        bundle="acme",
        path="bundles/acme/agents/bot.yaml",
        metadata={},
    )
    cat.assets["acme/tools/bash"] = AssetEntry(
        ref="acme/tools/bash",
        asset_type="tool",
        bundle="acme",
        path="bundles/acme/tools/bash.py",
        metadata={},
    )
    return cat


def test_lookup_returns_entry_for_known_ref() -> None:
    cat = _seed_catalog()
    entry = cat.lookup("acme/agents/bot")
    assert entry is not None
    assert entry.asset_type == "agent"
    assert entry.bundle == "acme"


def test_lookup_returns_none_for_unknown_ref() -> None:
    cat = _seed_catalog()
    assert cat.lookup("acme/agents/missing") is None


def test_lookup_returns_none_when_expected_type_mismatches() -> None:
    cat = _seed_catalog()
    assert cat.lookup("acme/agents/bot", expected_type="workflow") is None
    assert cat.lookup("acme/tools/bash", expected_type="agent") is None


def test_lookup_returns_none_for_malformed_ref() -> None:
    cat = _seed_catalog()
    assert cat.lookup("not-a-3-seg") is None
    assert cat.lookup("acme/bash") is None
    assert cat.lookup("") is None


def test_lookup_returns_entry_when_expected_type_matches() -> None:
    cat = _seed_catalog()
    entry = cat.lookup("acme/tools/bash", expected_type="tool")
    assert entry is not None
    assert entry.ref == "acme/tools/bash"


# ---------------------------------------------------------------------------
# 1.11 — No inline 3-seg ref construction in catalog.py / authz.py / observers.py
# ---------------------------------------------------------------------------


def test_no_inline_3seg_f_strings_in_chokepoint_files() -> None:
    src_root = Path(__file__).resolve().parent.parent.parent / "src"
    targets = [
        src_root / "assets" / "catalog.py",
        src_root / "providers" / "authz.py",
        src_root / "engine" / "observers.py",
    ]
    for path in targets:
        assert path.exists(), path
        result = subprocess.run(
            ["grep", "-Pn", r'f"\{[^}]*\}/[a-z_]*/\{[^}]*\}"', str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        # grep -P prints matched lines on success; AC 1.11 requires zero
        # matches. Comment lines and docstrings are flagged only if they
        # genuinely contain an interpolating f-string of that shape.
        assert result.stdout.strip() == "", (
            f"{path}: inline 3-seg f-string still present:\n{result.stdout}"
        )

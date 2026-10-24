"""Unit tests for `files.authz_target` — canonical authz path targets.

Pins the display / strict / loose forms of a file tool's `path`
argument, the lexical pattern normalisation, and the effect-based
matching rule (allows strict, denies/prompts loose). Real symlinks
under `tmp_path`; the case flag is injected so the suite behaves the
same on case-sensitive Linux and case-insensitive macOS.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import files
from files.authz_target import (
    PathTarget,
    is_case_insensitive_fs,
    normalise_path_target,
    path_pattern_forms,
    path_pattern_matches,
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A resolved workspace root with the symlinks the spec rows use."""
    root = (tmp_path / "ws").resolve()
    (root / "bundles").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "platform.yaml").write_text("x")
    (root / "data" / "link").symlink_to("../platform.yaml")
    (root / "data" / "dirlink").symlink_to("../bundles")
    (root / "bundles" / "blink").symlink_to("../data")
    return root


def _target(raw: str, root: Path | None, *, ci: bool = False) -> PathTarget:
    return normalise_path_target(raw, root=root, case_insensitive=ci)


def _with_slash_variants(forms: list[str]) -> tuple[str, ...]:
    """Each form followed by its other trailing-`/` spelling (loose rule)."""
    out: list[str] = []
    for form in forms:
        other = (
            form
            if form in (".", "/")
            else form[:-1]
            if form.endswith("/")
            else form + "/"
        )
        out.extend(f for f in (form, other) if f not in out)
    return tuple(out)


# --- normalise_path_target: display / strict / loose -------------------------


@pytest.mark.parametrize(
    ("raw", "display", "strict", "extra_loose"),
    [
        ("platform.yaml", "platform.yaml", ["platform.yaml", "R/platform.yaml"], []),
        (
            "./bundles//a/./b.yaml",
            "bundles/a/b.yaml",
            ["bundles/a/b.yaml", "R/bundles/a/b.yaml"],
            [],
        ),
        (
            "bundles/../platform.yaml",
            "platform.yaml",
            ["platform.yaml", "R/platform.yaml"],
            [],
        ),
        ("R/data/x", "data/x", ["data/x", "R/data/x"], []),
        (
            "data/link",
            "platform.yaml",
            ["platform.yaml", "R/platform.yaml"],
            ["data/link", "R/data/link"],
        ),
        (
            "data/dirlink/x",
            "bundles/x",
            ["bundles/x", "R/bundles/x"],
            ["data/dirlink/x", "R/data/dirlink/x"],
        ),
        ("bundles/", "bundles/", ["bundles/", "R/bundles/"], []),
        (".", ".", [".", "R"], []),
        ("", ".", [".", "R"], []),
        (
            "bundles\\..\\platform.yaml",
            "bundles\\..\\platform.yaml",
            ["bundles\\..\\platform.yaml", "R/bundles\\..\\platform.yaml"],
            [],
        ),
    ],
)
def test_inside_workspace_forms(
    ws: Path, raw: str, display: str, strict: list[str], extra_loose: list[str]
) -> None:
    def sub(s: str) -> str:
        return s.replace("R", ws.as_posix(), 1) if s.startswith("R") else s

    target = _target(sub(raw), ws)

    assert target.raw == sub(raw)
    assert target.display == display
    assert target.strict_forms == tuple(sub(s) for s in strict)
    assert target.loose_forms == _with_slash_variants(
        [sub(s) for s in strict + extra_loose]
    )
    assert target.root_given == ws.as_posix()
    assert target.root_resolved == ws.as_posix()


@pytest.mark.parametrize("raw", ["R/../etc/passwd", "../other/x"])
def test_outside_workspace_has_one_absolute_form(ws: Path, raw: str) -> None:
    raw = raw.replace("R", ws.as_posix(), 1)
    expected = (ws / raw).resolve().as_posix()

    target = _target(raw, ws)

    assert target.display == expected
    assert target.strict_forms == (expected,)
    assert target.loose_forms == (expected, expected + "/")


def test_embedded_nul_raises_value_error(ws: Path) -> None:
    with pytest.raises(ValueError, match="NUL"):
        _target("a\x00b", ws)
    with pytest.raises(ValueError, match="NUL"):
        _target("a\x00b", None)


def test_symlink_loop_never_escapes_as_a_non_value_error(ws: Path) -> None:
    (ws / "loop").symlink_to("loop")

    try:
        target = _target("loop/x", ws)
    except ValueError:
        return
    # Python 3.13+ resolves loops without raising; whatever comes back
    # is still a single canonical display form.
    assert target.display == target.strict_forms[0]


def test_resolution_oserror_becomes_value_error(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Path, *, strict: bool = False) -> Path:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "resolve", boom)

    with pytest.raises(ValueError, match="cannot be resolved"):
        _target("data/x", ws)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bundles/../platform.yaml", "platform.yaml"),
        ("../x", "../x"),
        ("/abs/./a//b", "/abs/a/b"),
        ("//abs/x", "/abs/x"),
        ("./a/", "a/"),
        ("", "."),
    ],
)
def test_root_none_is_lexical(raw: str, expected: str) -> None:
    target = normalise_path_target(raw, root=None)

    assert target.display == expected
    assert target.strict_forms == (expected,)
    assert target.loose_forms == _with_slash_variants([expected])
    assert target.case_insensitive is False
    assert target.root_given is None
    assert target.root_resolved is None


def test_symlinked_root_keeps_both_spellings(ws: Path) -> None:
    alias = ws.parent / "alias"
    alias.symlink_to(ws)
    g, r = alias.as_posix(), ws.as_posix()

    target = _target("data/link", alias)

    assert target.root_given == g
    assert target.root_resolved == r
    assert target.display == "platform.yaml"
    assert target.strict_forms == ("platform.yaml", f"{r}/platform.yaml")
    assert target.loose_forms == _with_slash_variants(
        [
            "platform.yaml",
            f"{r}/platform.yaml",
            f"{g}/platform.yaml",
            "data/link",
            f"{g}/data/link",
            f"{r}/data/link",
        ]
    )
    # An absolute spelling through the resolved root is recognised as
    # inside the workspace lexically too.
    via_r = _target(f"{r}/data/link", alias)
    assert "data/link" in via_r.loose_forms
    # A pattern spelled with the alias still matches the resolved form.
    assert path_pattern_matches(
        f"{g}/bundles/*", _target("bundles/x", alias), restrictive=False
    )


def test_loose_forms_spelled_literally_for_one_row(ws: Path) -> None:
    r = ws.as_posix()

    target = _target("bundles/", ws)

    assert target.strict_forms == ("bundles/", f"{r}/bundles/")
    assert target.loose_forms == (
        "bundles/",
        "bundles",
        f"{r}/bundles/",
        f"{r}/bundles",
    )


@pytest.mark.parametrize(
    ("raw", "pattern"),
    [
        ("data/secret.txt/", "data/secret.txt"),
        ("data/secret/", "data/secret"),
        ("data/secret", "data/secret/"),
    ],
)
def test_trailing_slash_never_escapes_a_deny(ws: Path, raw: str, pattern: str) -> None:
    target = _target(raw, ws)

    assert path_pattern_matches(pattern, target, restrictive=True)
    # Allows keep the D1 spelling exactly: a miss only prompts.
    assert not path_pattern_matches(pattern, target, restrictive=False)


@pytest.mark.parametrize(
    "pattern",
    ["ALIAS*/data/secret/*", "al*/data/secret/*", "ALIAS/data/secret/*"],
)
@pytest.mark.parametrize("spelling", ["rel", "given", "resolved"])
def test_deny_with_glob_in_root_segment_matches_both_root_spellings(
    tmp_path: Path, pattern: str, spelling: str
) -> None:
    real = (tmp_path / "real-ws").resolve()
    (real / "data" / "secret").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    raw = {
        "rel": "data/secret/k",
        "given": f"{alias.as_posix()}/data/secret/k",
        "resolved": f"{real.as_posix()}/data/secret/k",
    }[spelling]
    deny = f"{tmp_path.as_posix()}/{pattern}"

    target = _target(raw, alias)

    assert path_pattern_matches(deny, target, restrictive=True)


def test_allow_with_glob_in_root_segment_stays_strict(tmp_path: Path) -> None:
    real = (tmp_path / "real-ws").resolve()
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    target = _target("data/x", alias)

    # The strict forms are spelled through the resolved root only; an
    # allow globbing the alias's segment under-matches, which only prompts.
    assert not path_pattern_matches(
        f"{tmp_path.as_posix()}/al*/data/*", target, restrictive=False
    )
    assert path_pattern_matches(
        f"{tmp_path.as_posix()}/real*/data/*", target, restrictive=False
    )


_CAFE_NFC = "caf\u00e9"
_CAFE_NFD = "cafe\u0301"


def test_nfd_target_matches_nfc_deny(ws: Path) -> None:
    target = _target(f"{_CAFE_NFD}/x", ws)

    assert path_pattern_matches(f"{_CAFE_NFC}/*", target, restrictive=True)


def test_nfc_target_matches_nfd_deny(ws: Path) -> None:
    target = _target(f"{_CAFE_NFC}/x", ws)

    assert path_pattern_matches(f"{_CAFE_NFD}/*", target, restrictive=True)


@pytest.mark.parametrize("ci", [False, True])
def test_allow_unicode_matching_stays_byte_exact(ws: Path, *, ci: bool) -> None:
    nfd = _target(f"{_CAFE_NFD}/x", ws, ci=ci)
    nfc = _target(f"{_CAFE_NFC}/x", ws, ci=ci)

    assert not path_pattern_matches(f"{_CAFE_NFC}/*", nfd, restrictive=False)
    assert not path_pattern_matches(f"{_CAFE_NFD}/*", nfc, restrictive=False)
    assert path_pattern_matches(f"{_CAFE_NFC}/*", nfc, restrictive=False)


def test_comparable_forms_apply_the_effect_rule(ws: Path) -> None:
    target = _target(f"{_CAFE_NFD.upper()}/x", ws, ci=False)

    assert target.comparable_forms(restrictive=False) == target.strict_forms
    assert f"{_CAFE_NFC}/x" in target.comparable_forms(restrictive=True)


def test_case_flag_is_probed_when_not_injected(ws: Path) -> None:
    target = normalise_path_target("x", root=ws)

    assert target.case_insensitive is is_case_insensitive_fs(ws)


# --- path_pattern_forms -----------------------------------------------------

_G = "/tmp/ws"
_R = "/private/tmp/ws"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*", ("*",)),
        ("bundles/*", ("bundles/*",)),
        ("./data//*", ("data/*",)),
        ("data/", ("data/",)),
        ("../shared/*", ("/tmp/shared/*", "/private/tmp/shared/*")),
        ("/tmp/ws/data/*", ("/tmp/ws/data/*", "/private/tmp/ws/data/*")),
        ("/private/tmp/ws/data/*", ("/private/tmp/ws/data/*", "/tmp/ws/data/*")),
        ("/tmp/ws", ("/tmp/ws", "/private/tmp/ws")),
        ("/abs/a/../b/*", ("/abs/b/*",)),
        ("x/*/../y", ("x/*/../y",)),
        ("a/./b/../c", ("a/c",)),
        ("/..", ("/",)),
        ("/tmp/wsx/*", ("/tmp/wsx/*",)),
        ("../", ("/tmp/", "/private/tmp/")),
        ("..", ("/tmp", "/private/tmp")),
    ],
)
def test_path_pattern_forms(pattern: str, expected: tuple[str, ...]) -> None:
    assert path_pattern_forms(pattern, root_given=_G, root_resolved=_R) == expected


def test_path_pattern_forms_dedupes_when_root_is_not_a_symlink() -> None:
    assert path_pattern_forms("/ws/a/*", root_given="/ws", root_resolved="/ws") == (
        "/ws/a/*",
    )
    assert path_pattern_forms("../s/*", root_given="/ws", root_resolved="/ws") == (
        "/s/*",
    )


def test_root_with_glob_chars_is_escaped_when_spliced() -> None:
    g, r = "/x/w?s", "/y/w*s"

    assert path_pattern_forms("../sh/*", root_given=g, root_resolved=r) == (
        "/x/sh/*",
        "/y/sh/*",
    )
    assert path_pattern_forms("/x/w?s/d/*", root_given=g, root_resolved=r) == (
        "/x/w?s/d/*",
        "/y/w[*]s/d/*",
    )
    assert path_pattern_forms(
        "../../*", root_given="/a/w?s/b", root_resolved="/a/w?s/b"
    ) == ("/a/*",)
    assert path_pattern_forms(
        "../*", root_given="/a/w?s/b", root_resolved="/a/w?s/b"
    ) == ("/a/w[?]s/*",)


def test_escaped_root_twin_matches_only_the_real_root(tmp_path: Path) -> None:
    real = (tmp_path / "w?s").resolve()
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    sibling = tmp_path / "wXs"
    sibling.mkdir()
    deny = f"{alias.as_posix()}/d/*"

    inside = _target("d/k", alias)
    outside = _target(f"{sibling.as_posix()}/d/k", alias)

    assert path_pattern_matches(deny, inside, restrictive=True)
    assert not path_pattern_matches(deny, outside, restrictive=True)
    anchored = _target("../w?s/d/k", alias)
    assert path_pattern_matches("../w?s/d/*", anchored, restrictive=False)


def test_path_pattern_forms_without_root_is_lexical_only() -> None:
    assert path_pattern_forms("../s/*", root_given=None, root_resolved=None) == (
        "../s/*",
    )
    assert path_pattern_forms("./a//b", root_given=None, root_resolved=None) == ("a/b",)


def test_path_pattern_forms_does_no_filesystem_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("path_pattern_forms touched the filesystem")

    monkeypatch.setattr(Path, "resolve", boom)
    monkeypatch.setattr(os, "stat", boom)
    monkeypatch.setattr(os, "lstat", boom)
    monkeypatch.setattr(os.path, "realpath", boom)
    path_pattern_forms.cache_clear()

    assert path_pattern_forms(
        "/tmp/ws/../ws/data/*", root_given=_G, root_resolved=_R
    ) == ("/tmp/ws/data/*", "/private/tmp/ws/data/*")
    assert path_pattern_forms("../x/*", root_given=_G, root_resolved=_R) == (
        "/tmp/x/*",
        "/private/tmp/x/*",
    )


# --- path_pattern_matches ---------------------------------------------------


@pytest.mark.parametrize("restrictive", [False, True])
def test_traversal_out_of_pattern_dir_never_matches(
    ws: Path, *, restrictive: bool
) -> None:
    target = _target("bundles/../platform.yaml", ws)

    assert not path_pattern_matches("bundles/*", target, restrictive=restrictive)


@pytest.mark.parametrize("restrictive", [False, True])
def test_absolute_pattern_matches_relative_spelling(
    ws: Path, *, restrictive: bool
) -> None:
    target = _target("data/x", ws)

    assert path_pattern_matches(f"{ws.as_posix()}/*", target, restrictive=restrictive)


@pytest.mark.parametrize("restrictive", [False, True])
def test_relative_pattern_matches_absolute_spelling(
    ws: Path, *, restrictive: bool
) -> None:
    target = _target(f"{ws.as_posix()}/data/secret/k", ws)

    assert path_pattern_matches("data/secret/*", target, restrictive=restrictive)


def test_symlink_path_matches_loose_only(ws: Path) -> None:
    target = _target("data/link", ws)

    assert not path_pattern_matches("data/*", target, restrictive=False)
    assert path_pattern_matches("data/*", target, restrictive=True)


def test_symlinked_dir_allows_by_real_location(ws: Path) -> None:
    into_bundles = _target("data/dirlink/x", ws)
    out_of_bundles = _target("bundles/blink/x", ws)

    assert path_pattern_matches("bundles/*", into_bundles, restrictive=False)
    assert not path_pattern_matches("bundles/*", out_of_bundles, restrictive=False)
    assert path_pattern_matches("bundles/*", out_of_bundles, restrictive=True)


@pytest.mark.parametrize("ci", [False, True])
def test_case_rule(ws: Path, *, ci: bool) -> None:
    target = _target("BUNDLES/x", ws, ci=ci)

    assert path_pattern_matches("bundles/*", target, restrictive=False) is ci
    assert path_pattern_matches("bundles/*", target, restrictive=True)


def test_dot_dot_after_glob_never_matches(ws: Path) -> None:
    target = _target("x/a/../y", ws)

    assert target.display == "x/y"
    assert not path_pattern_matches("x/*/../y", target, restrictive=False)
    assert not path_pattern_matches("x/*/../y", target, restrictive=True)


def test_leading_dotdot_pattern_is_anchored_to_root(ws: Path) -> None:
    target = _target("../shared/x", ws)

    assert path_pattern_matches("../shared/*", target, restrictive=False)
    assert not path_pattern_matches("shared/*", target, restrictive=True)


def test_root_none_leading_dotdot_never_matches_dir_pattern() -> None:
    target = normalise_path_target("bundles/../../x", root=None)

    assert target.display == "../x"
    assert not path_pattern_matches("bundles/*", target, restrictive=True)


@pytest.mark.parametrize(
    "raw",
    ["platform.yaml", "bundles/../platform.yaml", "/etc/passwd", "DATA/x", ".", "R/x"],
)
@pytest.mark.parametrize("restrictive", [False, True])
def test_star_matches_every_target(ws: Path, raw: str, *, restrictive: bool) -> None:
    target = _target(raw.replace("R", ws.as_posix(), 1) if raw == "R/x" else raw, ws)

    assert path_pattern_matches("*", target, restrictive=restrictive)


def test_trailing_slash_is_kept(ws: Path) -> None:
    assert not path_pattern_matches(
        "bundles", _target("bundles/", ws), restrictive=False
    )
    assert path_pattern_matches("bundles/", _target("bundles/", ws), restrictive=False)
    assert path_pattern_matches("bundles", _target("bundles", ws), restrictive=False)


# --- is_case_insensitive_fs -------------------------------------------------


def test_case_probe_returns_bool_and_is_cached(tmp_path: Path) -> None:
    root = (tmp_path / "Probe").resolve()
    root.mkdir()
    before = is_case_insensitive_fs.cache_info().hits

    first = is_case_insensitive_fs(root)
    second = is_case_insensitive_fs(root)

    assert isinstance(first, bool)
    assert first is second
    assert is_case_insensitive_fs.cache_info().hits > before


def test_case_probe_reports_false_on_error(tmp_path: Path) -> None:
    assert is_case_insensitive_fs(tmp_path / "does-not-exist") is False


# --- package surface --------------------------------------------------------


def test_reexported_from_files_package() -> None:
    assert files.PathTarget is PathTarget
    assert files.normalise_path_target is normalise_path_target
    assert files.path_pattern_forms is path_pattern_forms
    assert files.path_pattern_matches is path_pattern_matches
    assert files.is_case_insensitive_fs is is_case_insensitive_fs

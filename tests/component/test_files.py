"""Component tests for src/files — TC-FIL-01..07.

Spec: docs/explanation/file-handling.md Part 4 +
Module API Reference catalog entries §4.11. The path policy gates
which paths the builtin file-touching tools (and the engine's input
validation) may reach.

These run on a real on-disk layout in ``tmp_path`` so the symlink
test (TC-FIL-05) exercises real ``Path.resolve`` semantics.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from errors import (
    FileAccessDenied,
    FileAccessInvalidFileRef,
    FileAccessPolicyLoadFailed,
)
from files import (
    PROTECTED_WRITE_PATHS,
    PathPolicy,
    assert_writable_destination,
    check_path_allowed,
    is_path_allowed,
    load_policy,
    validate_file_ref,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root(tmp_path: Path) -> Path:
    """Stand up the canonical platform-root layout under tmp_path."""
    for sub in ("bundles", "data", "sessions", "content", "logs", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    # A sample .env (denied by default policy).
    (tmp_path / ".env").write_text("X=y\n", encoding="utf-8")
    return tmp_path


def _default_policy() -> PathPolicy:
    """Spec §4.1 default."""
    return PathPolicy(
        allowed_paths=["bundles/", "data/", "sessions/", "content/", "logs/"],
        denied_paths=[".env", "platform.yaml", ".platform/"],
    )


# ===========================================================================
# TC-FIL-01 — allowed path permitted
# ===========================================================================


def test_allowed_path_permitted(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "data" / "x.txt").write_text("hi", encoding="utf-8")

    allowed, reason = is_path_allowed("data/x.txt", policy=_default_policy(), root=root)
    assert allowed is True
    assert reason is None


def test_allowed_path_works_for_deep_nesting(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "data" / "deep" / "tree").mkdir(parents=True)
    (root / "data" / "deep" / "tree" / "x.txt").write_text("ok", encoding="utf-8")

    allowed, _ = is_path_allowed(
        "data/deep/tree/x.txt",
        policy=_default_policy(),
        root=root,
    )
    assert allowed is True


def test_allowed_absolute_path_permitted(tmp_path: Path) -> None:
    """Absolute paths under an allowed prefix are also permitted."""
    root = _root(tmp_path)
    target = root / "data" / "x.txt"
    target.write_text("hi", encoding="utf-8")
    allowed, _ = is_path_allowed(str(target), policy=_default_policy(), root=root)
    assert allowed is True


# ===========================================================================
# TC-FIL-02 — unmatched path default-denied
# ===========================================================================


def test_unmatched_path_default_denied(tmp_path: Path) -> None:
    root = _root(tmp_path)
    # Use a temp-derived absolute path so the test is portable and
    # doesn't trigger ruff's /tmp hardcode warning.
    elsewhere = tmp_path.parent / "_unrelated_dir" / "thing"
    allowed, reason = is_path_allowed(
        str(elsewhere),
        policy=_default_policy(),
        root=root,
    )
    assert allowed is False
    assert reason is not None
    assert "default deny" in reason.lower()


def test_unmatched_relative_path_default_denied(tmp_path: Path) -> None:
    root = _root(tmp_path)
    # No "other/" in allowed_paths.
    allowed, _ = is_path_allowed(
        "other/foo",
        policy=_default_policy(),
        root=root,
    )
    assert allowed is False


# ===========================================================================
# TC-FIL-03 — deny overrides allow
# ===========================================================================


def test_deny_overrides_allow_when_path_under_allowed_root(
    tmp_path: Path,
) -> None:
    """Spec Fh-I3 — the .env file sits at the platform root which is
    nominally under an allowed prefix (or could be if the user added
    `./` to allowed_paths). Even so, the deny rule wins."""
    root = _root(tmp_path)
    # Policy that allows everything under root.
    policy = PathPolicy(
        allowed_paths=[str(root)],
        denied_paths=[".env"],
    )
    allowed, reason = is_path_allowed(".env", policy=policy, root=root)
    assert allowed is False
    assert reason is not None
    # Matches both "denied by rule '.env'" and "default deny" forms.
    assert "den" in reason.lower()


# ===========================================================================
# TC-FIL-04 — adding .env to allowed does NOT grant
# ===========================================================================


def test_env_in_both_allowed_and_denied_is_still_denied(
    tmp_path: Path,
) -> None:
    """Spec Fh-I3 — denies always win, even on direct collision."""
    root = _root(tmp_path)
    policy = PathPolicy(
        allowed_paths=[".env"],
        denied_paths=[".env"],
    )
    allowed, reason = is_path_allowed(".env", policy=policy, root=root)
    assert allowed is False
    assert reason is not None


# ===========================================================================
# TC-FIL-05 — symlink to denied is denied
# ===========================================================================


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="symlink creation requires elevated privileges on Windows",
)
def test_symlink_to_denied_target_is_denied(tmp_path: Path) -> None:
    """Spec Fh-I4 — paths MUST be normalised via ``Path.resolve`` BEFORE
    policy evaluation. A symlink in an allowed location that resolves
    to a denied target is treated as the denied target."""
    root = _root(tmp_path)
    # The symlink lives under data/ (allowed) but points at .env (denied).
    link = root / "data" / "secret_link"
    link.symlink_to(root / ".env")

    allowed, reason = is_path_allowed(
        "data/secret_link",
        policy=_default_policy(),
        root=root,
    )
    assert allowed is False, "symlink to denied target MUST be denied (Fh-I4)"
    assert reason is not None


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="symlink creation requires elevated privileges on Windows",
)
def test_dotdot_escape_attempt_is_denied(tmp_path: Path) -> None:
    """Spec §4.2 step 1 — `..` segments are resolved before policy
    evaluation; an escape attempt resolves to whatever the parent
    actually is and the default-deny catches it."""
    root = _root(tmp_path)
    allowed, _ = is_path_allowed(
        "data/../../escape",
        policy=_default_policy(),
        root=root,
    )
    assert allowed is False


# ===========================================================================
# TC-FIL-06 — bash bypasses path policy (documented limitation)
# ===========================================================================


def test_bash_bypasses_path_policy_documented_limitation() -> None:
    """Spec §4.7 + Fh-I6 — v1's native sandbox does NOT enforce path
    policy for bash. The runtime behaviour is pinned at the Tools-
    subsystem layer (tests/component/test_tools.py::
    test_builtin_bash_bypasses_path_policy_v1_gap, which actually
    runs ``cat`` on a denied path and verifies it succeeds).

    Here we pin the structural property — the ``bash`` builtin's
    factory MUST NOT reference :func:`check_path_allowed`. If a
    future change wires the policy in, this test forces the
    engineer to flip it deliberately rather than slipping.
    """
    import inspect

    from tools.builtins import make_bash

    source = inspect.getsource(make_bash)
    assert "check_path_allowed" not in source, (
        "bash builtin must NOT enforce path policy in v1 (Fh-I6); "
        "flip this test only when a real sandbox lands."
    )


# ===========================================================================
# TC-FIL-07 — tools never write to content/
# ===========================================================================


def test_writable_destination_rejects_content_directory(tmp_path: Path) -> None:
    """Spec Fh-I5 / Fh-I9 — only the engine writes to content/.
    The write-guard refuses any tool target under content/ regardless
    of the path policy (the default policy actually ALLOWS reads
    under content/; the write-side gate is separate)."""
    root = _root(tmp_path)
    with pytest.raises(FileAccessDenied) as exc_info:
        assert_writable_destination("content/foo.bin", root=root)
    assert "content/" in str(exc_info.value)


def test_writable_destination_rejects_absolute_content_path(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    target = root / "content" / "x.bin"
    with pytest.raises(FileAccessDenied):
        assert_writable_destination(str(target), root=root)


def test_writable_destination_accepts_data_directory(tmp_path: Path) -> None:
    root = _root(tmp_path)
    # Does not raise. Returns None.
    assert_writable_destination("data/reports/x.md", root=root)


# ===========================================================================
# bug-161 — casefolded deny matching + the protected write floor
#
# Filesystem-independent: the policy compares casefolded resolved paths,
# so these hold on case-sensitive Linux (where `.ENV` is a different,
# missing file) and on case-insensitive macOS APFS (where it IS `.env`).
# ===========================================================================


def test_writable_destination_rejects_content_directory_any_case(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    for target in ("CONTENT/x", "Content/sub/x.bin", "data/../CONTENT/x"):
        with pytest.raises(FileAccessDenied):
            assert_writable_destination(target, root=root)


@pytest.mark.parametrize(
    "target",
    [".ENV", ".Env", "data/../.ENV", ".PLATFORM/entities.db"],
)
def test_denied_paths_match_any_case(tmp_path: Path, target: str) -> None:
    """A `.env` deny must not be bypassable by `.ENV` (the macOS read
    bypass: `.` allows the root, and the case-sensitive deny missed)."""
    root = _root(tmp_path)
    policy = PathPolicy(allowed_paths=["."], denied_paths=[".env", ".platform/"])
    allowed, reason = is_path_allowed(target, policy=policy, root=root)
    assert allowed is False
    assert reason is not None
    assert "denied by rule" in reason


def test_user_denied_path_matches_lowercase_target(tmp_path: Path) -> None:
    root = _root(tmp_path)
    policy = PathPolicy(allowed_paths=["."], denied_paths=["Secret/"])
    with pytest.raises(FileAccessDenied) as exc_info:
        check_path_allowed("secret/x", policy=policy, root=root)
    assert exc_info.value.detail["matched_rule"] == {
        "pattern": "Secret/",
        "type": "denied_paths",
    }


def test_allow_never_grants_on_case_alone(tmp_path: Path) -> None:
    """Casefolding widens denies only; an allow entry stays exact."""
    root = _root(tmp_path)
    policy = PathPolicy(allowed_paths=["data/"], denied_paths=[])
    assert is_path_allowed("data/x", policy=policy, root=root) == (
        True,
        None,
    )
    allowed, reason = is_path_allowed("DATA/x", policy=policy, root=root)
    assert allowed is False
    assert reason == "default deny"


def test_protected_write_paths_constant() -> None:
    assert PROTECTED_WRITE_PATHS == ("platform.yaml", ".kolsch/", ".env", ".platform/")


@pytest.mark.parametrize(
    ("target", "entry"),
    [
        ("platform.yaml", "platform.yaml"),
        ("PLATFORM.YAML", "platform.yaml"),
        ("bundles/../platform.yaml", "platform.yaml"),
        ("platform.yaml/.", "platform.yaml"),
        (".kolsch", ".kolsch/"),
        (".kolsch/default-rules.json", ".kolsch/"),
        (".KOLSCH/x", ".kolsch/"),
        (".env", ".env"),
        (".Env", ".env"),
        (".platform/entities.db", ".platform/"),
    ],
)
def test_writable_destination_rejects_protected_floor(
    tmp_path: Path, target: str, entry: str
) -> None:
    """No `file_access` policy is involved: the floor is in the guard."""
    root = _root(tmp_path)
    with pytest.raises(FileAccessDenied) as exc_info:
        assert_writable_destination(target, root=root)
    err = exc_info.value
    assert err.detail["matched_rule"] == {
        "pattern": entry,
        "type": "protected_write_floor",
    }
    assert f"{entry} is protected" in str(err)
    assert "kolsch permissions" in str(err)


def test_writable_destination_rejects_absolute_protected_path(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    with pytest.raises(FileAccessDenied):
        assert_writable_destination(str(root / "platform.yaml"), root=root)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_writable_destination_rejects_symlinks_into_floor(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "platform.yaml").write_text("x: 1\n", encoding="utf-8")
    (root / "data" / "link").symlink_to("../platform.yaml")
    (root / "data" / "up").symlink_to("..", target_is_directory=True)
    for target in ("data/link", "data/up/platform.yaml", "data/up/.kolsch/x"):
        with pytest.raises(FileAccessDenied):
            assert_writable_destination(target, root=root)


@pytest.mark.skipif(sys.platform == "win32", reason="hard links differ on Windows")
def test_writable_destination_rejects_hard_link_to_protected_file(
    tmp_path: Path,
) -> None:
    """An in-place writer (`make_write` / `make_edit`) through a hard
    link would change `platform.yaml`'s bytes; refuse it by inode."""
    root = _root(tmp_path)
    (root / "platform.yaml").write_text("x: 1\n", encoding="utf-8")
    (root / "data" / "hl").hardlink_to(root / "platform.yaml")
    with pytest.raises(FileAccessDenied) as exc_info:
        assert_writable_destination("data/hl", root=root)
    assert exc_info.value.detail["matched_rule"]["pattern"] == "platform.yaml"


@pytest.mark.parametrize(
    "target",
    [
        "data/platform.yaml",
        "platform.yaml.bak",
        "platform.yaml.tmp.123",
        ".envrc",
        ".env.example",
        "data/.kolsch/x",
        ".kolschrc",
        "bundles/user/workflows/w.yaml",
    ],
)
def test_writable_destination_floor_does_not_over_match(
    tmp_path: Path, target: str
) -> None:
    root = _root(tmp_path)
    assert_writable_destination(target, root=root)


# ===========================================================================
# check_path_allowed — raise on denial
# ===========================================================================


def test_check_path_allowed_raises_file_access_denied_on_default_deny(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    target = str(tmp_path.parent / "_unrelated" / "x")
    with pytest.raises(FileAccessDenied) as exc_info:
        check_path_allowed(target, policy=_default_policy(), root=root)
    assert exc_info.value.detail["path"] == target
    assert "default deny" in exc_info.value.detail["reason"].lower()


def test_check_path_allowed_does_not_raise_for_allowed(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    (root / "data" / "x.txt").write_text("ok", encoding="utf-8")
    # No exception.
    check_path_allowed("data/x.txt", policy=_default_policy(), root=root)


def test_check_path_allowed_populates_matched_rule_on_rule_driven_deny(
    tmp_path: Path,
) -> None:
    """Spec §4.11 + A-I5 explainability — a denial that fired because
    of a specific rule MUST carry the structured rule in
    ``detail.matched_rule`` so callers can distinguish "denied by
    .env" from "default deny" without parsing the reason string."""
    root = _root(tmp_path)
    with pytest.raises(FileAccessDenied) as exc_info:
        check_path_allowed(".env", policy=_default_policy(), root=root)
    matched = exc_info.value.detail["matched_rule"]
    assert matched is not None
    assert matched["pattern"] == ".env"
    assert matched["type"] == "denied_paths"


def test_check_path_allowed_matched_rule_is_none_on_default_deny(
    tmp_path: Path,
) -> None:
    """The default-deny path carries `matched_rule=None` — the
    structured signal "no rule fired" rather than "rule X fired."""
    root = _root(tmp_path)
    target = str(tmp_path.parent / "_unrelated" / "x")
    with pytest.raises(FileAccessDenied) as exc_info:
        check_path_allowed(target, policy=_default_policy(), root=root)
    assert exc_info.value.detail["matched_rule"] is None


# ===========================================================================
# validate_file_ref — spec §1
# ===========================================================================


def test_validate_file_ref_accepts_normal_path() -> None:
    validate_file_ref("data/x.md")
    validate_file_ref("/absolute/path/file.txt")


def test_validate_file_ref_rejects_empty() -> None:
    """Handbook hard rule — public methods raise PlatformError
    subclasses only; not raw ValueError."""
    with pytest.raises(FileAccessInvalidFileRef) as exc_info:
        validate_file_ref("")
    assert exc_info.value.detail["value"] == ""
    assert "empty" in exc_info.value.detail["reason"].lower()


def test_validate_file_ref_rejects_null_byte() -> None:
    with pytest.raises(FileAccessInvalidFileRef) as exc_info:
        validate_file_ref("data/x\x00.md")
    assert "null byte" in exc_info.value.detail["reason"].lower()


# ===========================================================================
# load_policy — wraps Pydantic ValidationError as FileAccessPolicyLoadFailed
# ===========================================================================


def test_load_policy_from_default_section() -> None:
    policy = load_policy(
        {
            "allowed_paths": ["data/", "bundles/"],
            "denied_paths": [".env"],
        }
    )
    assert policy.allowed_paths == ["data/", "bundles/"]
    assert policy.denied_paths == [".env"]


def test_load_policy_missing_lists_yields_empty() -> None:
    """A policy section that omits both lists is valid (and trivially
    default-deny for everything)."""
    policy = load_policy({})
    assert policy.allowed_paths == []
    assert policy.denied_paths == []


def test_load_policy_with_bad_shape_raises_policy_load_failed() -> None:
    with pytest.raises(FileAccessPolicyLoadFailed) as exc_info:
        load_policy({"allowed_paths": "not-a-list"})
    assert exc_info.value.detail["config_section"] == "file_access"

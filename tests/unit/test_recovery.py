"""qol-090 — `errors.recovery.recovery_hint` install-mode detection + registry.

Pins the platform-owned recovery-hint helper the LLM sees on
`ToolExecutionFailed` failures. Detection is best-effort; on
ambiguity we emit the three-mode multi-variant hint so the user
always has something to run.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from errors import ToolExecutionFailed
from errors.recovery import _detect_install_mode, recovery_hint

if TYPE_CHECKING:
    import pytest


def _write_kolsch_pyproject(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "kolsch"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )


def test_source_run_mode_returns_cd_uv_sync_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Source-run detection: a pyproject.toml naming kolsch above `sys.argv[0]`."""
    _write_kolsch_pyproject(tmp_path)
    fake_venv_bin = tmp_path / ".venv" / "bin"
    fake_venv_bin.mkdir(parents=True)
    fake_argv0 = fake_venv_bin / "kolsch"
    fake_argv0.touch()

    monkeypatch.setattr("sys.argv", [str(fake_argv0)])
    # Force `_detect_install_mode`'s tool-mode signal to miss so source wins.
    monkeypatch.setattr("sys.executable", str(tmp_path / "python-not-in-tool-path"))

    hint = recovery_hint("BrowseUrlExtraNotInstalled")

    assert hint is not None
    assert hint.startswith(
        f"cd {tmp_path.resolve()} && uv sync --extra browse "
        f"&& uv run playwright install chromium"
    )


def test_tool_install_mode_returns_uv_tool_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tool-install detection: `sys.executable` under `uv/tools/kolsch/`."""
    fake_tool_exec = (
        tmp_path / ".local" / "share" / "uv" / "tools" / "kolsch" / "bin" / "python"
    )
    fake_tool_exec.parent.mkdir(parents=True)
    fake_tool_exec.touch()

    # Force `sys.argv[0]` to not resolve to a kolsch source checkout.
    argv_no_pyproject = tmp_path / "unrelated" / "bin" / "kolsch"
    argv_no_pyproject.parent.mkdir(parents=True)
    argv_no_pyproject.touch()
    monkeypatch.setattr("sys.argv", [str(argv_no_pyproject)])
    monkeypatch.setattr("sys.executable", str(fake_tool_exec))

    hint = recovery_hint("BrowseUrlExtraNotInstalled")

    assert hint == "uv tool install 'kolsch[browse]' && playwright install chromium"


def test_unknown_mode_returns_three_variant_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither source nor tool signal matches → all three modes with headings.

    Better to over-explain than emit the wrong single command — the
    canonical failure mode this qol prevents.
    """
    unrelated_argv0 = tmp_path / "bin" / "kolsch"
    unrelated_argv0.parent.mkdir(parents=True)
    unrelated_argv0.touch()
    monkeypatch.setattr("sys.argv", [str(unrelated_argv0)])
    monkeypatch.setattr("sys.executable", str(tmp_path / "some-python"))

    hint = recovery_hint("BrowseUrlExtraNotInstalled")

    assert hint is not None
    assert "## From source" in hint
    assert "## Tool-installed" in hint
    assert "## pip" in hint
    assert "uv sync --extra browse" in hint
    assert "uv tool install 'kolsch[browse]'" in hint
    assert "pip install 'kolsch[browse]'" in hint


def test_unknown_error_class_returns_none() -> None:
    """A class not in the registry returns None — no crash."""
    assert recovery_hint("NotARegisteredErrorClass") is None
    assert recovery_hint("") is None


def test_recovery_hint_never_raises_on_broken_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection must swallow any error and fall through to unknown-mode."""
    monkeypatch.setattr("sys.argv", ["\x00invalid\x00path\x00"])
    monkeypatch.setattr("sys.executable", "\x00invalid\x00")

    hint = recovery_hint("BrowseUrlExtraNotInstalled")
    # Falls through to the multi-mode hint rather than raising or
    # returning None (None is reserved for unknown error classes).
    assert hint is not None
    assert "## From source" in hint


def test_browser_binary_missing_source_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`BrowserBinaryMissing` hint uses `uv run playwright install` in source mode."""
    _write_kolsch_pyproject(tmp_path)
    fake_argv0 = tmp_path / ".venv" / "bin" / "kolsch"
    fake_argv0.parent.mkdir(parents=True)
    fake_argv0.touch()
    monkeypatch.setattr("sys.argv", [str(fake_argv0)])
    monkeypatch.setattr("sys.executable", str(tmp_path / "not-a-tool-path"))

    hint = recovery_hint("BrowserBinaryMissing")

    assert hint is not None
    assert hint == (f"cd {tmp_path.resolve()} && uv run playwright install chromium")


def test_runtime_dep_missing_registered() -> None:
    """`RuntimeDepMissing` is in the registry (fetch_url's ImportError arm)."""
    # Just assert the class is known — the mode-specific text is
    # covered by other tests' detection paths.
    assert recovery_hint("RuntimeDepMissing") is not None


def test_tool_execution_failed_carries_recovery_hint() -> None:
    """`ToolExecutionFailed(recovery_hint=...)` sets and preserves the attribute."""
    exc = ToolExecutionFailed(
        tool_name="probe",
        error_class="SomeClass",
        recovery_hint="run this to fix",
    )
    assert exc.recovery_hint == "run this to fix"


def test_tool_execution_failed_recovery_hint_defaults_to_none() -> None:
    """Backwards-compat: existing raise sites without the kwarg still work."""
    exc = ToolExecutionFailed(tool_name="probe", error_class="SomeClass")
    assert exc.recovery_hint is None


def test_detect_install_mode_returns_tuple() -> None:
    """Detection returns `(mode, path | None)`; never raises."""
    mode, root = _detect_install_mode()
    assert mode in {"source", "tool", "unknown"}
    if mode == "source":
        assert isinstance(root, Path)
    else:
        assert root is None

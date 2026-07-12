"""Component tests for src/platform_runtime/workspace.py — TC-UX-01..03.

Spec: docs/process/test-catalog.md Part 9.6, Phase 1.2 patch §1.1.

The resolver is pure (modulo `os.environ` and `platformdirs`); these
three cases pin the three branches of the precedence chain. The CLI
gate behavior (TC-UX-04) lives in :mod:`tests.component.test_setup`
because it exercises the dispatcher, not the resolver in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from platform_runtime.workspace import (
    DEFAULT_DIR_NAME,
    KOLSCH_ENV_VAR,
    resolve_workspace,
    workspace_is_initialized,
)

if TYPE_CHECKING:
    import pytest


# ---------------------------------------------------------------------------
# TC-UX-01 — `--root` flag wins
# ---------------------------------------------------------------------------


def test_tc_ux_01_cli_root_returned_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set the env var to confirm the CLI flag takes precedence.
    monkeypatch.setenv(KOLSCH_ENV_VAR, str(tmp_path / "from-env"))
    explicit = tmp_path / "from-flag"
    explicit.mkdir()
    out = resolve_workspace(cli_root=explicit)
    assert out == explicit.resolve()


def test_tc_ux_01_cli_root_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~` in the CLI value expands via `Path.expanduser`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    out = resolve_workspace(cli_root=Path("~/explicit"))
    assert out == (tmp_path / "explicit").resolve()


# ---------------------------------------------------------------------------
# TC-UX-02 — env var wins when no `--root`
# ---------------------------------------------------------------------------


def test_tc_ux_02_env_var_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "from-env"
    target.mkdir()
    monkeypatch.setenv(KOLSCH_ENV_VAR, str(target))
    out = resolve_workspace()
    assert out == target.resolve()


def test_tc_ux_02_env_var_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~` in the env var expands via `Path.expanduser`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(KOLSCH_ENV_VAR, "~/from-env")
    out = resolve_workspace()
    assert out == (tmp_path / "from-env").resolve()


# ---------------------------------------------------------------------------
# TC-UX-03 — platformdirs default when neither is set
# ---------------------------------------------------------------------------


def test_tc_ux_03_default_uses_platformdirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch `platformdirs.user_documents_dir` so the default
    branch is deterministic across machines."""
    monkeypatch.delenv(KOLSCH_ENV_VAR, raising=False)
    mock_docs = tmp_path / "Documents"
    mock_docs.mkdir()
    monkeypatch.setattr(
        "platform_runtime.workspace.platformdirs.user_documents_dir",
        lambda: str(mock_docs),
    )
    out = resolve_workspace()
    assert out == (mock_docs / DEFAULT_DIR_NAME).resolve()


def test_tc_ux_03_empty_env_var_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `KOLSCH_WORKSPACE` is treated as unset (`os.environ.get`
    returns the empty string but our guard checks truthiness)."""
    monkeypatch.setenv(KOLSCH_ENV_VAR, "")
    mock_docs = tmp_path / "Documents"
    mock_docs.mkdir()
    monkeypatch.setattr(
        "platform_runtime.workspace.platformdirs.user_documents_dir",
        lambda: str(mock_docs),
    )
    out = resolve_workspace()
    assert out == (mock_docs / DEFAULT_DIR_NAME).resolve()


# ---------------------------------------------------------------------------
# workspace_is_initialized — the gate the CLI uses
# ---------------------------------------------------------------------------


def test_workspace_is_initialized_false_when_empty(tmp_path: Path) -> None:
    assert workspace_is_initialized(tmp_path) is False


def test_workspace_is_initialized_true_when_platform_yaml_present(
    tmp_path: Path,
) -> None:
    (tmp_path / "platform.yaml").write_text("", encoding="utf-8")
    assert workspace_is_initialized(tmp_path) is True


def test_workspace_is_initialized_false_when_platform_yaml_is_a_dir(
    tmp_path: Path,
) -> None:
    """A directory named `platform.yaml` should NOT count as initialized."""
    (tmp_path / "platform.yaml").mkdir()
    assert workspace_is_initialized(tmp_path) is False

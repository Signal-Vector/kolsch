"""Platform-owned recovery hints for tool errors.

`recovery_hint(error_class, workspace_root=None)` returns the exact
command a user should run to unblock a failed tool call, tailored to
how kolsch is installed in the current process. The LLM sees the hint
verbatim through the `tool.failed` event payload (see
`schemas.events.ToolFailedData.recovery_hint` and the observer arm in
`engine.observers`), and the chat renderer surfaces it inside the
failure capsule. `kolsch doctor` (qol-089) uses the same registry to
report missing extras out-of-band.

Detection is best-effort. On ambiguity we return the multi-mode
variant (source + tool + pip, each under a heading) — over-explaining
beats emitting the wrong single command. Any exception raised during
detection is swallowed and the caller sees `mode="unknown"`.

qol-090.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

InstallMode = Literal["source", "tool", "unknown"]

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def recovery_hint(
    error_class: str,
    workspace_root: Path | None = None,  # noqa: ARG001 — reserved for future hints that need workspace context
) -> str | None:
    """Return the recovery command for `error_class`, or `None` if unknown.

    `workspace_root` is accepted for forward-compat with future hint
    builders that need workspace-scoped context (e.g. a config-file
    edit path). Current hints don't use it — they only need the
    install-mode of the running kolsch process.

    Never raises. On any detection failure the unknown-mode
    (three-variant) hint is returned so the caller always has
    something to show the user.
    """
    builder = _HINTS.get(error_class)
    if builder is None:
        return None
    try:
        mode, kolsch_root = _detect_install_mode()
    except Exception:  # noqa: BLE001 — detection is best-effort
        mode, kolsch_root = "unknown", None
    try:
        return builder(mode, kolsch_root)
    except Exception:  # noqa: BLE001 — hint building must never crash the raise site
        return None


# ---------------------------------------------------------------------------
# Install-mode detection
# ---------------------------------------------------------------------------


def _detect_install_mode() -> tuple[InstallMode, Path | None]:
    """Detect how kolsch is running in this process.

    Precedence:
    1. **source** — walk parents of `sys.argv[0]` for a `pyproject.toml`
       whose `[project].name == "kolsch"`. Match returns the repo root.
    2. **tool** — `sys.executable` under `**/uv/tools/kolsch/**` or
       `**/pipx/venvs/kolsch/**`.
    3. **unknown** — neither signal matches.

    Returns `(mode, kolsch_repo_root)`. Root is populated only for
    `"source"` mode; the source-mode hint needs it to emit the exact
    `cd <root>` prefix. Never raises — callers get `("unknown", None)`
    on any I/O or parse failure.
    """
    argv0 = _safe_path(sys.argv[0] if sys.argv else None)
    if argv0 is not None:
        for parent in _walk_up(argv0):
            root = _read_kolsch_root(parent)
            if root is not None:
                return "source", root

    executable = _safe_path(sys.executable)
    if executable is not None:
        parts = executable.parts
        if _has_subpath(parts, ("uv", "tools", "kolsch")) or _has_subpath(
            parts, ("pipx", "venvs", "kolsch")
        ):
            return "tool", None

    return "unknown", None


def _safe_path(value: str | None) -> Path | None:
    """Resolve `value` to an absolute path; return None on any failure."""
    if not value:
        return None
    try:
        return Path(value).resolve()
    except (OSError, ValueError):
        return None


def _walk_up(start: Path) -> list[Path]:
    """Yield `start` (if directory) or its parent, then each parent up to root."""
    base = start if start.is_dir() else start.parent
    return [base, *base.parents]


def _read_kolsch_root(directory: Path) -> Path | None:
    """Return `directory` if it contains a `pyproject.toml` naming kolsch, else None."""
    pyproject = directory / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = data.get("project", {}).get("name") if isinstance(data, dict) else None
    if name == "kolsch":
        return directory
    return None


def _has_subpath(parts: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """Return True if `needle` appears as a contiguous slice of `parts`."""
    if len(needle) > len(parts):
        return False
    for i in range(len(parts) - len(needle) + 1):
        if parts[i : i + len(needle)] == needle:
            return True
    return False


# ---------------------------------------------------------------------------
# Hint builders — one per registered error class
# ---------------------------------------------------------------------------


def _browse_extra_hint(mode: InstallMode, kolsch_root: Path | None) -> str:
    """Command to install the `kolsch[browse]` extra (Playwright + bs4).

    Source-mode uses `uv sync --extra browse` (the extra is a PEP 621
    optional-dependency, not a PEP 735 dependency-group — `--extra`,
    not `--group`). Tool-mode uses the packaged tool install verb.
    """
    if mode == "source" and kolsch_root is not None:
        return (
            f"cd {kolsch_root} && uv sync --extra browse "
            f"&& uv run playwright install chromium"
        )
    if mode == "tool":
        return "uv tool install 'kolsch[browse]' && playwright install chromium"
    return _multi_mode(
        source=(
            "cd <kolsch source checkout> && uv sync --extra browse "
            "&& uv run playwright install chromium"
        ),
        tool="uv tool install 'kolsch[browse]' && playwright install chromium",
        pip="pip install 'kolsch[browse]' && playwright install chromium",
    )


def _browser_binary_hint(mode: InstallMode, kolsch_root: Path | None) -> str:
    """Command to install the Chromium binary Playwright drives.

    The Python `playwright` package is separate from the browser
    binary it drives — the two-step install is intentional (docs at
    docs/how-to/workspace-and-setup.md).
    """
    if mode == "source" and kolsch_root is not None:
        return f"cd {kolsch_root} && uv run playwright install chromium"
    if mode == "tool":
        return "playwright install chromium"
    return _multi_mode(
        source="cd <kolsch source checkout> && uv run playwright install chromium",
        tool="playwright install chromium",
        pip="playwright install chromium",
    )


def _runtime_dep_missing_hint(mode: InstallMode, kolsch_root: Path | None) -> str:
    """Command to re-sync the base install when a core dep is missing.

    `httpx` / `trafilatura` are declared as core kolsch deps; a
    missing-import here means a broken venv, not a missing extra.
    Recovery is a plain re-sync, not an extra install.
    """
    if mode == "source" and kolsch_root is not None:
        return f"cd {kolsch_root} && uv sync"
    if mode == "tool":
        return "uv tool install --reinstall kolsch"
    return _multi_mode(
        source="cd <kolsch source checkout> && uv sync",
        tool="uv tool install --reinstall kolsch",
        pip="pip install --force-reinstall kolsch",
    )


def _multi_mode(*, source: str, tool: str, pip: str) -> str:
    """Format the three-variant hint used when install mode is unknown."""
    return (
        "Install mode could not be auto-detected. Run whichever fits:\n"
        "\n"
        "## From source\n"
        f"{source}\n"
        "\n"
        "## Tool-installed\n"
        f"{tool}\n"
        "\n"
        "## pip\n"
        f"{pip}"
    )


# ---------------------------------------------------------------------------
# Registry — keyed by `error_class` string (matches ToolExecutionFailed.detail)
# ---------------------------------------------------------------------------

_HINTS: dict[str, Callable[[InstallMode, Path | None], str]] = {
    "BrowseUrlExtraNotInstalled": _browse_extra_hint,
    "BrowserBinaryMissing": _browser_binary_hint,
    "RuntimeDepMissing": _runtime_dep_missing_hint,
}

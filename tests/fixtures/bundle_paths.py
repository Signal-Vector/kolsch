"""Bundle paths + tool loading for tests (bug-153).

The single place tests resolve bundle directories from. Two trees
exist in the repo and they must never overlap:

- **The starter bundle** — ``src/cli/_starter_bundle/kolsch/``. The
  only copy of every platform-shipped tool; it ships in the wheel and
  ``kolsch setup`` copies it to ``<workspace>/bundles/kolsch/``.
  [`STARTER_BUNDLE_DIR`][] is derived from
  ``cli._setup._STARTER_BUNDLE_TEMPLATE_DIR`` so tests load the exact
  directory ``kolsch setup`` copies.
- **The card-examples bundle** — the feat-009 tutorial tools
  (``hello_card``, ``download_with_progress``,
  ``confirm_dangerous_action``). Never shipped.

Tests must not build their own repo-root ``bundles/`` path: a
repo-root copy of a shipped tool is a fork users never run.

Bundle tools are not on the Python import path, so
[`load_bundle_tool`][] loads them through
[`discover_tools_in_file`][tools.discovery.discover_tools_in_file] —
the same discovery API the Tools subsystem uses at registration time.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli._setup import _STARTER_BUNDLE_TEMPLATE_DIR
from tools.decorator import get_tool_definition
from tools.discovery import discover_tools_in_file

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_REPO_ROOT_DIR = Path(__file__).resolve().parents[2]

STARTER_BUNDLE_DIR: Path = _STARTER_BUNDLE_TEMPLATE_DIR
"""The starter ``kolsch`` bundle — the directory ``kolsch setup`` copies."""

STARTER_TOOLS_DIR: Path = STARTER_BUNDLE_DIR / "tools"
"""The shipped platform tools (``run_workflow.py``, ``bash.py``, …)."""

CARD_EXAMPLES_TOOLS_DIR: Path = _REPO_ROOT_DIR / "bundles" / "kolsch" / "tools"
"""The feat-009 tutorial example tools (not shipped)."""


def load_bundle_tool(
    tools_dir: Path,
    filename: str,
    tool_name: str,
) -> Callable[..., Awaitable[Any]]:
    """Import ``<tools_dir>/<filename>`` and return one of its tools.

    Args:
        tools_dir: The bundle ``tools/`` directory, usually
            [`STARTER_TOOLS_DIR`][] or [`CARD_EXAMPLES_TOOLS_DIR`][].
        filename: File under ``tools_dir`` (e.g. ``"run_workflow.py"``).
        tool_name: The ``@tool(name=...)`` value to select — a single
            file may declare multiple tools.

    Returns:
        The ``@tool``-decorated callable, unmodified. Its
        ``_tool_definition`` attribute carries the ``ToolDefinition``
        metadata the dispatch pipeline reads.

    Raises:
        LookupError: ``filename`` declares no tool named ``tool_name``.
    """
    path = tools_dir / filename
    for fn in discover_tools_in_file(path):
        if get_tool_definition(fn).name == tool_name:
            return fn
    msg = f"tool {tool_name!r} not found in {path}"
    raise LookupError(msg)

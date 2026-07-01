"""Unit tests for the ``browse_url`` tool — Test 1 in
process/qol-browse-url-tool.md.

The tool file at ``src/cli/_starter_bundle/kolsch/tools/browse_url.py``
is a filesystem template (no ``__init__.py`` up the tree) and is
loaded here via ``importlib.util`` — the same pattern
``make_browse_url`` in ``src/tools/builtins.py`` uses at runtime.

Test 1 exercises argument-validation surfaces:
- non-http(s) URL → ``ToolExecutionFailed(error_class="InvalidUrl")``
  raised by the tool body (Pydantic passes anything shaped like a
  string through, so scheme-checking is the tool's job).
- ``request_timeout_seconds`` out of the ``ge=1, le=120`` bounds →
  Pydantic ``ValidationError`` at model construction (before the
  coroutine sees anything).
- ``format`` outside the Literal set → Pydantic ``ValidationError``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from errors import ToolExecutionFailed
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext


def _load_browse_url_module():  # type: ignore[no-untyped-def]
    """Load the starter-bundle template as an importable module."""
    module_name = "_test_kolsch_starter_bundle_browse_url"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    repo_root = Path(__file__).resolve().parents[2]
    tool_path = (
        repo_root
        / "src"
        / "cli"
        / "_starter_bundle"
        / "kolsch"
        / "tools"
        / "browse_url.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, tool_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_browse_url_module()
BrowseUrlArgs = _MODULE.BrowseUrlArgs
browse_url = _MODULE.browse_url


def _ctx() -> ToolContext:
    agent_id = "ag_test"
    return ToolContext(
        agent_id=AgentId(agent_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal=f"agent.{agent_id}",
        secrets={},
    )


# ---------------------------------------------------------------------------
# Test 1: argument validation
# ---------------------------------------------------------------------------


def test_browse_url_rejects_non_url_string() -> None:
    """`url="not-a-url"` → InvalidUrl (raised inside the tool body)."""
    args = BrowseUrlArgs(url="not-a-url")
    with pytest.raises(ToolExecutionFailed) as excinfo:
        asyncio.run(browse_url(args, _ctx()))
    assert excinfo.value.detail["error_class"] == "InvalidUrl"


def test_browse_url_rejects_ftp_scheme() -> None:
    """`url="ftp://x"` → InvalidUrl (non-http/https scheme)."""
    args = BrowseUrlArgs(url="ftp://x.example")
    with pytest.raises(ToolExecutionFailed) as excinfo:
        asyncio.run(browse_url(args, _ctx()))
    assert excinfo.value.detail["error_class"] == "InvalidUrl"


def test_browse_url_request_timeout_zero_raises_validation_error() -> None:
    """`request_timeout_seconds=0` violates the `ge=1` bound."""
    with pytest.raises(ValidationError):
        BrowseUrlArgs(url="https://example.com", request_timeout_seconds=0)


def test_browse_url_request_timeout_over_max_raises_validation_error() -> None:
    """`request_timeout_seconds=121` violates the `le=120` bound.

    Included alongside the ge=1 case so the bounded interval is
    locked at both ends — future refactoring can't quietly widen
    the range past the spec's 120-second cap.
    """
    with pytest.raises(ValidationError):
        BrowseUrlArgs(url="https://example.com", request_timeout_seconds=121)


def test_browse_url_bad_format_literal_raises_validation_error() -> None:
    """`format="markdown"` is outside the Literal["text","html"]."""
    with pytest.raises(ValidationError):
        BrowseUrlArgs(url="https://example.com", format="markdown")

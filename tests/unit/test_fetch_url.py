"""Unit tests for the ``fetch_url`` tool — Tests 1 and 12 in
process/qol-fetch-url-tool.md.

The tool file at ``src/cli/_starter_bundle/kolsch/tools/fetch_url.py``
is a filesystem template (no ``__init__.py`` up the tree) and is
loaded here via ``importlib.util`` — the same pattern
``make_fetch_url`` in ``src/tools/builtins.py`` uses at runtime.

Test 1 exercises argument-validation surfaces:
- non-http(s) URL → ``ToolExecutionFailed(error_class="InvalidUrl")``
  raised by the tool body (Pydantic passes anything shaped like a
  string through, so scheme-checking is the tool's job).
- ``request_timeout_seconds`` out of the ``ge=1, le=120`` bounds →
  Pydantic ``ValidationError`` at model construction (before the
  coroutine sees anything).
- ``format`` outside the Literal set → Pydantic ``ValidationError``.

Test 12 exercises the ``NetworkError`` wrapping:
- ``httpx.ConnectError`` at the AsyncClient layer becomes
  ``ToolExecutionFailed(error_class="NetworkError")``. Monkeypatched
  at ``httpx.AsyncClient.request`` so the wrapping is proven on the
  real error path (patching the tool's own body would not).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    from types import ModuleType

from errors import ToolExecutionFailed
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext


def _load_fetch_url_module() -> ModuleType:
    """Load the starter-bundle template as an importable module."""
    module_name = "_test_kolsch_starter_bundle_fetch_url"
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
        / "fetch_url.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, tool_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_fetch_url_module()
FetchUrlArgs = _MODULE.FetchUrlArgs
fetch_url = _MODULE.fetch_url


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


def test_fetch_url_rejects_non_url_string() -> None:
    """`url="not-a-url"` → InvalidUrl (raised inside the tool body)."""
    args = FetchUrlArgs(url="not-a-url")
    with pytest.raises(ToolExecutionFailed) as excinfo:
        asyncio.run(fetch_url(args, _ctx()))
    assert excinfo.value.detail["error_class"] == "InvalidUrl"


def test_fetch_url_rejects_ftp_scheme() -> None:
    """`url="ftp://x"` → InvalidUrl (non-http/https scheme)."""
    args = FetchUrlArgs(url="ftp://x.example")
    with pytest.raises(ToolExecutionFailed) as excinfo:
        asyncio.run(fetch_url(args, _ctx()))
    assert excinfo.value.detail["error_class"] == "InvalidUrl"


def test_fetch_url_request_timeout_zero_raises_validation_error() -> None:
    """`request_timeout_seconds=0` violates the `ge=1` bound."""
    with pytest.raises(ValidationError):
        FetchUrlArgs(url="https://example.com", request_timeout_seconds=0)


def test_fetch_url_request_timeout_over_max_raises_validation_error() -> None:
    """`request_timeout_seconds=121` violates the `le=120` bound.

    Included alongside the ge=1 case so the bounded interval is
    locked at both ends — future refactoring can't quietly widen
    the range past the spec's 120-second cap.
    """
    with pytest.raises(ValidationError):
        FetchUrlArgs(url="https://example.com", request_timeout_seconds=121)


def test_fetch_url_bad_format_literal_raises_validation_error() -> None:
    """`format="markdown"` is outside the Literal["text","html","bytes_base64"]."""
    with pytest.raises(ValidationError):
        FetchUrlArgs(url="https://example.com", format="markdown")


# ---------------------------------------------------------------------------
# Test 12: httpx.ConnectError → ToolExecutionFailed(error_class="NetworkError")
# ---------------------------------------------------------------------------


def test_fetch_url_network_error_wraps_as_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 12: patch ``httpx.AsyncClient.request`` → ConnectError → NetworkError.

    Patching at the AsyncClient layer — not at the tool's body — is
    the whole point: it proves the ``except httpx.HTTPError`` branch
    fires on the real error path, and that the tool's wrapping logic
    isn't a no-op. If a future refactor moves the ``except`` clause
    or renames the ``error_class`` string, this test flags it.
    """
    import httpx

    async def _boom(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("dns fail")

    monkeypatch.setattr(httpx.AsyncClient, "request", _boom)

    args = FetchUrlArgs(url="https://example.com")
    with pytest.raises(ToolExecutionFailed) as excinfo:
        asyncio.run(fetch_url(args, _ctx()))

    assert excinfo.value.detail["tool_name"] == "fetch_url"
    assert excinfo.value.detail["error_class"] == "NetworkError"
    # The original ConnectError message must survive the wrapping so
    # the caller / diagnostics chain can see what actually went wrong.
    # __cause__ carries the ``from exc`` chain.
    cause = excinfo.value.__cause__
    assert isinstance(cause, httpx.ConnectError)
    assert "dns fail" in str(cause)

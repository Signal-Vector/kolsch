"""Unit tests for the ``browse_url`` tool — Tests 1 and 7 in
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

Test 7 exercises the missing-browser-binary path:
- Patch ``BrowserType.launch`` to raise ``PlaywrightError`` with the
  "Executable doesn't exist" phrase Playwright itself uses.
- Assert the tool raises ``ToolExecutionFailed`` immediately with
  ``error_class="BrowserBinaryMissing"``, the underlying
  ``playwright install chromium`` hint survives on ``__cause__``, and
  no subprocess is spawned by the tool (load-bearing: the spec
  forbids in-process auto-install of Chromium).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from errors import ToolExecutionFailed
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext


def _load_browse_url_module():
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


# ---------------------------------------------------------------------------
# Test 7: missing browser binary path — Playwright's "Executable doesn't
# exist" surfaces as BrowserBinaryMissing with the install hint
# preserved, AND the tool never spawns a subprocess (no auto-install).
# ---------------------------------------------------------------------------


# The exact phrasing Playwright emits when Chromium hasn't been fetched
# via ``playwright install chromium`` — pin the ``Executable doesn't
# exist`` prefix (the substring the tool matches on) and the
# ``playwright install chromium`` hint (the guidance the user needs)
# so a future refactor that drops either signal breaks the test.
_MISSING_BINARY_MESSAGE = (
    "Executable doesn't exist at /root/.cache/ms-playwright/"
    "chromium_headless_shell-1181/chrome-linux/headless_shell\n"
    "Looks like Playwright was just installed or updated. "
    "Please run the following command to download new browsers: "
    "playwright install chromium"
)


def test_browse_url_missing_binary_surfaces_as_browser_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 7: missing Chromium binary → BrowserBinaryMissing, no auto-install.

    Patches ``BrowserType.launch`` (the concrete ``p.chromium.launch``
    target the tool awaits) to raise Playwright's own ``Error`` with
    the ``Executable doesn't exist`` phrase. The tool must:

    - Raise ``ToolExecutionFailed`` with
      ``error_class="BrowserBinaryMissing"`` immediately.
    - Preserve the underlying install hint on ``__cause__`` so the
      chat surface's error envelope still shows the user how to fix
      it (``playwright install chromium``).
    - NEVER spawn a subprocess. This is the load-bearing guard against
      a future dev re-adding an in-process ``playwright install
      chromium`` — the spec is explicit that mid-chat 150 MB downloads
      are user-hostile.
    """
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api._generated import BrowserType

    async def _boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise PlaywrightError(_MISSING_BINARY_MESSAGE)

    monkeypatch.setattr(BrowserType, "launch", _boom)

    # Load-bearing: any subprocess spawn from inside the tool would
    # mean auto-install crept back in. Wrap both the sync-Popen and
    # asyncio.create_subprocess_exec entry points so either shape gets
    # flagged. Fail the test if the wrapped function is called at all.
    subprocess_calls: list[tuple[str, tuple[Any, ...]]] = []

    import asyncio as _asyncio
    import subprocess as _subprocess

    real_popen = _subprocess.Popen
    real_create_exec = _asyncio.create_subprocess_exec
    real_create_shell = _asyncio.create_subprocess_shell

    def _tracked_popen(*args: Any, **kwargs: Any) -> Any:
        subprocess_calls.append(("Popen", args))
        return real_popen(*args, **kwargs)

    async def _tracked_create_exec(*args: Any, **kwargs: Any) -> Any:
        subprocess_calls.append(("create_subprocess_exec", args))
        return await real_create_exec(*args, **kwargs)

    async def _tracked_create_shell(*args: Any, **kwargs: Any) -> Any:
        subprocess_calls.append(("create_subprocess_shell", args))
        return await real_create_shell(*args, **kwargs)

    monkeypatch.setattr(_subprocess, "Popen", _tracked_popen)
    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _tracked_create_exec)
    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _tracked_create_shell)

    args = BrowseUrlArgs(url="https://example.com")
    with pytest.raises(ToolExecutionFailed) as excinfo:
        asyncio.run(browse_url(args, _ctx()))

    detail = excinfo.value.detail
    assert detail["tool_name"] == "browse_url"
    assert detail["error_class"] == "BrowserBinaryMissing"

    # The install hint the user actually needs must survive the
    # wrapping so the diagnostic chain shows how to fix it. The
    # ``from exc`` chain in the tool body carries the original
    # PlaywrightError as ``__cause__``.
    cause = excinfo.value.__cause__
    assert isinstance(cause, PlaywrightError)
    assert "playwright install chromium" in str(cause)

    # No install-shaped subprocess must have been spawned by the tool.
    # This is the load-bearing guard against a future dev re-adding
    # auto-install of Chromium at first tool call — the spec forbids
    # mid-chat 150 MB downloads.
    #
    # We can't assert ``subprocess_calls == []`` outright because
    # Playwright itself spawns its Node.js driver process on
    # ``async_playwright()`` entry (that spawn is internal to the
    # library, not the tool). Instead, scan every recorded call for
    # the specific install command shape — a ``playwright install``
    # invocation (either the ``playwright`` CLI directly, or
    # ``python -m playwright install`` / ``uv run playwright
    # install``). If any such call ever appears here, auto-install
    # crept back in — revert it.
    def _looks_like_install(call: tuple[str, tuple[Any, ...]]) -> bool:
        _kind, argv = call
        # argv is either a positional tuple or nested tuple (Popen).
        # Flatten one level and stringify for a robust substring check.
        flat: list[str] = []
        for a in argv:
            if isinstance(a, (list, tuple)):
                flat.extend(str(x) for x in a)
            else:
                flat.append(str(a))
        joined = " ".join(flat)
        return "playwright" in joined and "install" in joined

    install_calls = [c for c in subprocess_calls if _looks_like_install(c)]
    assert install_calls == [], (
        "browse_url must not spawn a `playwright install` subprocess "
        "on missing-binary (auto-install is out of scope by design); "
        f"observed install-shaped calls: {install_calls}"
    )


def test_browse_url_missing_binary_matches_install_hint_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widened match: ``playwright install`` alone must also surface as BrowserBinaryMissing.

    Guards against Playwright rewording the "Executable doesn't exist"
    prefix upstream. As long as the install hint survives in the error
    text, users continue to see an actionable BrowserBinaryMissing
    rather than a confusing NavigationError.
    """
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api._generated import BrowserType

    hint_only_message = (
        "Browser binary not available. Please run: playwright install chromium"
    )

    async def _boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise PlaywrightError(hint_only_message)

    monkeypatch.setattr(BrowserType, "launch", _boom)

    args = BrowseUrlArgs(url="https://example.com")
    with pytest.raises(ToolExecutionFailed) as excinfo:
        asyncio.run(browse_url(args, _ctx()))

    assert excinfo.value.detail["error_class"] == "BrowserBinaryMissing"

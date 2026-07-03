"""Component tests for the ``browse_url`` tool — Tests 2-6 from
process/qol-browse-url-tool.md (core happy / html / overflow / timeout
/ cancel).

Each test spins up a local ``http.server.ThreadingHTTPServer`` on an
ephemeral port. A per-test route table (a plain ``dict[str, tuple]``)
decides how the fixture responds. No test touches a real network — the
whole surface exercises ``127.0.0.1``. The fixture pattern mirrors
``tests/component/test_fetch_url.py`` deliberately so a future refactor
can lift the shared bits into a helper module without either file
diverging first.

This file loads the starter-bundle template at
``src/cli/_starter_bundle/kolsch/tools/browse_url.py`` via
``importlib.util`` — the same pattern the sibling
``tests/unit/test_browse_url.py`` uses. The template lives outside any
importable package (no ``__init__.py`` up the tree) so a plain import
from the tests package cannot find it.

Tests covered here (from ``process/qol-browse-url-tool.md``):
- Test 2 — happy-path fetch → ``content`` includes ``hello`` and the
  return shape is populated correctly.
- Test 3 — ``format='html'`` captures the DOM AFTER the inline
  ``<script>`` mutated ``document.body`` — proves JS ran before
  capture, not before load.
- Test 4 — extracted text > 64 KiB persists to
  ``<workspace>/logs/tool-outputs/<session>/…`` and the inline
  ``content`` is a truncated preview.
- Test 5 — a never-completing route + ``request_timeout_seconds=2``
  raises ``ToolTimeout`` AND leaves no Chromium PID behind (probed
  via ``psutil``, NOT ``browser.contexts`` — the spec explicitly
  warns that ``browser.contexts`` is trivially empty after
  ``browser.close()`` and doesn't prove anything).
- Test 6 — ``ctx.cancellation.cancel()`` mid-fetch → the tool raises
  ``ExchangeCancelled`` within one heartbeat and no page leaks.
- Test 9 — ``/a`` responds ``302 → /b``; ``final_url`` ends with
  ``/b`` and ``content`` reflects the redirect target body.
- Test 10 — ``wait_selector`` exercises ``page.wait_for_selector``'s
  own timeout (distinct code path from Test 5's ``goto`` timeout);
  the tool raises ``ToolTimeout`` when the selector never appears.
- Test 11 — non-2xx (404) status is surfaced verbatim without
  raising — authors branch on ``status`` downstream.
- Test 12 — caller-supplied ``headers`` (custom ``X-Test`` and
  ``User-Agent``) survive to the fixture, proving the extra-header
  merge order is caller-wins.
- Test 13 — caller-supplied ``cookies`` reach the fixture, proving
  ``context.add_cookies`` fires BEFORE navigation and the
  domain-from-``args.url`` derivation is correct.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psutil
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from engine.context import CancelToken
from errors import ExchangeCancelled, ToolTimeout
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext

pytestmark = pytest.mark.slow


def _load_browse_url_module():
    """Load the starter-bundle template as an importable module.

    Shares the module-name key with ``tests/unit/test_browse_url.py``
    so the two files exercise the same module object when the full
    suite runs (matters for stateful patches — none today, but the
    unit-test file uses the same key for consistency).
    """
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


# ---------------------------------------------------------------------------
# ToolContext helpers
# ---------------------------------------------------------------------------


def _ctx() -> ToolContext:
    """Bare ToolContext with no ``cancellation`` (Test 5's path)."""
    agent_id = "ag_test"
    return ToolContext(
        agent_id=AgentId(agent_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal=f"agent.{agent_id}",
        secrets={},
    )


def _ctx_with_workspace(workspace_root: Path) -> ToolContext:
    """ToolContext pointed at ``workspace_root``.

    Overflow persistence writes under
    ``<workspace_root>/logs/tool-outputs/<session_id>/<call_id>.content.txt``,
    so the persistence branch only fires when ``ctx.workspace_root`` is
    set. Fresh session id per call keeps the disk layout deterministic
    for Test 4's assertions.
    """
    agent_id = "ag_test_browse_overflow"
    return ToolContext(
        agent_id=AgentId(agent_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal=f"agent.{agent_id}",
        secrets={},
        workspace_root=workspace_root,
    )


class _CtxWithCancel:
    """Minimal duck-typed ``ToolContext`` carrying a ``cancellation`` token.

    :class:`ToolContext` is a frozen Pydantic model with
    ``extra='forbid'`` so a real ``cancellation`` field can't be
    attached from outside the schema. ``browse_url`` reads the token
    via ``getattr(ctx, 'cancellation', None)`` — deliberately
    duck-typed precisely so tests (this file) and future call sites
    can inject a token without reshaping the ToolContext schema. This
    shim exposes the same attribute surface plus ``cancellation``.
    Mirrors ``test_fetch_url.py::_CtxWithCancel`` — the two exist so
    each file can be read in isolation without cross-file coupling.
    """

    def __init__(
        self,
        *,
        base: ToolContext,
        cancellation: CancelToken | None,
    ) -> None:
        self.agent_id = base.agent_id
        self.session_id = base.session_id
        self.thread_id = base.thread_id
        self.principal = base.principal
        self.secrets = base.secrets
        self.workspace_root = base.workspace_root
        self.path_policy = base.path_policy
        self.cancellation = cancellation


# ---------------------------------------------------------------------------
# Local HTTP fixture — same route-table shape as test_fetch_url.py
# ---------------------------------------------------------------------------


# Route table shape:
#   path -> (status, headers, body)  # static response
#     OR
#   path -> callable(BaseHTTPRequestHandler) -> (status, headers, body)
Route = tuple[int, dict[str, str], bytes]
RouteHandler = Callable[[BaseHTTPRequestHandler], Route]
RouteTable = dict[str, "Route | RouteHandler"]


class _LocalHttpServer:
    """Wrap a ``ThreadingHTTPServer`` with a per-test route table.

    Same shape as ``test_fetch_url.py::_LocalHttpServer``. Kept as a
    local class so this test file can be read on its own — the two
    fixtures will be lifted into a shared helper the next time one of
    them needs a new capability.
    """

    def __init__(self) -> None:
        self.routes: RouteTable = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None, "server not started"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        routes = self.routes

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:
                entry = routes.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", "9")
                    self.end_headers()
                    self.wfile.write(b"not-found")
                    return
                resolved = entry(self) if callable(entry) else entry
                status, headers, body = resolved
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="browse-url-fixture"
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None


@pytest.fixture
def local_http_server() -> Iterator[_LocalHttpServer]:
    """Fresh local HTTP server per test."""
    server = _LocalHttpServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Chromium PID leak probe (Test 5 / Test 6)
# ---------------------------------------------------------------------------


def _chromium_pids() -> set[int]:
    """Return the PIDs of every currently running Chromium process.

    Used by Test 5 and Test 6 as the AFTER-call leak probe. The spec
    explicitly forbids using ``browser.contexts`` because that check
    is trivially empty after ``browser.close()`` and doesn't prove
    the OS process actually terminated — a leaked Chromium survives
    ``browser.close()`` if the tool forgot to close on the error
    path. Playwright's chromium ships under the name
    ``chrome_crashpad_handler`` too; match any executable containing
    ``chrom`` (Chrome, Chromium, chrome_headless_shell, etc.) so the
    probe catches every flavor Playwright might launch.
    """
    pids: set[int] = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "chrom" in name:
            pids.add(proc.info["pid"])
    return pids


async def _wait_for_pids_to_drain(
    before: set[int],
    *,
    timeout_seconds: float = 5.0,
) -> set[int]:
    """Return any Chromium PIDs that appeared and outlived the call.

    Playwright's ``browser.close()`` returns before the OS has fully
    reaped every subprocess Chromium spawned — there's a brief window
    where the parent has exited but the crash-handler child is still
    winding down. Poll for up to ``timeout_seconds`` for the new PIDs
    to drop off ``psutil.process_iter`` before failing the leak
    assertion so a slow OS scheduler doesn't produce flakes.
    """
    deadline = time.monotonic() + timeout_seconds
    leaked: set[int] = set()
    while time.monotonic() < deadline:
        current = _chromium_pids()
        leaked = current - before
        if not leaked:
            return leaked
        # Give the OS a slice to reap Chromium's children.
        await asyncio.sleep(0.1)
    return leaked


# ---------------------------------------------------------------------------
# Test 2 — happy path
# ---------------------------------------------------------------------------


# Deliberately tiny — the point is to prove the fetch renders and the
# return shape is populated, not to exercise big-DOM parsing.
_HAPPY_HTML = b"""<!doctype html>
<html>
  <head><title>t</title></head>
  <body>
    <h1>hello</h1>
    <p>rendered via headless chromium</p>
  </body>
</html>
"""


async def test_browse_url_happy_path_renders_and_populates_shape(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 2: format='text' → content has 'hello', shape is populated."""
    local_http_server.routes["/hello"] = (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        _HAPPY_HTML,
    )
    url = f"{local_http_server.base_url}/hello"
    args = BrowseUrlArgs(url=url)

    result = await browse_url(args, _ctx())

    assert result["status"] == 200
    assert result["format"] == "text"
    # `content` is title + "\n\n" + body.innerText; both must appear.
    assert "hello" in result["content"]
    # Playwright normalizes the final URL — a trailing slash may or
    # may not appear depending on the path. The `/hello` route was
    # requested exactly and no redirect fixture is wired, so the
    # final URL should still end in `/hello`.
    assert result["final_url"].endswith("/hello")
    assert result["url"] == url
    assert result["truncated"] is False
    assert result["content_persisted_to"] is None
    assert result["content_length"] > 0
    assert result["duration_seconds"] >= 0.0


# ---------------------------------------------------------------------------
# Test 3 — HTML mode captures post-JS DOM
# ---------------------------------------------------------------------------


# Inline <script> mutates the DOM at parse time. `format='html'`
# returns `page.content()` AFTER the load event, so the mutation
# must be visible in the returned HTML — proving JS ran before
# capture, not just that JS ran at all.
_POST_JS_HTML = b"""<!doctype html>
<html>
  <head><title>post-js</title></head>
  <body>
    <h1>hello</h1>
    <script>document.body.dataset.k = "ok";</script>
  </body>
</html>
"""


async def test_browse_url_html_mode_captures_post_js_dom(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 3: format='html' → captured DOM shows JS-set data-k='ok'."""
    local_http_server.routes["/spa"] = (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        _POST_JS_HTML,
    )
    args = BrowseUrlArgs(
        url=f"{local_http_server.base_url}/spa",
        format="html",
    )

    result = await browse_url(args, _ctx())

    assert result["status"] == 200
    assert result["format"] == "html"
    # `data-k="ok"` is the load-bearing assertion: the attribute is
    # NOT in the served HTML — Playwright must have captured the DOM
    # after the inline script ran. If capture happened before load,
    # this attribute would be missing.
    assert 'data-k="ok"' in result["content"]


# ---------------------------------------------------------------------------
# Test 4 — overflow persistence
# ---------------------------------------------------------------------------


# One paragraph large enough that a couple hundred repetitions blow
# past 64 KiB of extracted TEXT (Test 4 uses format='text' so this
# also has to survive body.innerText's whitespace handling).
_BIG_PARAGRAPH = (
    "This is a long paragraph that the text-mode formatter will "
    "concatenate into body.innerText. It contains no navigation, no "
    "script tags, and no chrome — just enough sentences to survive "
    "Playwright's inner_text extraction. Each copy adds a few hundred "
    "bytes so a couple hundred copies push the output well past the "
    "64 KiB inline cap. Sentinel token: OVERFLOW_MARKER_BROWSE_TEST_4."
)


def _large_page_html(paragraph_repeats: int) -> bytes:
    body = "\n".join(f"<p>{_BIG_PARAGRAPH}</p>" for _ in range(paragraph_repeats))
    return (
        "<!doctype html><html><head><title>overflow</title></head>"
        "<body>"
        f"{body}"
        "</body></html>"
    ).encode()


async def test_browse_url_overflow_persists_to_logs_tool_outputs(
    local_http_server: _LocalHttpServer,
    tmp_path: Path,
) -> None:
    """Test 4: rendered text > 64 KiB persists; inline is a preview + footer."""
    html_body = _large_page_html(paragraph_repeats=256)
    local_http_server.routes["/big"] = (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        html_body,
    )
    args = BrowseUrlArgs(url=f"{local_http_server.base_url}/big")
    ctx = _ctx_with_workspace(tmp_path)

    result = await browse_url(args, ctx)

    assert result["status"] == 200
    assert result["truncated"] is True

    persisted = result["content_persisted_to"]
    assert persisted is not None, "expected overflow to persist a full copy"
    persisted_path = Path(persisted)
    # Landing directory must be
    # <workspace>/logs/tool-outputs/<session_id>/…
    assert persisted_path.is_file()
    assert persisted_path.parent.parent == tmp_path / "logs" / "tool-outputs"
    assert persisted_path.parent.name == str(ctx.session_id)

    on_disk = persisted_path.read_text(encoding="utf-8")
    # The overflow marker appears many times in the rendered body;
    # asserting on the count proves the disk file is the FULL text
    # (all 256 copies), not the preview.
    assert on_disk.count("OVERFLOW_MARKER_BROWSE_TEST_4") == 256

    # The inline preview must contain SOME copies of the marker but
    # strictly fewer than the disk file — that's what "truncated"
    # means.
    inline = result["content"]
    inline_hits = inline.count("OVERFLOW_MARKER_BROWSE_TEST_4")
    assert 0 < inline_hits < 256
    # Footer signals truncation with the persisted-to path so the
    # caller (LLM or workflow) can retrieve the full body via
    # `read_file` on that path.
    assert "[... truncated" in inline
    assert persisted in inline


# ---------------------------------------------------------------------------
# Test 5 — timeout closes the page, no leaked Chromium PID
# ---------------------------------------------------------------------------


def _register_sleep_forever_route(
    server: _LocalHttpServer,
    path: str,
    *,
    max_sleep_seconds: float = 30.0,
) -> None:
    """Route that never sends a response body within test time.

    The handler blocks its request thread on ``time.sleep`` — the
    ``ThreadingHTTPServer`` gives each request its own thread, so
    this only stalls the one request under test. ``max_sleep_seconds``
    is a safety cap so a runaway test doesn't leak a thread forever;
    real tests should trip a timeout or cancellation well before this
    fires.
    """

    def _sleep_handler(handler: BaseHTTPRequestHandler) -> Route:
        # No response line sent — Playwright's `goto` is still
        # blocked on the initial response, which is exactly the case
        # we want to exercise for timeout / cancellation.
        time.sleep(max_sleep_seconds)
        return (200, {"Content-Type": "text/plain"}, b"never")

    server.routes[path] = _sleep_handler


async def test_browse_url_timeout_closes_page_no_chromium_leak(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 5: sleep-forever route + timeout=2s → ToolTimeout AND no PID leak.

    The leak probe uses ``psutil.process_iter`` — NOT
    ``browser.contexts`` — because ``browser.contexts`` is
    trivially empty after ``browser.close()`` and doesn't prove the
    OS process was reaped. A leaked Chromium survives ``browser.close()``
    only when the tool's ``finally``-close was skipped on the error
    path; the process probe catches that regression.
    """
    _register_sleep_forever_route(local_http_server, "/sleep")

    before = _chromium_pids()

    args = BrowseUrlArgs(
        url=f"{local_http_server.base_url}/sleep",
        request_timeout_seconds=2,
    )

    start = time.monotonic()
    with pytest.raises(ToolTimeout) as excinfo:
        await browse_url(args, _ctx())
    elapsed = time.monotonic() - start

    # ToolTimeout must carry the tool_name and the numeric bound so
    # downstream diagnostics can surface both without introspection.
    assert excinfo.value.detail["tool_name"] == "browse_url"
    assert excinfo.value.detail["timeout_seconds"] == 2
    # Guard the tight bound too — a hung fetch that took >10s implies
    # the timeout wasn't actually applied and something else stopped
    # the fetch (regression in the goto timeout arg or the racer).
    assert elapsed < 10.0, f"timeout took too long: {elapsed:.2f}s"

    # AFTER the exception: no Chromium PID that appeared during the
    # call should survive. Poll briefly to let the OS reap the
    # crash-handler child before failing.
    leaked = await _wait_for_pids_to_drain(before)
    assert not leaked, f"leaked Chromium PIDs after timeout: {leaked}"


# ---------------------------------------------------------------------------
# Test 6 — cancellation via ctx.cancellation
# ---------------------------------------------------------------------------


async def test_browse_url_cancellation_raises_exchange_cancelled(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 6: cancel token flipped mid-fetch → ExchangeCancelled + no leak.

    Mirrors the fetch_url cancellation test: schedule ``cancel()``
    after 100 ms while a slow route is being fetched, then assert
    the tool aborts within a bounded latency window (one heartbeat)
    — not just "eventually raised". A bug that swapped the racer's
    branches would fall through to the timeout instead of the
    cancellation branch, and the elapsed-latency guard below would
    catch it.
    """
    _register_sleep_forever_route(local_http_server, "/sleep")

    before = _chromium_pids()

    args = BrowseUrlArgs(
        url=f"{local_http_server.base_url}/sleep",
        # Generous request timeout — the cancellation racer is what
        # should abort, not the timeout. If a regression swapped the
        # order, the elapsed-latency guard below still catches it.
        request_timeout_seconds=30,
    )
    cancel = CancelToken()
    ctx = _CtxWithCancel(base=_ctx(), cancellation=cancel)

    async def _flip_after_delay() -> None:
        await asyncio.sleep(0.1)
        cancel.cancel()

    flipper = asyncio.create_task(_flip_after_delay())
    start = time.monotonic()
    with pytest.raises(ExchangeCancelled):
        await browse_url(args, ctx)
    elapsed = time.monotonic() - start
    await flipper

    # 100 ms flip delay + a bounded grace for browser teardown +
    # asyncio task cancellation. Well below the 30s request
    # timeout — any regression that fell through to the timeout
    # branch instead of the cancellation branch would blow past
    # this bound. Chromium startup can be slow on cold caches, so
    # the bound is generous but still small enough to catch a real
    # regression.
    assert elapsed < 10.0, f"cancellation took too long: {elapsed:.2f}s"

    # No Chromium PID that appeared during the call should survive
    # the cancel path either. The tool's `finally: browser.close()`
    # is what makes this hold.
    leaked = await _wait_for_pids_to_drain(before)
    assert not leaked, f"leaked Chromium PIDs after cancel: {leaked}"


# ---------------------------------------------------------------------------
# Test 9 — redirect resolves final_url
# ---------------------------------------------------------------------------


def _register_redirect_pair(server: _LocalHttpServer) -> None:
    """Wire ``/a`` → 302 → ``/b`` and ``/b`` → rendered landing page.

    ``Location`` is a relative path so the test never has to know the
    ephemeral port. ``/b`` returns a full HTML doc so the tool's
    text-mode extractor produces a body innerText that the assertion
    below can pin against.
    """
    server.routes["/a"] = (
        302,
        {"Location": "/b", "Content-Type": "text/plain"},
        b"redirecting",
    )
    server.routes["/b"] = (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        (
            b"<!doctype html><html><head><title>b</title></head>"
            b"<body><h1>redirected</h1></body></html>"
        ),
    )


async def test_browse_url_redirect_resolves_final_url(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 9: /a → 302 → /b resolves final_url to /b and captures /b body.

    Playwright's ``page.goto`` follows the 302 by default and the tool
    reads ``page.url`` for ``final_url``, so the check compares
    ``final_url`` against the redirect TARGET (``/b``) — not the
    requested URL (``/a``). A regression that captured
    ``response.url`` instead of ``page.url`` (or that recorded
    ``args.url``) would fail this test.
    """
    _register_redirect_pair(local_http_server)
    args = BrowseUrlArgs(url=f"{local_http_server.base_url}/a")

    result = await browse_url(args, _ctx())

    assert result["status"] == 200
    assert result["final_url"].endswith("/b")
    assert not result["final_url"].endswith("/a")
    # The 302 body ("redirecting") must NOT leak into the rendered
    # content — Playwright discarded it and rendered /b's DOM.
    assert "redirected" in result["content"]
    assert "redirecting" not in result["content"]


# ---------------------------------------------------------------------------
# Test 10 — wait_selector times out on its own budget
# ---------------------------------------------------------------------------


# Note the deliberate lack of a `#target` node — the whole point of
# this fixture is that `wait_for_selector('#target')` never resolves.
# The page LOADS immediately so `goto` returns; the timeout must come
# from `page.wait_for_selector`'s own budget — a distinct code path
# from Test 5 (which exercises `goto`'s timeout on a stalled route).
_MISSING_SELECTOR_HTML = b"""<!doctype html>
<html>
  <head><title>no-target</title></head>
  <body>
    <h1>page loaded but the selector never renders</h1>
  </body>
</html>
"""


async def test_browse_url_wait_selector_timeout(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 10: wait_selector on missing node → ToolTimeout on its budget.

    Distinct code path from Test 5: ``goto`` COMPLETES successfully
    here (the page returns 200 immediately). The timeout comes from
    ``page.wait_for_selector('#target', timeout=…)`` — a separate
    Playwright call with its own timeout arg. A regression that
    forgot to pass ``timeout=`` to ``wait_for_selector`` would fall
    through to Playwright's own 30 s default and this test would
    hang for far longer than the elapsed-latency guard below allows.
    """
    local_http_server.routes["/no-target"] = (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        _MISSING_SELECTOR_HTML,
    )
    args = BrowseUrlArgs(
        url=f"{local_http_server.base_url}/no-target",
        wait_selector="#target",
        request_timeout_seconds=2,
    )

    start = time.monotonic()
    with pytest.raises(ToolTimeout) as excinfo:
        await browse_url(args, _ctx())
    elapsed = time.monotonic() - start

    # The exception's kwarg is still ``timeout_seconds`` even though
    # the tool ARG is ``request_timeout_seconds`` — the spec calls
    # this out explicitly (kolsch's ToolTimeout attribute name is
    # ``timeout_seconds``, unrelated to the tool's arg naming).
    assert excinfo.value.detail["tool_name"] == "browse_url"
    assert excinfo.value.detail["timeout_seconds"] == 2
    # Tight bound catches a regression where wait_for_selector was
    # called without a ``timeout=`` arg and fell back to Playwright's
    # 30 s default.
    assert elapsed < 10.0, f"wait_selector timeout took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Test 11 — non-2xx status returns content without raising
# ---------------------------------------------------------------------------


# A 404 body that Chromium will actually render. The <h1> lets the
# text-mode extractor produce a distinctive innerText the assertion
# can pin against — a bare "not found" body would be too easy to
# collide with any other 404 rendering.
_NOT_FOUND_HTML = b"""<!doctype html>
<html>
  <head><title>404</title></head>
  <body>
    <h1>404: this page does not exist</h1>
    <p>error-page-body-sentinel</p>
  </body>
</html>
"""


async def test_browse_url_non_2xx_returns_content_without_raising(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 11: 404 with a rendered body → status=404, content has body.

    A non-2xx response must NOT raise. The tool's contract is that
    ``status`` is surfaced verbatim so authors can branch on it —
    over-eagerly raising on 4xx/5xx forecloses that pattern. Playwright's
    ``page.goto`` returns a response for 4xx/5xx (they're valid HTTP
    responses); only network / navigation failures raise.
    """
    local_http_server.routes["/missing"] = (
        404,
        {"Content-Type": "text/html; charset=utf-8"},
        _NOT_FOUND_HTML,
    )
    args = BrowseUrlArgs(url=f"{local_http_server.base_url}/missing")

    result = await browse_url(args, _ctx())

    assert result["status"] == 404
    # Both assertions matter: the status must reflect the 404, AND
    # the error page body must render into the returned content.
    # Missing either half means the tool short-circuited on non-2xx
    # instead of surfacing the response verbatim.
    assert "404: this page does not exist" in result["content"]
    assert "error-page-body-sentinel" in result["content"]


# ---------------------------------------------------------------------------
# Test 12 — custom headers reach the target
# ---------------------------------------------------------------------------


async def test_browse_url_custom_headers_reach_target(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 12: X-Test AND caller-supplied User-Agent survive to the fixture.

    Two assertions in one test — the second is the load-bearing one:
    Playwright ships a real Chromium ``User-Agent`` by default (a long
    string that includes ``HeadlessChrome``). If the extra-header
    merge order were reversed, that default would win and the test's
    ``kolsch-test/1`` UA would never reach the fixture. The fixture
    echoes BOTH headers back into the rendered body so a single
    ``page.inner_text('body')`` capture proves each one landed.
    """

    def _echo_headers(handler: BaseHTTPRequestHandler) -> Route:
        x_test = handler.headers.get("X-Test", "")
        user_agent = handler.headers.get("User-Agent", "")
        # Render both into the body so the tool's text-mode extractor
        # surfaces them via `page.inner_text('body')`. Using distinct
        # <p> tags keeps the innerText separator predictable.
        body = (
            "<!doctype html><html><head><title>echo</title></head>"
            "<body>"
            f"<p>x-test={x_test}</p>"
            f"<p>user-agent={user_agent}</p>"
            "</body></html>"
        ).encode()
        return (200, {"Content-Type": "text/html; charset=utf-8"}, body)

    local_http_server.routes["/echo-headers"] = _echo_headers

    args = BrowseUrlArgs(
        url=f"{local_http_server.base_url}/echo-headers",
        headers={"X-Test": "hello", "User-Agent": "kolsch-test/1"},
    )

    result = await browse_url(args, _ctx())

    assert result["status"] == 200
    # X-Test proves custom headers reach the target at all.
    assert "x-test=hello" in result["content"]
    # User-Agent proves the caller override wins over Playwright's
    # default. If the merge order were flipped (Playwright default
    # taking precedence), this assertion would fail against a real
    # Chromium UA string like
    # ``Mozilla/5.0 ... HeadlessChrome/… Safari/…``.
    assert "user-agent=kolsch-test/1" in result["content"]


# ---------------------------------------------------------------------------
# Test 13 — cookies reach the target
# ---------------------------------------------------------------------------


async def test_browse_url_cookies_reach_target(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 13: cookies attach to browser context BEFORE goto → auth reached.

    The fixture branches on the ``Cookie`` request header:
    ``session=abc123`` present → ``authenticated`` body; otherwise
    ``anon``. ``browse_url`` derives the cookie's domain from
    ``args.url``'s hostname (127.0.0.1 here) and calls
    ``context.add_cookies`` BEFORE ``page.goto`` — the timing matters:
    cookies attached after ``goto`` would arrive too late to be
    included in the request. A regression that reordered those two
    steps would produce the ``anon`` body and fail the assertion.
    """

    def _cookie_gated(handler: BaseHTTPRequestHandler) -> Route:
        cookie_header = handler.headers.get("Cookie", "")
        # Match a whole cookie name=value pair, not a substring —
        # a bare "abc123" check could pass on nonsense like
        # `_ga=abc123`. The full pair proves the tool sent the
        # cookie under the expected name.
        if "session=abc123" in cookie_header:
            body = (
                b"<!doctype html><html><head><title>auth</title></head>"
                b"<body><h1>authenticated</h1></body></html>"
            )
        else:
            body = (
                b"<!doctype html><html><head><title>anon</title></head>"
                b"<body><h1>anon</h1></body></html>"
            )
        return (200, {"Content-Type": "text/html; charset=utf-8"}, body)

    local_http_server.routes["/gated"] = _cookie_gated

    args = BrowseUrlArgs(
        url=f"{local_http_server.base_url}/gated",
        cookies={"session": "abc123"},
    )

    result = await browse_url(args, _ctx())

    assert result["status"] == 200
    # The authenticated branch is the load-bearing check — asserting
    # NOT anon is the negative complement so a partial regression
    # (fixture serving both branches) can't sneak through.
    assert "authenticated" in result["content"]
    assert "anon" not in result["content"]

"""Component tests for the ``fetch_url`` tool — happy path + redirects/auth
+ failure paths + overflow persistence.

Each test spins up a local ``http.server.ThreadingHTTPServer`` on an
ephemeral port. A per-test route table (a plain ``dict[str, tuple]``)
decides how the fixture responds. No test touches a real network — the
whole surface exercises ``127.0.0.1``.

This file loads the starter-bundle template via ``importlib.util``, the
same pattern ``make_fetch_url`` in ``src/tools/builtins.py`` uses at
runtime and the sibling ``tests/unit/test_fetch_url.py`` uses for arg
validation. The template ships under
``src/cli/_starter_bundle/kolsch/tools/`` — no ``__init__.py`` up the
tree, so a plain ``import`` from the tests package won't find it.

Tests covered here (from ``process/qol-fetch-url-tool.md``):
- Test 2 — HTML article via trafilatura → clean extracted text.
- Test 3 — ``application/json`` auto-detected → pretty-printed JSON.
- Test 4 — ``format='bytes_base64'`` round-trips to the original bytes.
- Test 5 — ``follow_redirects=True`` resolves ``/a`` → ``/b`` and
  populates ``final_url``.
- Test 6 — ``follow_redirects=False`` returns the raw 302 with
  ``final_url`` unchanged.
- Test 7 — custom ``headers`` + ``cookies`` reach the target.
- Test 8 — non-2xx status is returned verbatim in ``status`` +
  ``content`` (tool does NOT raise on 4xx / 5xx).
- Test 9 — a sleep-forever route + ``request_timeout_seconds=1``
  raises ``ToolTimeout`` with ``timeout_seconds=1``.
- Test 10 — ``ctx.cancellation.cancel()`` during a slow fetch raises
  ``ExchangeCancelled`` within one heartbeat.
- Test 11 — extracted text >64 KiB persists to
  ``<workspace>/logs/tool-outputs/…`` and inline ``content`` is a
  truncated preview.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import socket
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from engine.context import CancelToken
from errors import ExchangeCancelled, ToolTimeout
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext

pytestmark = pytest.mark.slow


def _load_fetch_url_module():  # type: ignore[no-untyped-def]
    """Load the starter-bundle template as an importable module.

    Shares the module-name key with ``tests/unit/test_fetch_url.py`` so
    the two files exercise the same module object when the full suite
    runs.
    """
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


# ---------------------------------------------------------------------------
# ToolContext helper
# ---------------------------------------------------------------------------


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
# Local HTTP fixture
# ---------------------------------------------------------------------------


# Route table shape:
#   path -> (status, headers, body)  # static response
#     OR
#   path -> callable(BaseHTTPRequestHandler) -> (status, headers, body)
# where headers is a dict[str, str] and body is bytes. Callables let a
# test inspect request headers (see the /echo-headers route in Test 7)
# without teaching the fixture about every possible dynamic response.
Route = tuple[int, dict[str, str], bytes]
RouteHandler = Callable[[BaseHTTPRequestHandler], Route]
RouteTable = dict[str, "Route | RouteHandler"]


class _LocalHttpServer:
    """Wrap a ``ThreadingHTTPServer`` with a per-test route table.

    The fixture builds a handler class bound to the current route table
    and serves forever on a background thread. ``base_url`` returns the
    ``http://127.0.0.1:<port>`` prefix a test uses to build request
    URLs.
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
            # Silence the default request-per-line log line so pytest's
            # captured stderr stays readable when a test fails.
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
                # Callable routes get the raw request handler so they
                # can inspect self.headers / self.path before deciding
                # on a response.
                resolved = entry(self) if callable(entry) else entry
                status, headers, body = resolved
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # Port 0 lets the kernel pick a free port; grab it and use it.
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Fast shutdown between tests — no lingering CLOSE_WAIT.
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="fetch-url-fixture"
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
    """Provide a fresh local HTTP server per test.

    Tests populate ``server.routes`` before firing ``fetch_url``. Route
    entries are ``(status, headers, body)`` triples; the handler
    responds with ``404 not-found`` when a route isn't registered so an
    accidental path typo fails loudly.
    """
    server = _LocalHttpServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 2 — HTML article via trafilatura
# ---------------------------------------------------------------------------


# Deliberately article-shaped: <main> block with real prose that
# trafilatura's boilerplate remover will keep, plus a <script> and
# <nav> that it should strip. The assertion string below appears
# only in the extracted-text output — never in a raw-HTML dump with
# tags intact — so accidentally returning the raw HTML would fail
# the assertion.
_ARTICLE_HTML = b"""<!doctype html>
<html lang="en">
  <head>
    <title>Kolsch fetch_url happy path</title>
    <script>window.__DATA__ = {should: "not appear"};</script>
  </head>
  <body>
    <nav>
      <a href="/">home</a>
      <a href="/about">about</a>
    </nav>
    <main>
      <article>
        <h1>Kolsch fetch_url happy path</h1>
        <p>The light fetch tool wraps httpx and trafilatura so agents
        can pull static web pages without a headless browser. The
        common case &mdash; docs pages, JSON APIs, RSS feeds &mdash;
        resolves fine on this tier.</p>
        <p>Trafilatura extracts the main-content prose from an article
        and drops chrome like the nav bar above and the site footer
        below. This paragraph exists specifically to give the
        extractor real prose to keep.</p>
      </article>
    </main>
    <footer>&copy; nobody in particular</footer>
  </body>
</html>
"""


async def test_fetch_url_happy_html_trafilatura(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 2: HTML article → trafilatura-extracted body text."""
    local_http_server.routes["/article"] = (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        _ARTICLE_HTML,
    )
    args = FetchUrlArgs(url=f"{local_http_server.base_url}/article")

    result = await fetch_url(args, _ctx())

    assert result["status"] == 200
    assert result["content_type"] == "text"
    # Extracted prose must appear verbatim (trafilatura preserves
    # sentence boundaries).
    assert "Trafilatura extracts the main-content prose" in result["content"]
    # Nav / script / footer chrome must NOT appear in the extracted
    # text. If it did, we'd be returning the raw HTML instead of the
    # extracted output.
    assert "<script>" not in result["content"]
    assert "<nav>" not in result["content"]
    assert "window.__DATA__" not in result["content"]
    # No truncation — the article body is well under 64 KiB.
    assert result["truncated"] is False
    assert result["content_persisted_to"] is None


# ---------------------------------------------------------------------------
# Test 3 — JSON auto-detection
# ---------------------------------------------------------------------------


async def test_fetch_url_json_pretty_prints(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 3: application/json → content_type='json' + pretty JSON."""
    body = json.dumps({"ok": True, "n": 3}).encode("utf-8")
    local_http_server.routes["/api/status"] = (
        200,
        {"Content-Type": "application/json"},
        body,
    )
    args = FetchUrlArgs(url=f"{local_http_server.base_url}/api/status")

    result = await fetch_url(args, _ctx())

    assert result["status"] == 200
    assert result["content_type"] == "json"
    # Pretty-printed: indent=2 produces newlines between fields. A
    # single-line json.dumps would collapse everything onto one line
    # and this assertion would fail.
    assert "\n" in result["content"]
    assert '  "ok": true' in result["content"]
    assert '  "n": 3' in result["content"]
    # Round-trip through json.loads to prove the pretty output is
    # still valid JSON.
    assert json.loads(result["content"]) == {"ok": True, "n": 3}


# ---------------------------------------------------------------------------
# Test 4 — bytes_base64 round-trip
# ---------------------------------------------------------------------------


async def test_fetch_url_bytes_base64_round_trip(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 4: format='bytes_base64' round-trips to the original bytes."""
    original = b"\x00\x01\xfe\xff"
    local_http_server.routes["/blob"] = (
        200,
        {"Content-Type": "application/octet-stream"},
        original,
    )
    args = FetchUrlArgs(
        url=f"{local_http_server.base_url}/blob",
        format="bytes_base64",
    )

    result = await fetch_url(args, _ctx())

    assert result["status"] == 200
    assert result["content_type"] == "bytes_base64"
    decoded = base64.b64decode(result["content"])
    assert decoded == original


# ---------------------------------------------------------------------------
# Test 5 & 6 — redirect fixture pair
# ---------------------------------------------------------------------------


def _register_redirect_pair(server: _LocalHttpServer) -> None:
    """Wire up the ``/a`` → 302 → ``/b`` route pair.

    Both Test 5 and Test 6 exercise the SAME fixture pair — the ONLY
    difference between them is the ``follow_redirects`` arg passed to
    ``fetch_url``. Keeping the routes in one helper prevents the two
    tests from drifting apart.
    """
    # /a always redirects to /b — Location is a relative path so the
    # test doesn't need to know the ephemeral port.
    server.routes["/a"] = (
        302,
        {"Location": "/b", "Content-Type": "text/plain"},
        b"redirecting",
    )
    server.routes["/b"] = (
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
        b"landed at /b",
    )


async def test_fetch_url_redirect_follows_and_populates_final_url(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 5: follow_redirects=True → final_url ends with /b."""
    _register_redirect_pair(local_http_server)
    args = FetchUrlArgs(
        url=f"{local_http_server.base_url}/a",
        # follow_redirects defaults to True; make that explicit so the
        # test doesn't silently pass if the default ever flipped.
        follow_redirects=True,
    )

    result = await fetch_url(args, _ctx())

    assert result["status"] == 200
    assert result["final_url"].endswith("/b")
    # The /b fixture body is served as text/plain, not html, so the
    # tool takes the utf-8-decode branch and content_type stays "text".
    assert result["content_type"] == "text"
    assert "landed at /b" in result["content"]


async def test_fetch_url_no_follow_returns_302(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 6: follow_redirects=False → status=302, final_url unchanged."""
    _register_redirect_pair(local_http_server)
    args = FetchUrlArgs(
        url=f"{local_http_server.base_url}/a",
        follow_redirects=False,
    )

    result = await fetch_url(args, _ctx())

    assert result["status"] == 302
    # final_url must NOT have advanced to /b — httpx should have
    # returned the 302 response verbatim.
    assert result["final_url"].endswith("/a")
    assert not result["final_url"].endswith("/b")


# ---------------------------------------------------------------------------
# Test 7 — headers + cookies reach the target
# ---------------------------------------------------------------------------


async def test_fetch_url_auth_headers_and_cookies_reach_target(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 7: Authorization header AND session cookie reach the fixture."""

    def _echo_headers(handler: BaseHTTPRequestHandler) -> Route:
        # Echo BOTH headers back into the body so the assertion below
        # can prove each one actually reached the server (a
        # server-side check would be brittle against httpx's
        # canonicalization).
        auth = handler.headers.get("Authorization", "")
        cookie = handler.headers.get("Cookie", "")
        body = f"auth={auth}\ncookie={cookie}".encode()
        return (
            200,
            {"Content-Type": "text/plain; charset=utf-8"},
            body,
        )

    local_http_server.routes["/echo-headers"] = _echo_headers

    args = FetchUrlArgs(
        url=f"{local_http_server.base_url}/echo-headers",
        headers={"Authorization": "Bearer secret"},
        cookies={"session": "abc123"},
    )

    result = await fetch_url(args, _ctx())

    assert result["status"] == 200
    # BOTH the header AND the cookie must appear in the echoed body.
    # Missing either half is a real regression — the tool must not
    # silently drop cookies (they can arrive through a separate
    # httpx.AsyncClient constructor arg from headers).
    assert "auth=Bearer secret" in result["content"]
    assert "cookie=session=abc123" in result["content"]


# ---------------------------------------------------------------------------
# Cancellation shim
# ---------------------------------------------------------------------------


class _CtxWithCancel:
    """Minimal duck-typed ``ToolContext`` carrying a ``cancellation`` token.

    :class:`ToolContext` is a frozen Pydantic model with ``extra="forbid"``
    so a real ``cancellation`` field can't be attached from outside the
    schema. The fetch_url tool reads the token via
    ``getattr(ctx, "cancellation", None)`` — deliberately duck-typed
    precisely so tests and future call sites can inject a token without
    reshaping the ToolContext schema. This shim exposes the same
    attribute surface (agent_id / session_id / thread_id / principal /
    secrets / workspace_root / path_policy) plus ``cancellation``.
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
# Failure-path fixture routes
# ---------------------------------------------------------------------------


def _register_error_route(server: _LocalHttpServer, path: str, body: bytes) -> None:
    """Wire up a route that always returns 404 with ``body``."""
    server.routes[path] = (
        404,
        {"Content-Type": "text/plain; charset=utf-8"},
        body,
    )


def _register_sleep_forever_route(
    server: _LocalHttpServer,
    path: str,
    *,
    max_sleep_seconds: float = 30.0,
) -> None:
    """Wire up a route that never responds within test time.

    The handler blocks its request thread on ``time.sleep`` — the
    ``ThreadingHTTPServer`` gives each request its own thread, so this
    only stalls the one request under test. ``max_sleep_seconds`` is a
    safety cap so a runaway test doesn't leak a thread forever; real
    tests should trip a timeout or cancellation well before this fires.
    """

    def _sleep_handler(handler: BaseHTTPRequestHandler) -> Route:
        # No headers sent yet — sleeping here means the client is still
        # waiting on the response line, which is exactly the case we
        # want to exercise for timeout / cancellation.
        time.sleep(max_sleep_seconds)
        return (200, {"Content-Type": "text/plain"}, b"never")

    server.routes[path] = _sleep_handler


# ---------------------------------------------------------------------------
# Test 8 — non-2xx returns status + body without raising
# ---------------------------------------------------------------------------


async def test_fetch_url_non_2xx_returns_status_and_body(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 8: 404 → tool returns status=404 + the error body inline.

    The tool must NOT raise on non-2xx — a 4xx / 5xx is a valid
    response the caller may want to inspect (Retry-After, error JSON,
    etc.). Only network-level failures wrap as ``NetworkError``.
    """
    error_body = b"resource missing: sentinel-marker-Test8"
    _register_error_route(local_http_server, "/missing", error_body)
    args = FetchUrlArgs(url=f"{local_http_server.base_url}/missing")

    result = await fetch_url(args, _ctx())

    assert result["status"] == 404
    # Content-Type is text/plain, so the extraction path is
    # utf-8-decode → content_type="text".
    assert result["content_type"] == "text"
    # The fixture body must reach the caller verbatim; asserting on the
    # sentinel token proves the tool didn't swallow the body.
    assert "sentinel-marker-Test8" in result["content"]


# ---------------------------------------------------------------------------
# Test 9 — request_timeout_seconds bounds the request
# ---------------------------------------------------------------------------


async def test_fetch_url_timeout_raises_tool_timeout(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 9: sleep-forever route + timeout=1 → ToolTimeout(timeout_seconds=1).

    Asserts on the exception's ``timeout_seconds`` attribute (the
    kolsch error field on ``detail``), NOT on the request arg
    ``request_timeout_seconds`` — the two are intentionally distinct
    names so a rename on one side surfaces here.
    """
    _register_sleep_forever_route(local_http_server, "/sleep-forever")
    args = FetchUrlArgs(
        url=f"{local_http_server.base_url}/sleep-forever",
        request_timeout_seconds=1,
    )

    start = time.monotonic()
    with pytest.raises(ToolTimeout) as excinfo:
        await fetch_url(args, _ctx())
    elapsed = time.monotonic() - start

    # The kolsch ToolTimeout exposes its bound via ``detail`` (see
    # errors/tools.py::ToolTimeout). This is the field the test spec
    # explicitly calls out — "the exception's field, not the arg".
    assert excinfo.value.detail["timeout_seconds"] == 1
    assert excinfo.value.detail["tool_name"] == "fetch_url"
    # Guard the tight bound too — a hung request that took >5s would
    # be a real regression (implies the timeout wasn't actually
    # applied and something else stopped the fetch).
    assert elapsed < 5.0, f"timeout took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Test 10 — cancellation via ctx.cancellation
# ---------------------------------------------------------------------------


async def test_fetch_url_cancellation_raises_exchange_cancelled(
    local_http_server: _LocalHttpServer,
) -> None:
    """Test 10: cancel token flipped mid-fetch → ExchangeCancelled within a heartbeat.

    Mirrors the bash cancellation pattern (see the bash cancel test
    referenced in the build plan): schedule ``cancel()`` after 100 ms
    while a slow route is being fetched, then assert the tool aborts
    within a bounded latency window (not just "eventually raised").
    """
    _register_sleep_forever_route(local_http_server, "/sleep-forever")
    args = FetchUrlArgs(
        url=f"{local_http_server.base_url}/sleep-forever",
        # Give the request a generous timeout — the cancellation racer
        # is what should abort, not the timeout. If a bug swaps the
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
        await fetch_url(args, ctx)  # type: ignore[arg-type]
    elapsed = time.monotonic() - start
    await flipper

    # 100 ms flip delay + a bounded grace for asyncio task shutdown
    # and cancel propagation. Well below the 30s request timeout —
    # any regression that fell through to the timeout instead of
    # the cancellation branch would blow past this bound.
    assert elapsed < 2.0, f"cancellation took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Test 11 — overflow persistence to <workspace>/logs/tool-outputs/
# ---------------------------------------------------------------------------


# One paragraph large enough that ~256 repetitions blow past 64 KiB of
# extracted text. Wrapped in <p> tags so trafilatura sees article-shaped
# HTML and returns the concatenated prose rather than falling back to
# BeautifulSoup or None.
_BIG_PARAGRAPH = (
    "This is a very long paragraph that trafilatura will happily "
    "extract as main-content prose. It contains no navigation, no "
    "script tags, and no chrome — just enough sentences to survive the "
    "extractor's boilerplate filter. Each copy adds a few hundred "
    "bytes of extracted text so a couple hundred copies push the "
    "output well past the 64 KiB inline cap. Sentinel token: "
    "OVERFLOW_MARKER_TEST_11."
)


def _large_article_html(paragraph_repeats: int) -> bytes:
    body = "\n".join(f"<p>{_BIG_PARAGRAPH}</p>" for _ in range(paragraph_repeats))
    return (
        "<!doctype html><html><head><title>overflow</title></head>"
        "<body><main><article>"
        f"{body}"
        "</article></main></body></html>"
    ).encode()


def _ctx_with_workspace(workspace_root: Path) -> ToolContext:
    """Build a ToolContext pointed at ``workspace_root``.

    Overflow persistence writes under
    ``<workspace_root>/logs/tool-outputs/<session_id>/<call_id>.content.txt``,
    so the persistence branch only fires when ``ctx.workspace_root`` is
    set. Fresh session id per call keeps the disk layout deterministic
    for this test's assertions.
    """
    agent_id = "ag_test_overflow"
    return ToolContext(
        agent_id=AgentId(agent_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal=f"agent.{agent_id}",
        secrets={},
        workspace_root=workspace_root,
    )


async def test_fetch_url_overflow_persists_to_logs_tool_outputs(
    local_http_server: _LocalHttpServer,
    tmp_path: Path,
) -> None:
    """Test 11: extracted text >64 KiB persists; inline = preview + footer.

    Verifies:
      - ``content_persisted_to`` is a path under
        ``<workspace>/logs/tool-outputs/<session_id>/``.
      - The on-disk file contains the FULL extracted text (not the
        preview), byte-for-byte.
      - Inline ``content`` is a 64 KiB (utf-8-byte) preview with a
        ``[... truncated N bytes; ...]`` footer.
      - ``truncated is True``.
    """
    # 256 repetitions of the ~400-byte paragraph → ~100 KiB extracted,
    # comfortably past the 64 KiB threshold with headroom for
    # trafilatura's whitespace trimming.
    html_body = _large_article_html(paragraph_repeats=256)
    local_http_server.routes["/big"] = (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        html_body,
    )
    args = FetchUrlArgs(url=f"{local_http_server.base_url}/big")
    ctx = _ctx_with_workspace(tmp_path)

    result = await fetch_url(args, ctx)

    assert result["status"] == 200
    assert result["content_type"] == "text"
    assert result["truncated"] is True

    persisted = result["content_persisted_to"]
    assert persisted is not None, "expected overflow to persist a full copy"
    persisted_path = Path(persisted)
    # Landing directory must be <workspace>/logs/tool-outputs/<session_id>/…
    # exactly per the spec's overflow persistence contract. The Path
    # blocking-IO warnings ASYNC240 flags below are fine here — the
    # writes already happened synchronously inside fetch_url; these
    # asserts merely inspect the outcome.
    assert persisted_path.is_file()
    assert persisted_path.parent.parent == tmp_path / "logs" / "tool-outputs"
    assert persisted_path.parent.name == str(ctx.session_id)

    on_disk = persisted_path.read_text(encoding="utf-8")
    # The overflow marker appears many times in the extracted body;
    # asserting on the count proves the disk file is the FULL text
    # (all 256 copies), not the preview.
    assert on_disk.count("OVERFLOW_MARKER_TEST_11") == 256
    # The inline preview must contain SOME copies of the marker but
    # strictly fewer than the disk file — that's what "truncated"
    # means.
    inline = result["content"]
    inline_hits = inline.count("OVERFLOW_MARKER_TEST_11")
    assert 0 < inline_hits < 256
    # The footer signals truncation with the persisted-to path so the
    # caller (LLM or workflow) can retrieve the full body.
    assert "[... truncated" in inline
    assert persisted in inline

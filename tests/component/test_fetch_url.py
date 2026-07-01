"""Component tests for the ``fetch_url`` tool — happy path + redirects/auth.

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
"""

from __future__ import annotations

import base64
import importlib.util
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

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

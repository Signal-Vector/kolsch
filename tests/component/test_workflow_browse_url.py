"""Component tests for the ``kolsch/nodes/browse_url`` workflow node.

Tests 4, 5, 6, 7, 9 from ``process/qol-browse-url-node.md``:

- Test 4 — happy path: node fetches a local HTTP fixture and captures
  the narrowed six-field shape (``{url, final_url, status, content,
  content_persisted_to, truncated}``).
- Test 5 — Jinja renders ``url`` before dispatch: the receiver-bound
  ``url: "{{ inputs.target }}"`` resolves against ``ctx.workflow_inputs``.
- Test 6 — ``ToolUnauthorized`` from dispatch wraps as
  ``WorkflowNodeFailed`` with ``.node`` set to the failing node's name
  and ``.cause`` the underlying ``ToolUnauthorized``.
- Test 7 — ``ToolTimeout`` from the tool propagates UNWRAPPED (the
  executor turns it into ``workflow.node.failed`` — the runner is
  deliberately silent on this exception, mirroring bash).
- Test 9 — downstream node binds against ``{{ nodes.<n>.content }}``
  and reads the fixture body through the workflow scope.

Each test spins up a local ``ThreadingHTTPServer`` on an ephemeral port
— the same fixture shape used by ``tests/component/test_browse_url.py``.
The route table is duplicated here (per Step 3's guidance) so the file
reads on its own; a future shared-fixture refactor can absorb both.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from errors import ToolTimeout, ToolUnauthorized
from files import PathPolicy
from providers.authz import RuleBasedAuthorizationProvider
from providers.sandbox import NativeSandboxProvider
from providers.secrets import DotenvSecretsProvider
from tools.builtins import make_browse_url
from tools.decorator import get_tool_definition
from workflows.builtins import BrowseUrlRunner, register_builtins
from workflows.context import NodeContext
from workflows.errors import WorkflowNodeFailed
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import BrowseUrlNodeDef, Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import Iterator

# All tests in this file launch headless Chromium via the browse_url
# tool, so they cost seconds even on a warm cache.
pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Local HTTP fixture — same shape as ``tests/component/test_browse_url.py``.
# ---------------------------------------------------------------------------


Route = tuple[int, dict[str, str], bytes]
RouteHandler = Callable[[BaseHTTPRequestHandler], Route]
RouteTable = dict[str, "Route | RouteHandler"]


class _LocalHttpServer:
    """Wrap a ``ThreadingHTTPServer`` with a per-test route table.

    Mirrors ``tests/component/test_browse_url.py::_LocalHttpServer``.
    Kept local so this file reads on its own; a shared helper can
    absorb both the next time either grows a new capability.
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
            target=server.serve_forever,
            daemon=True,
            name="browse-url-node-fixture",
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
# Provider + registry helpers
# ---------------------------------------------------------------------------


_HAPPY_HTML = b"""<!doctype html>
<html>
  <head><title>fixture</title></head>
  <body>
    <h1>hello</h1>
    <p>browse-url-node-body-sentinel</p>
  </body>
</html>
"""


def _make_workspace(tmp_path: Path) -> Path:
    """Fresh workspace with an empty .env so DotenvSecretsProvider works."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return tmp_path


def _make_browse_url_defn(workspace: Path) -> Any:
    """Build a ``ToolDefinition`` for the browse_url tool.

    Stamps ``home_bundle="kolsch"`` so ``action_name`` resolves to the
    canonical 3-seg ref ``kolsch/tools/browse_url`` — matching the
    engine-driven registration path and the current authz-rule
    convention on this branch. Without the stamp, ``action_name``
    would fall back to the legacy ``tool.browse_url`` form and Test 6's
    ``kolsch/tools/browse_url``-shaped deny rule would silently miss.
    """
    # PathPolicy is unused by make_browse_url (browse_url reaches the
    # network, not the filesystem; the factory accepts policy/root for
    # signature symmetry with make_bash / make_read_file). An empty
    # policy is enough.
    fn = make_browse_url(policy=PathPolicy(), root=workspace)
    defn = get_tool_definition(fn)
    defn.home_bundle = "kolsch"
    return defn


def _make_registry(
    *,
    workspace: Path,
    authz: RuleBasedAuthorizationProvider,
) -> tuple[RunnerRegistry, Any]:
    """Build a registry wired with browse_url + the supplied authz."""
    browse_url_defn = _make_browse_url_defn(workspace)
    reg = RunnerRegistry()
    register_builtins(
        reg,
        # bash is a required kwarg — pass the browse_url defn as a
        # stand-in since these tests never dispatch bash. The runner
        # is constructed but never invoked, so its underlying tool
        # identity is irrelevant.
        bash_defn=browse_url_defn,
        authz=authz,
        secrets=DotenvSecretsProvider(
            root=workspace, config={"path": ".env"}
        ),
        sandbox=NativeSandboxProvider(config={}),
        browse_url_defn=browse_url_defn,
    )
    return reg, browse_url_defn


def _allow_all_authz() -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "*", "pattern": "*", "effect": "allow"},
            ]
        }
    )


async def _run_workflow(
    *,
    tmp_path: Path,
    workflow: Workflow,
    registry: RunnerRegistry,
    inputs: dict[str, Any] | None = None,
) -> tuple[WorkflowRunStatus, list[dict[str, Any]]]:
    """Execute ``workflow`` through the real ``IterativeExecutor``."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/x", inputs=inputs or {}
    )
    executor = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    status = await executor.run()
    return status, list(store.read_events(run.run_id))


def _register_happy_page(server: _LocalHttpServer, path: str = "/hello") -> str:
    server.routes[path] = (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        _HAPPY_HTML,
    )
    return f"{server.base_url}{path}"


# ---------------------------------------------------------------------------
# Test 4 — happy-path node run against a local HTTP fixture.
# ---------------------------------------------------------------------------


async def test_browse_url_node_happy_narrows_return_shape(
    local_http_server: _LocalHttpServer, tmp_path: Path
) -> None:
    """Test 4: node captures narrowed 6-field shape from a rendered page.

    The tool returns nine fields (``url, final_url, status, content,
    content_persisted_to, content_length, format, duration_seconds,
    truncated``). The node narrows to six — dropping ``format``,
    ``content_length``, ``duration_seconds``. Asserting the DROPPED
    fields are absent is load-bearing here: a regression that
    forwarded ``raw`` verbatim from the tool would silently expose
    them via ``{{ nodes.<n>.content_length }}`` in downstream binds.
    """
    workspace = _make_workspace(tmp_path)
    url = _register_happy_page(local_http_server)

    registry, browse_defn = _make_registry(
        workspace=workspace, authz=_allow_all_authz()
    )
    runner = BrowseUrlRunner(
        tool_defn=browse_defn,
        authz=_allow_all_authz(),
        secrets=DotenvSecretsProvider(
            root=workspace, config={"path": ".env"}
        ),
        sandbox=NativeSandboxProvider(config={}),
    )
    _ = registry  # constructed for side-effect coverage of register_builtins

    node_def = BrowseUrlNodeDef(
        type="kolsch/nodes/browse_url",
        url=url,
    )
    ctx = NodeContext.for_test(workspace_root=workspace)

    output = await runner.run(
        node_name="fetch",
        node_def=node_def,
        bound_inputs={},
        state={},
        ctx=ctx,
    )

    # Narrowed six fields present.
    assert set(output.keys()) == {
        "url",
        "final_url",
        "status",
        "content",
        "content_persisted_to",
        "truncated",
    }
    # Dropped fields must not leak.
    for dropped in ("format", "content_length", "duration_seconds"):
        assert dropped not in output, f"node leaked {dropped!r} from tool shape"

    assert output["status"] == 200
    assert output["url"] == url
    assert output["final_url"].endswith("/hello")
    assert "hello" in output["content"]
    assert "browse-url-node-body-sentinel" in output["content"]
    assert output["content_persisted_to"] is None
    assert output["truncated"] is False


# ---------------------------------------------------------------------------
# Test 5 — Jinja renders ``url`` before dispatch.
# ---------------------------------------------------------------------------


async def test_browse_url_node_jinja_renders_url_from_workflow_inputs(
    local_http_server: _LocalHttpServer, tmp_path: Path
) -> None:
    """Test 5: url ``"{{ inputs.target }}"`` resolves from workflow inputs.

    The runner composes its Jinja scope as ``{"inputs":
    {**ctx.workflow_inputs, **bound_inputs}, "state": state}``. This
    test binds the URL to ``inputs.target`` via the workflow-level
    ``workflow_inputs`` (no edge-bound override) — a regression that
    forgot to fold ``ctx.workflow_inputs`` into the scope would leave
    the Jinja template literal on the wire and the tool would fail
    URL validation.
    """
    workspace = _make_workspace(tmp_path)
    url = _register_happy_page(local_http_server, "/target")

    _, browse_defn = _make_registry(
        workspace=workspace, authz=_allow_all_authz()
    )
    runner = BrowseUrlRunner(
        tool_defn=browse_defn,
        authz=_allow_all_authz(),
        secrets=DotenvSecretsProvider(
            root=workspace, config={"path": ".env"}
        ),
        sandbox=NativeSandboxProvider(config={}),
    )

    node_def = BrowseUrlNodeDef(
        type="kolsch/nodes/browse_url",
        url="{{ inputs.target }}",
    )
    # `NodeContext.for_test` doesn't accept workflow_inputs as a kwarg;
    # the frozen dataclass exposes the field though, so bypass
    # `__setattr__` (matches the pattern used by tests/component/
    # test_workflow_map.py::_make_ctx).
    ctx = NodeContext.for_test(workspace_root=workspace)
    object.__setattr__(ctx, "workflow_inputs", {"target": url})

    output = await runner.run(
        node_name="fetch",
        node_def=node_def,
        bound_inputs={},
        state={},
        ctx=ctx,
    )

    assert output["status"] == 200
    # If the render was skipped, the tool would have received the
    # literal `{{ inputs.target }}` and raised ToolExecutionFailed
    # before returning any body — reaching this assertion proves the
    # substitution happened before dispatch.
    assert "browse-url-node-body-sentinel" in output["content"]


# ---------------------------------------------------------------------------
# Test 6 — ``ToolUnauthorized`` wraps as ``WorkflowNodeFailed``.
# ---------------------------------------------------------------------------


async def test_browse_url_node_authz_denied_raises_workflow_node_failed(
    local_http_server: _LocalHttpServer, tmp_path: Path
) -> None:
    """Test 6: rule denying ``kolsch/tools/browse_url`` → WorkflowNodeFailed.

    The runner catches ``ToolUnauthorized`` at the dispatch boundary
    and re-raises as ``WorkflowNodeFailed(node=..., invocation_id=...,
    cause=exc)``. The ``.cause`` attribute must be the raw
    ``ToolUnauthorized`` instance (not a message string) so downstream
    diagnostics can introspect ``matched_rule`` / ``principal`` etc.
    """
    workspace = _make_workspace(tmp_path)
    url = _register_happy_page(local_http_server)

    deny = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {
                    "what": "kolsch/tools/browse_url",
                    "pattern": "*",
                    "effect": "deny",
                },
                {"what": "*", "pattern": "*", "effect": "allow"},
            ]
        }
    )
    _, browse_defn = _make_registry(workspace=workspace, authz=deny)
    runner = BrowseUrlRunner(
        tool_defn=browse_defn,
        authz=deny,
        secrets=DotenvSecretsProvider(
            root=workspace, config={"path": ".env"}
        ),
        sandbox=NativeSandboxProvider(config={}),
    )

    node_def = BrowseUrlNodeDef(
        type="kolsch/nodes/browse_url",
        url=url,
    )
    ctx = NodeContext.for_test(workspace_root=workspace)

    with pytest.raises(WorkflowNodeFailed) as exc_info:
        await runner.run(
            node_name="fetch",
            node_def=node_def,
            bound_inputs={},
            state={},
            ctx=ctx,
        )

    assert exc_info.value.node == "fetch"
    assert isinstance(exc_info.value.cause, ToolUnauthorized)
    # The tool_name in the underlying ToolUnauthorized must match the
    # deny target — proves the denial fired against browse_url, not
    # some other slot that leaked into the layers tuple.
    assert exc_info.value.cause.detail["tool_name"] == "browse_url"


# ---------------------------------------------------------------------------
# Test 7 — ``ToolTimeout`` propagates unwrapped.
# ---------------------------------------------------------------------------


def _register_sleep_forever_route(
    server: _LocalHttpServer, path: str
) -> None:
    """Route whose handler thread blocks — Playwright's goto never returns."""

    def _sleep_handler(handler: BaseHTTPRequestHandler) -> Route:
        # Cap at 30 s so a runaway test doesn't leak a thread forever.
        time.sleep(30.0)
        return (200, {"Content-Type": "text/plain"}, b"never")

    server.routes[path] = _sleep_handler


async def test_browse_url_node_timeout_propagates_unwrapped(
    local_http_server: _LocalHttpServer, tmp_path: Path
) -> None:
    """Test 7: node ``request_timeout_seconds=2`` → ToolTimeout, unwrapped.

    Mirrors the bash-timeout contract: the runner does NOT catch
    ``ToolTimeout``. The executor's dispatch loop turns it into a
    ``workflow.node.failed`` event with ``error_class="ToolTimeout"``
    — but here we probe the runner directly to keep the assertion
    tight against the propagation contract. A regression that wrapped
    timeout in ``WorkflowNodeFailed`` would show up as a
    ``WorkflowNodeFailed`` raise instead.
    """
    workspace = _make_workspace(tmp_path)
    _register_sleep_forever_route(local_http_server, "/sleep")

    _, browse_defn = _make_registry(
        workspace=workspace, authz=_allow_all_authz()
    )
    runner = BrowseUrlRunner(
        tool_defn=browse_defn,
        authz=_allow_all_authz(),
        secrets=DotenvSecretsProvider(
            root=workspace, config={"path": ".env"}
        ),
        sandbox=NativeSandboxProvider(config={}),
    )

    node_def = BrowseUrlNodeDef(
        type="kolsch/nodes/browse_url",
        url=f"{local_http_server.base_url}/sleep",
        request_timeout_seconds=2,
    )
    ctx = NodeContext.for_test(workspace_root=workspace)

    start = time.monotonic()
    with pytest.raises(ToolTimeout) as exc_info:
        await runner.run(
            node_name="fetch",
            node_def=node_def,
            bound_inputs={},
            state={},
            ctx=ctx,
        )
    elapsed = time.monotonic() - start

    assert exc_info.value.detail["tool_name"] == "browse_url"
    assert exc_info.value.detail["timeout_seconds"] == 2
    # Bounded upper limit — the request_timeout_seconds=2 must actually
    # bind. A regression that dropped the arg would fall through to
    # Playwright's 30 s default.
    assert elapsed < 10.0, f"timeout took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Test 9 — downstream node binds ``{{ nodes.<name>.content }}``.
# ---------------------------------------------------------------------------


async def test_browse_url_node_downstream_binds_content(
    local_http_server: _LocalHttpServer, tmp_path: Path
) -> None:
    """Test 9: fetch → compute binds ``message = {{ nodes.fetch.content }}``.

    The downstream ``compute`` node's captured ``value`` must contain
    the fixture body — proving the six-field return shape is exposed
    in the workflow scope the same way ``bash``'s shape is. A
    regression that failed to publish ``content`` under
    ``nodes.fetch.<field>`` would trip this test on the downstream
    render step, not on browse_url itself.
    """
    workspace = _make_workspace(tmp_path)
    url = _register_happy_page(local_http_server)

    registry, _ = _make_registry(
        workspace=workspace, authz=_allow_all_authz()
    )

    wf = Workflow.model_validate(
        {
            "nodes": {
                "fetch": {
                    "type": "kolsch/nodes/browse_url",
                    "url": url,
                },
                "echo": {
                    "type": "kolsch/nodes/compute",
                    "value": "{{ inputs.message }}",
                },
            },
            "graph": {
                "edges": [
                    {
                        "from": "fetch",
                        "to": "echo",
                        "binds": {
                            "message": "{{ nodes.fetch.content }}",
                        },
                    }
                ]
            },
        }
    )

    status, events = await _run_workflow(
        tmp_path=workspace, workflow=wf, registry=registry
    )
    assert status == WorkflowRunStatus.SUCCEEDED

    echo_completed = [
        e
        for e in events
        if e["kind"] == "workflow.node.completed"
        and e["payload"]["node_name"] == "echo"
    ]
    assert len(echo_completed) == 1
    echo_value = echo_completed[0]["payload"]["output"]["value"]
    assert "browse-url-node-body-sentinel" in echo_value

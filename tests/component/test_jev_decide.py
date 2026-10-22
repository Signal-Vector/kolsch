"""Component tests for the ``jev_decide`` tool — HTTP behaviour + thresholds.

spec-136 Tests 2 and 3 (AC 4-7). Each test spins up a local
``http.server.ThreadingHTTPServer`` on an ephemeral port that plays
the Jev ``/v1/systemone`` endpoint from a per-test response queue,
and points the tool at it by replacing the ``openrouter`` profile on
the module loaded via ``importlib.util`` (same loader key as
``tests/unit/test_jev_decide_args.py``). No test touches a real
network or uses a real key.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import socket
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from schemas import CallerChain

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from providers import AuthorizationProvider

from engine.context import CancelToken
from errors import (
    CATALOG,
    ExchangeCancelled,
    JevBelowThreshold,
    Level,
    SecretNotFound,
    ToolExecutionFailed,
    ToolTimeout,
)
from providers import DotenvSecretsProvider, NativeSandboxProvider
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext, ToolDefinition
from tools.dispatch import dispatch_tool

pytestmark = pytest.mark.slow

_TEST_KEY = "sk-or-test-not-a-real-key"


def _load_jev_decide_module() -> ModuleType:
    """Load the starter-bundle template (shared key with the unit tests)."""
    module_name = "_test_kolsch_starter_bundle_jev_decide"
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
        / "jev_decide.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, tool_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_jev_decide_module()
JevDecideArgs = _MODULE.JevDecideArgs
jev_decide = _MODULE.jev_decide


# ---------------------------------------------------------------------------
# Fake Jev server
# ---------------------------------------------------------------------------

Reply = tuple[int, dict[str, str], bytes]
ReplyFn = Callable[[], Reply]


@dataclasses.dataclass
class _SeenRequest:
    path: str
    headers: dict[str, str]
    body: bytes


class _FakeJevServer:
    """``ThreadingHTTPServer`` that answers POSTs from a reply queue.

    Each POST pops the next reply (``(status, headers, body)`` or a
    zero-arg callable returning one, e.g. to sleep first). When the
    queue is empty the last reply repeats. Every request is recorded.
    """

    def __init__(self) -> None:
        self.replies: list[Reply | ReplyFn] = []
        self.seen: list[_SeenRequest] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None, "server not started"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"  # type: ignore[str-bytes-safe]  # host is str from socket.AF_INET

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                fake.seen.append(
                    _SeenRequest(
                        path=self.path,
                        headers={k.lower(): v for k, v in self.headers.items()},
                        body=body,
                    )
                )
                entry = (
                    fake.replies.pop(0) if len(fake.replies) > 1 else fake.replies[0]
                )
                status, headers, payload = entry() if callable(entry) else entry
                try:
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    return  # client gave up (timeout / cancel tests)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.daemon_threads = True
        self._server = server
        # Short poll interval so per-test shutdown doesn't cost 0.5 s.
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
            name="jev-fixture",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


@pytest.fixture
def jev_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeJevServer]:
    """Start a fake Jev server and point the ``openrouter`` profile at it."""
    server = _FakeJevServer()
    server.start()
    profile = _MODULE._PROFILES["openrouter"]
    monkeypatch.setitem(
        _MODULE._PROFILES,
        "openrouter",
        dataclasses.replace(profile, base_url=server.base_url),
    )
    # Keep exponential backoff test-fast; retry-after tests set it explicitly.
    monkeypatch.setattr(_MODULE, "_BACKOFF_BASE_SECONDS", 0.01)
    try:
        yield server
    finally:
        server.stop()


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> Reply:
    return (
        status,
        {"Content-Type": "application/json", **(headers or {})},
        json.dumps(payload).encode("utf-8"),
    )


def _ok(
    answers: dict[str, Any],
    *,
    cost: float | None = None,
    model: str = "typesafe/jev-1.14",
) -> Reply:
    usage: dict[str, Any] = {"input_tokens": 120, "output_tokens": 12}
    if cost is not None:
        usage["cost"] = cost
    return _json(200, {"model": model, "answers": answers, "usage": usage})


def _ctx(
    *,
    secrets: dict[str, str] | None = None,
    cancellation: CancelToken | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.ag_test",
        secrets={"OPENROUTER_API_KEY": _TEST_KEY} if secrets is None else secrets,
        cancellation=cancellation,
        caller_chain=CallerChain.unattributed(),
    )


_BILLING_NOUL = {"billing": {"type": "noul", "instructions": "Is this about billing?"}}


def _args(questions: dict[str, Any] | None = None) -> Any:
    return JevDecideArgs.model_validate(
        {"state": "I was charged twice.", "questions": questions or _BILLING_NOUL}
    )


# ---------------------------------------------------------------------------
# Test 2 — HTTP behaviour
# ---------------------------------------------------------------------------


async def test_happy_path_returns_model_answers_usage(
    jev_server: _FakeJevServer,
) -> None:
    """(a) 200 → {model, answers, usage}; request carried bearer + exact body."""
    # OpenRouter answers with a dated model id; the tool must return the
    # ANSWERING id, not echo the requested one (live-observed shape).
    jev_server.replies = [
        _ok(
            {"billing": {"type": "noul", "noul": 0.98}},
            model="typesafe/jev-1.14-20261014",
        )
    ]
    questions = {
        "billing": {
            "type": "noul",
            "instructions": "Is this about billing?",
            "fail_below": 0.1,
        }
    }

    result = await jev_decide(_args(questions), _ctx())

    assert result == {
        "model": "typesafe/jev-1.14-20261014",
        "answers": {"billing": {"type": "noul", "noul": 0.98}},
        "usage": {"input_tokens": 120, "output_tokens": 12},
    }
    assert "cost" not in result["usage"]
    json.dumps(result)  # JSON-serialisable
    assert len(jev_server.seen) == 1
    seen = jev_server.seen[0]
    assert seen.path == "/v1/systemone"
    assert seen.headers["authorization"] == f"Bearer {_TEST_KEY}"
    assert json.loads(seen.body) == {
        "model": "typesafe/jev-1.14",
        "state": "I was charged twice.",
        "questions": {
            "billing": {"type": "noul", "instructions": "Is this about billing?"}
        },
    }


async def test_usage_cost_passed_through_when_upstream_sends_it(
    jev_server: _FakeJevServer,
) -> None:
    jev_server.replies = [_ok({"billing": {"type": "noul", "noul": 0.9}}, cost=1.2e-6)]
    result = await jev_decide(_args(), _ctx())
    assert result["usage"] == {"input_tokens": 120, "output_tokens": 12, "cost": 1.2e-6}


async def test_answers_returned_as_upstream_sent_them(
    jev_server: _FakeJevServer,
) -> None:
    answers = {
        "team": {
            "type": "choice",
            "choice": "billing",
            "confidence": 0.91,
            "probabilities": {"billing": 0.91, "outage": 0.05, "feature": 0.04},
        },
        "urgency": {
            "type": "score",
            "score": 1.7,
            "confidence": 0.8,
            "legend": {"0": "low", "1": "mid", "2": "high"},
            "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8},
        },
    }
    jev_server.replies = [_ok(answers)]
    questions = {
        "team": {
            "type": "choice",
            "instructions": "Route",
            "criteria": {"billing": None, "outage": None, "feature": None},
        },
        "urgency": {
            "type": "score",
            "instructions": "Urgency",
            "criteria": ["low", "mid", "high"],
        },
    }
    result = await jev_decide(_args(questions), _ctx())
    assert result["answers"] == answers


async def test_429_with_retry_after_zero_then_200_succeeds(
    jev_server: _FakeJevServer,
) -> None:
    """(b) 429 retry-after:0 → retried → 200 on the 2nd attempt."""
    jev_server.replies = [
        _json(429, {"error": {"message": "slow down"}}, {"Retry-After": "0"}),
        _ok({"billing": {"type": "noul", "noul": 0.7}}),
    ]
    result = await jev_decide(_args(), _ctx())
    assert result["answers"]["billing"]["noul"] == 0.7
    assert len(jev_server.seen) == 2


async def test_three_529s_exhaust_retry_budget(jev_server: _FakeJevServer) -> None:
    """(c) 529 x3 → ToolExecutionFailed naming 529."""
    jev_server.replies = [_json(529, {"error": {"message": "overloaded"}})]
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    err = exc_info.value
    assert len(jev_server.seen) == 3
    assert "529" in str(err)
    assert err.detail["status"] == 529
    assert err.detail["attempts"] == 3
    assert err.detail["error_class"] == "UpstreamHTTP529"


def test_retry_delay_honours_retry_after_and_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry-after honoured but capped at 10 s; otherwise exponential."""
    import httpx

    monkeypatch.setattr(_MODULE, "_BACKOFF_BASE_SECONDS", 1.0)

    def _resp(headers: dict[str, str]) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    assert _MODULE._retry_delay(_resp({"retry-after": "3"}), 1) == 3.0
    assert _MODULE._retry_delay(_resp({"retry-after": "120"}), 1) == 10.0
    assert _MODULE._retry_delay(_resp({}), 1) == 1.0
    assert _MODULE._retry_delay(_resp({}), 2) == 2.0
    assert _MODULE._retry_delay(_resp({}), 6) == 10.0
    # HTTP-date form: a past date waits 0 s; a far-future date is capped.
    assert (
        _MODULE._retry_delay(_resp({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}), 2)
        == 0.0
    )
    assert (
        _MODULE._retry_delay(_resp({"retry-after": "Fri, 01 Jan 2100 00:00:00 GMT"}), 2)
        == 10.0
    )
    # Unparseable falls back to backoff.
    assert _MODULE._retry_delay(_resp({"retry-after": "soon"}), 2) == 2.0


async def test_401_carries_connect_recovery_hint(jev_server: _FakeJevServer) -> None:
    """(d) 401 → ToolExecutionFailed with recovery_hint 'kolsch connect openrouter'."""
    jev_server.replies = [
        _json(401, {"error": {"message": "No auth credentials found"}})
    ]
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    err = exc_info.value
    assert err.recovery_hint is not None
    assert "kolsch connect openrouter" in err.recovery_hint
    assert err.detail["status"] == 401
    assert len(jev_server.seen) == 1  # not retried
    # The key never leaks into the error envelope.
    assert _TEST_KEY not in json.dumps(err.to_dict())


async def test_403_carries_connect_recovery_hint(jev_server: _FakeJevServer) -> None:
    jev_server.replies = [_json(403, {"error": {"message": "forbidden"}})]
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    assert exc_info.value.recovery_hint == "kolsch connect openrouter"


async def test_422_carries_upstream_loc_msg(jev_server: _FakeJevServer) -> None:
    """(e) 422 → ToolExecutionFailed containing the upstream ``msg``."""
    jev_server.replies = [
        _json(
            422,
            {
                "detail": [
                    {
                        "loc": ["body", "questions", "team", "criteria"],
                        "msg": "Dictionary should have at least 1 item",
                        "type": "too_short",
                    }
                ]
            },
        )
    ]
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    err = exc_info.value
    assert err.detail["status"] == 422
    assert err.detail["upstream_message"] == [
        {
            "loc": ["body", "questions", "team", "criteria"],
            "msg": "Dictionary should have at least 1 item",
            "type": "too_short",
        }
    ]
    assert "Dictionary should have at least 1 item" in json.dumps(err.to_dict())
    assert err.recovery_hint is None


@pytest.mark.parametrize("status", [400, 413])
async def test_400_and_413_carry_upstream_message(
    jev_server: _FakeJevServer, status: int
) -> None:
    jev_server.replies = [_json(status, {"error": {"message": "max_tokens_exceeded"}})]
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    assert exc_info.value.detail["status"] == status
    assert exc_info.value.detail["upstream_message"] == "max_tokens_exceeded"


async def test_other_5xx_reported_as_upstream_failure(
    jev_server: _FakeJevServer,
) -> None:
    jev_server.replies = [(502, {"Content-Type": "text/plain"}, b"bad gateway")]
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    err = exc_info.value
    assert err.detail["status"] == 502
    assert err.detail["reason"] == "upstream failure"
    assert err.detail["upstream_message"] == "bad gateway"
    assert len(jev_server.seen) == 1  # only 429 / 529 retry


@pytest.mark.parametrize(
    "reply",
    [
        (200, {"Content-Type": "application/json"}, b"{not json"),
        _json(200, {"model": "typesafe/jev-1.14", "answers": {}}),
        _json(
            200,
            {
                "model": "typesafe/jev-1.14",
                "answers": {"billing": {"type": "noul"}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        _json(
            200,
            {
                "model": "typesafe/jev-1.14",
                "answers": {"other": {"type": "noul", "noul": 0.5}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        _json(
            200,
            {
                "model": "typesafe/jev-1.14",
                "answers": {
                    "billing": {
                        "type": "choice",
                        "choice": "x",
                        "confidence": 0.5,
                        "probabilities": {"x": 1.0},
                    }
                },
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        # NaN / Infinity would slip past a `<` threshold check: fail closed.
        (
            200,
            {"Content-Type": "application/json"},
            b'{"model": "typesafe/jev-1.14", "answers": {"billing": '
            b'{"type": "noul", "noul": NaN}}, '
            b'"usage": {"input_tokens": 1, "output_tokens": 1}}',
        ),
        (
            200,
            {"Content-Type": "application/json"},
            b'{"model": "typesafe/jev-1.14", "answers": {"billing": '
            b'{"type": "noul", "noul": Infinity}}, '
            b'"usage": {"input_tokens": 1, "output_tokens": 1}}',
        ),
    ],
    ids=[
        "not-json",
        "missing-usage",
        "answer-missing-metric",
        "unasked",
        "type-mismatch",
        "nan-metric",
        "infinite-metric",
    ],
)
async def test_malformed_200_raises_tool_execution_failed(
    jev_server: _FakeJevServer, reply: Reply
) -> None:
    """(f) a malformed 200 body → ToolExecutionFailed, never a raw exception."""
    jev_server.replies = [reply]
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    assert exc_info.value.detail["error_class"] == "MalformedResponse"


async def test_slow_server_raises_tool_timeout(
    jev_server: _FakeJevServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(g) a server sleeping past the request timeout → ToolTimeout."""
    monkeypatch.setattr(_MODULE, "_REQUEST_TIMEOUT_SECONDS", 0.3)

    def _slow() -> Reply:
        time.sleep(1.5)
        return _ok({"billing": {"type": "noul", "noul": 0.9}})

    jev_server.replies = [_slow]
    with pytest.raises(ToolTimeout) as exc_info:
        await jev_decide(_args(), _ctx(cancellation=CancelToken()))
    assert exc_info.value.detail["timeout_seconds"] == 0.3


async def test_cancel_during_retry_wait_returns_promptly(
    jev_server: _FakeJevServer,
) -> None:
    """(h) cancelling during a retry-after wait raises ExchangeCancelled fast."""
    jev_server.replies = [
        _json(429, {"error": {"message": "slow down"}}, {"Retry-After": "10"})
    ]
    token = CancelToken()

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.2)
        token.cancel()

    canceller = asyncio.create_task(_cancel_soon())
    start = time.monotonic()
    with pytest.raises(ExchangeCancelled):
        await jev_decide(_args(), _ctx(cancellation=token))
    elapsed = time.monotonic() - start
    await canceller
    assert elapsed < 2.0
    assert len(jev_server.seen) == 1


async def test_cancel_during_request_returns_promptly(
    jev_server: _FakeJevServer,
) -> None:
    def _slow() -> Reply:
        time.sleep(3)
        return _ok({"billing": {"type": "noul", "noul": 0.9}})

    jev_server.replies = [_slow]
    token = CancelToken()

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.2)
        token.cancel()

    canceller = asyncio.create_task(_cancel_soon())
    start = time.monotonic()
    with pytest.raises(ExchangeCancelled):
        await jev_decide(_args(), _ctx(cancellation=token))
    await canceller
    assert time.monotonic() - start < 2.0


async def test_slow_server_without_cancel_token_raises_tool_timeout(
    jev_server: _FakeJevServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No CancelToken → the ``asyncio.wait_for`` bound still yields ToolTimeout."""
    monkeypatch.setattr(_MODULE, "_REQUEST_TIMEOUT_SECONDS", 0.3)

    def _slow() -> Reply:
        time.sleep(1.5)
        return _ok({"billing": {"type": "noul", "noul": 0.9}})

    jev_server.replies = [_slow]
    with pytest.raises(ToolTimeout):
        await jev_decide(_args(), _ctx(cancellation=None))


async def test_outer_task_cancel_does_not_orphan_request(
    jev_server: _FakeJevServer,
) -> None:
    """Cancelling the calling task mid-request cancels the in-flight request."""

    def _slow() -> Reply:
        time.sleep(2)
        return _ok({"billing": {"type": "noul", "noul": 0.9}})

    jev_server.replies = [_slow]
    call = asyncio.create_task(jev_decide(_args(), _ctx(cancellation=CancelToken())))
    await asyncio.sleep(0.2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    leftovers = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    assert leftovers == []


async def test_non_selectable_profile_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _MODULE._PROFILES["openrouter"]
    monkeypatch.setitem(
        _MODULE._PROFILES,
        "openrouter",
        dataclasses.replace(profile, selectable=False),
    )
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    assert exc_info.value.detail["error_class"] == "ProfileNotSelectable"
    assert exc_info.value.detail["profile"] == "openrouter"


async def test_connection_refused_is_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens on this port now
    profile = _MODULE._PROFILES["openrouter"]
    monkeypatch.setitem(
        _MODULE._PROFILES,
        "openrouter",
        dataclasses.replace(profile, base_url=f"http://127.0.0.1:{port}"),
    )
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await jev_decide(_args(), _ctx())
    assert exc_info.value.detail["error_class"] == "NetworkError"


# ---------------------------------------------------------------------------
# Through dispatch_tool (secrets path)
# ---------------------------------------------------------------------------


class _AllowAuthz:
    async def allow(
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        layers: tuple[Any, ...] = (),
        caller_chain: tuple[str, ...] = (),
        tool_call_id: str | None = None,
    ) -> Any:
        class _Allowed:
            allowed = True
            matched_rule = None
            reason = "test-allow"

        return _Allowed()


def _defn() -> ToolDefinition:
    defn: ToolDefinition = jev_decide._tool_definition
    return dataclasses.replace(defn, home_bundle="kolsch")


async def test_missing_secret_raises_secret_not_found_via_dispatch(
    tmp_path: Path, jev_server: _FakeJevServer
) -> None:
    """(i) no OPENROUTER_API_KEY in .env → SecretNotFound from dispatch."""
    jev_server.replies = [_ok({"billing": {"type": "noul", "noul": 0.9}})]
    (tmp_path / ".env").write_text("OTHER_KEY=x\n", encoding="utf-8")
    with pytest.raises(SecretNotFound) as exc_info:
        await dispatch_tool(
            defn=_defn(),
            args={"state": "I was charged twice.", "questions": _BILLING_NOUL},
            context=_ctx(secrets={}),
            authz=cast("AuthorizationProvider", _AllowAuthz()),
            secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
            sandbox=NativeSandboxProvider(),
        )
    assert exc_info.value.detail["name"] == "OPENROUTER_API_KEY"
    assert jev_server.seen == []  # never reached the network


async def test_dispatch_resolves_secret_from_dotenv(
    tmp_path: Path, jev_server: _FakeJevServer
) -> None:
    jev_server.replies = [_ok({"billing": {"type": "noul", "noul": 0.9}})]
    (tmp_path / ".env").write_text(
        f"OPENROUTER_API_KEY={_TEST_KEY}\n", encoding="utf-8"
    )
    result = await dispatch_tool(
        defn=_defn(),
        args={"state": "I was charged twice.", "questions": _BILLING_NOUL},
        context=_ctx(secrets={}),
        authz=cast("AuthorizationProvider", _AllowAuthz()),
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(),
    )
    assert result["answers"]["billing"]["noul"] == 0.9
    assert jev_server.seen[0].headers["authorization"] == f"Bearer {_TEST_KEY}"


# ---------------------------------------------------------------------------
# Test 3 — threshold fail outcome (D6)
# ---------------------------------------------------------------------------


async def test_threshold_choice_confidence_below_raises(
    jev_server: _FakeJevServer,
) -> None:
    answers = {
        "team": {
            "type": "choice",
            "choice": "billing",
            "confidence": 0.4,
            "probabilities": {"billing": 0.4, "outage": 0.35, "feature": 0.25},
        }
    }
    jev_server.replies = [_ok(answers)]
    questions = {
        "team": {
            "type": "choice",
            "instructions": "Route",
            "criteria": {"billing": None, "outage": None, "feature": None},
            "fail_below": 0.6,
        }
    }
    with pytest.raises(JevBelowThreshold) as exc_info:
        await jev_decide(_args(questions), _ctx())
    err = exc_info.value
    assert err.code == "tool.jev_below_threshold"
    assert err.level is Level.DIAGNOSTIC
    assert err.detail["failures"] == [
        {"question": "team", "metric": "confidence", "value": 0.4, "threshold": 0.6}
    ]
    assert err.detail["answers"] == answers
    assert err.failures == err.detail["failures"]
    assert err.answers == answers


async def test_threshold_score_and_noul_use_their_own_metric(
    jev_server: _FakeJevServer,
) -> None:
    answers = {
        "urgency": {
            "type": "score",
            "score": 0.8,
            "confidence": 0.99,  # high confidence must NOT rescue a low score
            "legend": {"0": "low", "1": "mid", "2": "high"},
            "probabilities": {"0": 0.3, "1": 0.6, "2": 0.1},
        },
        "billing": {"type": "noul", "noul": 0.2},
    }
    jev_server.replies = [_ok(answers)]
    questions = {
        "urgency": {
            "type": "score",
            "instructions": "Urgency",
            "criteria": ["low", "mid", "high"],
            "fail_below": 1.0,
        },
        "billing": {
            "type": "noul",
            "instructions": "Billing?",
            "fail_below": 0.5,
        },
    }
    with pytest.raises(JevBelowThreshold) as exc_info:
        await jev_decide(_args(questions), _ctx())
    assert exc_info.value.detail["failures"] == [
        {"question": "urgency", "metric": "score", "value": 0.8, "threshold": 1.0},
        {"question": "billing", "metric": "noul", "value": 0.2, "threshold": 0.5},
    ]


async def test_threshold_equal_metric_passes(jev_server: _FakeJevServer) -> None:
    jev_server.replies = [_ok({"billing": {"type": "noul", "noul": 0.5}})]
    questions = {
        "billing": {"type": "noul", "instructions": "Billing?", "fail_below": 0.5}
    }
    result = await jev_decide(_args(questions), _ctx())
    assert result["answers"]["billing"]["noul"] == 0.5


async def test_threshold_absent_never_raises_on_values(
    jev_server: _FakeJevServer,
) -> None:
    answers = {
        "team": {
            "type": "choice",
            "choice": "x",
            "confidence": 0.01,
            "probabilities": {"x": 0.01, "y": 0.0},
        },
        "billing": {"type": "noul", "noul": 0.0},
    }
    jev_server.replies = [_ok(answers)]
    questions = {
        "team": {
            "type": "choice",
            "instructions": "?",
            "criteria": {"x": None, "y": None},
        },
        "billing": {"type": "noul", "instructions": "?"},
    }
    result = await jev_decide(_args(questions), _ctx())
    assert result["answers"] == answers


def test_threshold_code_registered_in_catalog_at_diagnostic() -> None:
    assert JevBelowThreshold.code == "tool.jev_below_threshold"
    assert CATALOG["tool.jev_below_threshold"] is Level.DIAGNOSTIC


# ---------------------------------------------------------------------------
# AC 8 — default authz rule
# ---------------------------------------------------------------------------


def test_default_rules_prompt_jev_decide_before_deny_tail() -> None:
    from cli._setup import _workspace_only_default_rules

    rules = _workspace_only_default_rules()
    jev = {"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": "prompt"}
    assert jev in rules
    assert rules[-1] == {"what": "*", "pattern": "*", "effect": "deny"}
    assert rules.index(jev) < len(rules) - 1

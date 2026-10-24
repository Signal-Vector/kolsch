"""bug-148 Test 5 — a failed `kolsch run` in a real spawned CLI process.

Reproduces the 2026-10-21 incident without tokens: a fresh workspace's
`opencode` provider points at a local stub that answers every
`POST …/chat/completions` with HTTP 400, and `python -m cli run` goes
through the real CrewAI/OpenAI client. Without `-v` the terminal shows
only the failure block (no traceback, no CrewAI panels or warnings, no
third-party log lines), stdout stays empty, and `logs/kolsch.log` keeps
the detail. With `-v` the traceback is on stderr too.

Also spawns the stub-free pre-flight case (`--session not-an-id`) so the
pre-flight wiring is proven in a real process (QA NB-6).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest
import yaml

from tests.component.test_cli_smoke import _assert_no_traceback, _run

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterator
    from pathlib import Path

_AGENT = "kolsch/agents/helper"
_MODEL = "opencode/stub-model"
_STUB_BODY = json.dumps(
    {"type": "error", "error": {"type": "MissingSessionID", "message": "stub 400"}}
).encode()
_NOISE = (
    "Traceback",
    "LLM Error",
    "CrewAIEventsBus",
    "ERROR root",
    "OpenAI API call failed",
)


class _Stub400(BaseHTTPRequestHandler):
    """400 on every POST (chat completions); an empty catalog on GET."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self._reply(400, _STUB_BODY)

    def do_GET(self) -> None:
        self._reply(200, b'{"data": []}')

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_run` copies `os.environ`: keep an ambient proxy from routing the
    # spawned CLI's calls to the local stub elsewhere.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def stub_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub400)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _workspace_with_failing_opencode(root: Path, base_url: str) -> Path:
    result = _run("setup", "--non-interactive", workspace=root, timeout=60)
    assert result.returncode == 0, result.stderr
    # The shape `kolsch connect opencode` writes, pointed at the stub.
    platform = root / "platform.yaml"
    data = yaml.safe_load(platform.read_text(encoding="utf-8"))
    data.setdefault("llm_providers", {})["opencode"] = {
        "base_url": base_url,
        "api_key_secret": "OPENCODE_API_KEY",
        "timeout": 5,
    }
    platform.write_text(yaml.safe_dump(data), encoding="utf-8")
    env_file = root / ".env"
    existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    env_file.write_text(
        existing + "OPENCODE_API_KEY=dummy-not-a-key\n", encoding="utf-8"
    )
    return root


def _session_ids(root: Path) -> set[str]:
    return {p.stem for p in (root / "sessions").glob("ses_*.jsonl")}


def _run_failing(
    root: Path, *prefix: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    before = _session_ids(root)
    result = _run(
        *prefix,
        "run",
        _AGENT,
        "--model",
        _MODEL,
        "--input",
        "content=hello",
        workspace=root,
        timeout=90,
    )
    created = _session_ids(root) - before
    assert len(created) == 1, (created, result.stderr)
    return result, created.pop()


def test_failed_run_prints_only_the_block_and_logs_the_detail(
    tmp_path: Path, stub_url: str
) -> None:
    root = _workspace_with_failing_opencode(tmp_path / "ws", stub_url)

    result, session_id = _run_failing(root)

    assert result.returncode == 2, result.stderr  # (a)
    assert result.stdout == ""  # (b) no panels, no warnings
    # (c) a provider HTTP 400 is classified at the call seam (bug-145)
    assert "model.bad_request" in result.stderr
    assert f"  session: {session_id}" in result.stderr
    assert f"kolsch sessions events {session_id}" in result.stderr
    for noise in _NOISE:  # (d)
        assert noise not in result.stderr, (noise, result.stderr)
    log = (root / "logs" / "kolsch.log").read_text(encoding="utf-8")  # (e)
    assert "OpenAI API call failed" in log
    assert "Traceback (most recent call last)" in log
    assert "AgentExecutionError" in log
    assert "dummy-not-a-key" not in log

    verbose, _ = _run_failing(root, "-v")

    assert verbose.returncode == 2
    assert verbose.stdout == ""
    assert "Traceback (most recent call last)" in verbose.stderr
    assert "ERROR root: OpenAI API call failed" in verbose.stderr
    assert "LLM Error" not in verbose.stderr
    assert "CrewAIEventsBus" not in verbose.stderr


def test_malformed_session_is_one_error_line_in_a_real_process(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    assert (
        _run("setup", "--non-interactive", workspace=root, timeout=60).returncode == 0
    )

    result = _run(
        "run", _AGENT, "--session", "not-an-id", "--input", "content=hi", workspace=root
    )

    assert result.returncode == 2
    assert result.stdout == ""
    _assert_no_traceback(result)
    assert result.stderr.startswith("error: invalid --session 'not-an-id': ")

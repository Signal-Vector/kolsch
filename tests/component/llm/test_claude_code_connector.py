"""Component tests for the `claude_code` provider connector (spec-118).

Covers spec-118 Tests 2, 4, 5, 6 — every case drives the connector
through the **injected runner**, never a live `claude`:

- **content** (Test 2): `.call` returns a `str`; a non-None `tools` list
  is ignored; through `make_llm_callable` the engine gets
  `LLMResponse(content=…, tool_calls=None)`; `supports_function_calling`
  is `False`.
- **invocation** (Test 4): captured argv / env / cwd show the redirect
  vars scrubbed, `--disallowedTools=*`, `--permission-mode dontAsk`,
  `--model <id>`, a neutral (temp, non-repo) cwd, and config isolation
  (`--setting-sources user`). See the `--bare` deviation note in the
  connector docstring — `--bare` is INCOMPATIBLE with subscription auth
  on the installed CLI, so neutral-cwd + `--setting-sources user`
  provide the isolation `--bare` was meant to.
- **failure** (Test 5): non-zero exit → Recoverable `model.server_error`;
  timeout → Recoverable `model.timeout` and the child is reaped; missing
  binary → `validate_credentials(ok=False)` + Fatal
  `model.binary_unavailable` at runtime; no child stdout/argv/prompt in
  any raised error.
- **models** (Test 6): `discover_models` aliases each carry a non-None
  `context_length`; registry lookup by `type_name` resolves the
  connector.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

import llm.providers  # noqa: F401 — import side-effect registers connectors
from engine._crewai_adapter import make_llm_callable
from engine.framework import LLMRequest, Message
from errors import (
    ModelAuthenticationFailed,
    ModelBinaryUnavailable,
    ModelServerError,
    ModelTimeout,
    PlatformError,
)
from errors.level import Level
from llm import registry
from llm.protocol import Credentials, ProviderConfig
from llm.providers.claude_code import (
    _SCRUBBED_ENV_VARS,
    ClaudeCodeConnector,
    _ClaudeCodeLLM,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_SUCCESS_JSON = json.dumps({"is_error": False, "result": "hello from claude"})
_NOT_LOGGED_IN_JSON = json.dumps(
    {"is_error": True, "subtype": "success", "result": "Not logged in · run /login"}
)


class _StubCompleted:
    """Minimal `_CompletedProcess` stand-in."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


class _RecordingRunner:
    """Captures argv/env/cwd/timeout; returns a canned result or raises."""

    def __init__(
        self,
        *,
        result: _StubCompleted | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._result = (
            result if result is not None else _StubCompleted(0, _SUCCESS_JSON)
        )
        self._raises = raises
        self.argv: list[str] | None = None
        self.env: Mapping[str, str] | None = None
        self.cwd: str | None = None
        self.timeout: float | None = None
        self.calls = 0

    def __call__(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        timeout: float,
    ) -> _StubCompleted:
        self.calls += 1
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        if self._raises is not None:
            raise self._raises
        return self._result


# ===========================================================================
# Test 6 — discover_models catalog + registry
# ===========================================================================


def test_claude_code_registry_resolves_connector() -> None:
    connector = registry.get("claude_code")
    assert isinstance(connector, ClaudeCodeConnector)
    assert connector.type_name == "claude_code"


def test_claude_code_models_all_have_context_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # qol-124: isolate from any real ~/.claude.json cache — pure static base.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    connector = ClaudeCodeConnector()
    entries = asyncio.run(connector.discover_models(Credentials(), None))
    ids = {e.id for e in entries}
    assert {"sonnet", "opus", "haiku"} <= ids
    for entry in entries:
        assert entry.context_length is not None
        assert entry.context_length > 0


def test_claude_code_discover_includes_versioned_pins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """qol-123 — discover surfaces the aliases AND current versioned ids,
    each at the conservative 200k subscription window."""
    # qol-124: isolate from any real ~/.claude.json cache — pure static base.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    connector = ClaudeCodeConnector()
    entries = asyncio.run(connector.discover_models(Credentials(), None))
    ids = {e.id for e in entries}
    assert "opus" in ids  # a stable alias
    assert "claude-opus-4-8" in ids  # a versioned pin
    assert len(entries) > 3
    for entry in entries:
        assert entry.context_length == 200_000


def test_claude_code_versioned_pin_serializes_with_context_length() -> None:
    """qol-123 — a versioned-id catalog entry round-trips through the
    platform.yaml serializer WITH its context_length, so a fresh connect /
    refresh never writes a context-less entry."""
    from cli.llm_commands import _model_entry_to_yaml
    from llm.protocol import ModelEntry

    row = _model_entry_to_yaml(ModelEntry(id="claude-opus-4-8", context_length=200_000))
    assert row == {"id": "claude-opus-4-8", "context_length": 200_000}


def test_discover_merges_cache_extra_models(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-124 — discover_models merges the account's cache extras onto the
    static base (deduped, static-first, all 200k), and a discovered entry
    persists with its context_length."""
    from cli.llm_commands import _model_entry_to_yaml

    cfg_dir = tmp_path / "cc"
    cfg_dir.mkdir()
    (cfg_dir / ".claude.json").write_text(
        json.dumps(
            {
                "additionalModelOptionsCache": [
                    {"value": "claude-opus-4-6", "label": "Opus 4.6"},  # new -> merged
                    {"value": "claude-opus-4-8[1m]"},  # already in base -> deduped
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))

    connector = ClaudeCodeConnector()
    entries = asyncio.run(connector.discover_models(Credentials(), None))
    ids = [e.id for e in entries]

    assert "claude-opus-4-6" in ids  # discovered extra, merged on top
    assert ids.count("claude-opus-4-8") == 1  # cache dup did NOT duplicate the base
    assert "opus" in ids  # static base survives
    assert len(ids) == len(set(ids))  # no dups
    for entry in entries:
        assert entry.context_length == 200_000

    discovered = next(e for e in entries if e.id == "claude-opus-4-6")
    assert _model_entry_to_yaml(discovered) == {
        "id": "claude-opus-4-6",
        "context_length": 200_000,
    }


def test_claude_code_auth_prompt_has_no_secret_field() -> None:
    connector = ClaudeCodeConnector()
    spec = connector.auth_prompt_spec()
    assert not any(f.kind == "secret" for f in spec.fields)


# ===========================================================================
# Test 2 — .call returns str; tools ignored; adapter → LLMResponse.content
# ===========================================================================


def test_claude_code_call_returns_str_and_ignores_tools() -> None:
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    # A non-None tools list must be ignored (v1 text-only).
    out = llm.call(
        [{"role": "user", "content": "hi"}],
        tools=[{"name": "some_tool", "schema": {}}],
    )
    assert out == "hello from claude"
    assert isinstance(out, str)


def test_claude_code_supports_function_calling_is_false() -> None:
    llm = _ClaudeCodeLLM(model="sonnet", runner=_RecordingRunner())
    assert llm.supports_function_calling() is False


def test_claude_code_str_message_form_is_accepted() -> None:
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    assert llm.call("just a bare string prompt") == "hello from claude"


async def test_claude_code_through_adapter_maps_str_to_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driven through make_llm_callable, a str return → content, tool_calls None."""
    # Disable the adapter's wait_for wrap so the sync stub runs unbounded.
    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "0")
    runner = _RecordingRunner()
    connector = ClaudeCodeConnector(runner=runner)
    provider_config = ProviderConfig(
        name="claude_code",
        type="claude_code",
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
    )
    crewai_llm = await connector.build_llm("sonnet", provider_config, None)
    callable_ = make_llm_callable(crewai_llm)
    request = LLMRequest(
        messages=[
            Message(role="system", content="be terse"),
            Message(role="user", content="hi"),
        ],
        # Tools present on the request must not break the call.
        tools=[{"name": "some_tool", "schema": {"type": "object"}}],
        model="sonnet",
    )
    response = await callable_(request)
    assert response.content == "hello from claude"
    assert response.tool_calls is None


# ===========================================================================
# Test 4 — env scrub + invocation shape
# ===========================================================================


def test_claude_code_invocation_scrubs_redirect_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in _SCRUBBED_ENV_VARS:
        monkeypatch.setenv(var, "SHOULD_BE_REMOVED")
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="opus", runner=runner)
    llm.call([{"role": "user", "content": "hi"}])
    assert runner.env is not None
    for var in _SCRUBBED_ENV_VARS:
        assert var not in runner.env, f"{var} leaked into the child env"


def test_claude_code_invocation_argv_shape(tmp_path: Any) -> None:
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    llm.call([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])
    argv = runner.argv
    assert argv is not None
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "sonnet"
    # Single `=`-bound token (not two separate argv entries) — the
    # installed `claude` CLI declares `--disallowedTools` variadic
    # (`<tools...>`), which would otherwise swallow a following bare
    # `body` positional with no intervening recognized flag.
    assert "--disallowedTools=*" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    # Config isolation (the --bare-replacement; see connector docstring).
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--append-system-prompt") + 1] == "sys"
    # --bare must NOT be present (incompatible with subscription auth).
    assert "--bare" not in argv


def test_claude_code_invocation_uses_neutral_temp_cwd() -> None:
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    llm.call([{"role": "user", "content": "hi"}])
    assert runner.cwd is not None
    # Neutral cwd is a temp dir, not the repo/workspace — no CLAUDE.md to
    # auto-discover.
    assert "kolsch-claude-" in runner.cwd


def test_claude_code_subprocess_timeout_below_adapter_default() -> None:
    """The connector timeout sits below the adapter's 210s wait_for wrap."""
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    llm.call([{"role": "user", "content": "hi"}])
    assert runner.timeout is not None
    assert runner.timeout < 210.0


# ===========================================================================
# Test 5 — failure mapping + redaction + reap
# ===========================================================================


def test_claude_code_nonzero_exit_is_recoverable_server_error() -> None:
    runner = _RecordingRunner(
        result=_StubCompleted(1, "some crash noise on stdout SECRET-CANARY")
    )
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    with pytest.raises(ModelServerError) as exc_info:
        llm.call([{"role": "user", "content": "hi"}])
    assert exc_info.value.level is Level.RECOVERABLE
    # Redaction: child stdout must not appear anywhere on the error.
    assert "SECRET-CANARY" not in str(exc_info.value)
    assert "SECRET-CANARY" not in json.dumps(exc_info.value.to_dict())


def test_claude_code_timeout_is_recoverable_model_timeout() -> None:
    # The real `subprocess.run(timeout=…)` raises `TimeoutExpired(cmd=argv)`,
    # and argv carries the rendered prompt + `--append-system-prompt`. The
    # raised `ModelTimeout` must NOT chain it (spec-118 B1) — else the prompt
    # leaks into the durable JSONL log via `PlatformError.to_dict()`.
    prompt_canary = "USER-SECRET-TIMEOUT-CANARY-5518"
    runner = _RecordingRunner(
        raises=subprocess.TimeoutExpired(
            cmd=[
                "claude",
                "-p",
                prompt_canary,
                "--append-system-prompt",
                prompt_canary,
            ],
            timeout=180.0,
        )
    )
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    with pytest.raises(ModelTimeout) as exc_info:
        llm.call([{"role": "user", "content": prompt_canary}])
    assert exc_info.value.level is Level.RECOVERABLE
    # Redaction: the argv/prompt in TimeoutExpired must not reach the error.
    assert prompt_canary not in json.dumps(exc_info.value.to_dict())
    assert prompt_canary not in str(exc_info.value)


def test_claude_code_missing_binary_is_fatal_binary_unavailable() -> None:
    runner = _RecordingRunner(raises=FileNotFoundError("claude"))
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    with pytest.raises(ModelBinaryUnavailable) as exc_info:
        llm.call([{"role": "user", "content": "hi"}])
    assert exc_info.value.level is Level.FATAL
    assert exc_info.value.code == "model.binary_unavailable"


def test_claude_code_not_logged_in_result_is_fatal_auth_failed() -> None:
    runner = _RecordingRunner(result=_StubCompleted(0, _NOT_LOGGED_IN_JSON))
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    with pytest.raises(ModelAuthenticationFailed) as exc_info:
        llm.call([{"role": "user", "content": "hi"}])
    assert exc_info.value.level is Level.FATAL
    # The CLI's login-error text must not surface verbatim.
    assert "Not logged in" not in str(exc_info.value)


def test_claude_code_unparseable_json_is_recoverable() -> None:
    runner = _RecordingRunner(result=_StubCompleted(0, "not json at all {{{"))
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    with pytest.raises(ModelServerError) as exc_info:
        llm.call([{"role": "user", "content": "hi"}])
    assert exc_info.value.level is Level.RECOVERABLE
    assert "not json at all" not in str(exc_info.value)


def test_claude_code_rendered_prompt_never_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure must not leak the rendered prompt into the error surface."""
    prompt_canary = "USER-SECRET-PROMPT-CANARY-9271"
    runner = _RecordingRunner(result=_StubCompleted(1, "crash"))
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    with pytest.raises(PlatformError) as exc_info:
        llm.call([{"role": "user", "content": prompt_canary}])
    serialized = json.dumps(exc_info.value.to_dict())
    assert prompt_canary not in serialized
    assert prompt_canary not in str(exc_info.value)


# ===========================================================================
# validate_credentials — missing binary → ok=False (no live probe reached)
# ===========================================================================


async def test_validate_credentials_missing_binary_returns_not_ok() -> None:
    connector = ClaudeCodeConnector(runner=_RecordingRunner())
    result = await connector.validate_credentials(
        Credentials(values={"binary_path": "/nonexistent/definitely/not/claude"}),
        None,
    )
    assert result.ok is False
    assert result.reason is not None


async def test_validate_credentials_success_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logged-in probe (stubbed) reports ok=True without shelling live."""
    # Point the connector at a binary shutil.which resolves; stub the run.
    monkeypatch.setattr(
        "llm.providers.claude_code.shutil.which",
        lambda _binary: "/opt/homebrew/bin/claude",
    )
    runner = _RecordingRunner(result=_StubCompleted(0, _SUCCESS_JSON))
    connector = ClaudeCodeConnector(runner=runner)
    result = await connector.validate_credentials(Credentials(values={}), None)
    assert result.ok is True


async def test_validate_credentials_not_logged_in_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logged-out probe result reports ok=False with a login hint."""
    monkeypatch.setattr(
        "llm.providers.claude_code.shutil.which",
        lambda _binary: "/opt/homebrew/bin/claude",
    )
    runner = _RecordingRunner(result=_StubCompleted(0, _NOT_LOGGED_IN_JSON))
    connector = ClaudeCodeConnector(runner=runner)
    result = await connector.validate_credentials(Credentials(values={}), None)
    assert result.ok is False
    assert "auth login" in (result.reason or "")


async def test_validate_credentials_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "llm.providers.claude_code.shutil.which",
        lambda _binary: "/opt/homebrew/bin/claude",
    )
    runner = _RecordingRunner(
        raises=subprocess.TimeoutExpired(cmd=["claude"], timeout=60.0)
    )
    connector = ClaudeCodeConnector(runner=runner)
    result = await connector.validate_credentials(Credentials(values={}), None)
    assert result.ok is False
    assert "timed out" in (result.reason or "")


async def test_validate_credentials_probe_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "llm.providers.claude_code.shutil.which",
        lambda _binary: "/opt/homebrew/bin/claude",
    )
    runner = _RecordingRunner(result=_StubCompleted(1, "crash"))
    connector = ClaudeCodeConnector(runner=runner)
    result = await connector.validate_credentials(Credentials(values={}), None)
    assert result.ok is False
    assert "failed" in (result.reason or "")


async def test_validate_credentials_probe_binary_vanished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """which() resolves but the runner then raises FileNotFoundError."""
    monkeypatch.setattr(
        "llm.providers.claude_code.shutil.which",
        lambda _binary: "/opt/homebrew/bin/claude",
    )
    runner = _RecordingRunner(raises=FileNotFoundError("claude"))
    connector = ClaudeCodeConnector(runner=runner)
    result = await connector.validate_credentials(Credentials(values={}), None)
    assert result.ok is False


# ===========================================================================
# Message rendering — content-block lists and non-dict payloads
# ===========================================================================


def test_claude_code_renders_content_block_list() -> None:
    """A list-of-blocks content (Anthropic shape) is flattened to text."""
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    llm.call(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first block"},
                    {"type": "text", "text": "second block"},
                    "bare string block",
                ],
            }
        ]
    )
    argv = runner.argv
    assert argv is not None
    body = argv[-1]
    assert "first block" in body
    assert "second block" in body
    assert "bare string block" in body


def test_claude_code_non_dict_json_payload_is_recoverable() -> None:
    """A JSON array (not an object) on stdout → Recoverable server error."""
    runner = _RecordingRunner(result=_StubCompleted(0, "[1, 2, 3]"))
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    with pytest.raises(ModelServerError) as exc_info:
        llm.call([{"role": "user", "content": "hi"}])
    assert exc_info.value.level is Level.RECOVERABLE


def test_claude_code_no_system_message_omits_append_flag() -> None:
    """With no system message, `--append-system-prompt` is not passed."""
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    llm.call([{"role": "user", "content": "hi"}])
    assert runner.argv is not None
    assert "--append-system-prompt" not in runner.argv


def test_claude_code_is_error_not_login_is_recoverable() -> None:
    """An `is_error` result that is NOT a login error → Recoverable."""
    upstream = json.dumps(
        {"is_error": True, "subtype": "error", "result": "upstream overloaded"}
    )
    runner = _RecordingRunner(result=_StubCompleted(0, upstream))
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    with pytest.raises(ModelServerError) as exc_info:
        llm.call([{"role": "user", "content": "hi"}])
    assert exc_info.value.level is Level.RECOVERABLE
    assert "upstream overloaded" not in str(exc_info.value)


def test_claude_code_none_content_renders_empty() -> None:
    """A message with content=None renders without crashing."""
    runner = _RecordingRunner()
    llm = _ClaudeCodeLLM(model="sonnet", runner=runner)
    out = llm.call([{"role": "user", "content": None}])
    assert out == "hello from claude"

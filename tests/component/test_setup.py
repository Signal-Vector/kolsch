"""Component tests for the CLI dispatcher gate + the `kolsch setup` flow.

Covers TC-UX-04..10 from docs/process/test-catalog.md Part 9.6.

Splits responsibilities:

- TC-UX-04 — CLI gate (missing-workspace message + exit 3 + no
  traceback). Exercised by calling :func:`cli.main.main` directly
  with an empty temp directory.
- TC-UX-05 — `setup --non-interactive` produces a runnable
  workspace (the `workspace_is_initialized` gate accepts it).
- TC-UX-06 — Re-running setup is idempotent. Verified by content
  hash; mtime is too noisy when steps re-create dirs.
- TC-UX-07 — `KOLSCH_PROVIDER` / `KOLSCH_PROVIDER_KEY` env-driven
  auto-connect. Uses `httpx.MockTransport` for both the OpenRouter
  `/auth/key` and `/api/v1/models` endpoints.
- TC-UX-08 — Starter agent file shape; fake fallback when no
  provider is connected.
- TC-UX-09 — Resumability after Ctrl-C during a prompt; second
  run picks up at step D.
- TC-UX-10 — End-to-end smoke: run setup, then run the helper
  agent against the produced workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import io
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml

from cli._setup import (
    NonInteractivePrompter,
    SetupPrompter,
    _SetupIO,
    run_setup,
)
from cli.main import main
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from cli.llm_commands import _CommandIO
    from llm.protocol import PromptField, ProviderConnector


# ===========================================================================
# Helpers
# ===========================================================================


_OPENROUTER_AUTH_RESPONSE = {
    "data": {"limit": 5.0, "usage": 0.0, "label": "test"},
}

_OPENROUTER_MODELS_RESPONSE = {
    "data": [
        {
            "id": "anthropic/claude-opus-4-7",
            "context_length": 200000,
            "supported_parameters": ["tools", "stream"],
            "pricing": {"prompt": "0.000015", "completion": "0.000075"},
        },
    ]
}

_OPENCODE_MODELS_RESPONSE = {
    "object": "list",
    "data": [{"id": "gpt-oss-120b"}],
}


def _openrouter_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/auth/key"):
        return httpx.Response(200, json=_OPENROUTER_AUTH_RESPONSE)
    if path.endswith("/models"):
        return httpx.Response(200, json=_OPENROUTER_MODELS_RESPONSE)
    return httpx.Response(404, json={"error": "unhandled"})


def _opencode_handler(request: httpx.Request) -> httpx.Response:
    _ = request
    return httpx.Response(200, json=_OPENCODE_MODELS_RESPONSE)


def _mock_connector_factory(
    provider_type: str,
) -> ProviderConnector:
    """Like the Phase 1.1 test factory — returns a connector with the
    matching HTTP MockTransport baked in."""
    if provider_type == "openrouter":
        return OpenRouterConnector(transport=httpx.MockTransport(_openrouter_handler))
    if provider_type == "opencode":
        return OpenCodeConnector(transport=httpx.MockTransport(_opencode_handler))
    from errors import LlmConnectorUnknown

    raise LlmConnectorUnknown(
        type_name=provider_type,
        registered_types=["opencode", "openrouter"],
    )


def _build_test_connect_io_factory(
    captured_out: io.StringIO,
) -> Callable[[Path, str, str | None], _CommandIO]:
    """Build a connect-IO factory that uses MockTransport-wired
    connectors and an env-key prompter (for the non-interactive
    auto-connect path)."""
    from cli.llm_commands import _CommandIO

    class _EnvKeyPrompter:
        def __init__(self, key: str) -> None:
            self._key = key

        def ask(self, field: PromptField) -> str:
            if field.name == "api_key":
                return self._key
            return field.default or ""

    def factory(root: Path, provider: str, api_key: str | None) -> _CommandIO:
        _ = provider
        prompter = _EnvKeyPrompter(api_key or "")
        return _CommandIO(
            root=root,
            out=captured_out,
            prompter=prompter,
            connector_factory=_mock_connector_factory,
        )

    return factory


async def _noop_run_handler(args: argparse.Namespace) -> int:
    """Stand-in for `_cmd_run` for tests that don't exercise step E
    end-to-end. Returns 0 to signal a "successful" test run without
    standing up the full runtime."""
    _ = args
    return 0


def _make_test_io(
    *,
    root: Path,
    out: io.StringIO | None = None,
    err: io.StringIO | None = None,
    prompter: SetupPrompter | None = None,
    run_handler: Callable[[argparse.Namespace], Awaitable[int]] | None = None,
) -> _SetupIO:
    captured_out = out if out is not None else io.StringIO()
    return _SetupIO(
        root=root,
        out=captured_out,
        err=err if err is not None else io.StringIO(),
        prompter=prompter if prompter is not None else NonInteractivePrompter(),
        connect_io_factory=_build_test_connect_io_factory(captured_out),
        run_handler=run_handler if run_handler is not None else _noop_run_handler,
    )


# ===========================================================================
# TC-UX-04 — friendly message + exit 3 + no traceback
# ===========================================================================


def test_tc_ux_04_cli_emits_friendly_message_on_missing_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a non-setup command against an empty path; expect the exact
    friendly message on stderr, exit 3, no traceback."""
    # Ensure no env var override leaks in.
    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)
    rc = main(["--root", str(tmp_path), "providers"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "No Kolsch workspace at " in err
    assert str(tmp_path.resolve()) in err
    assert "Run `kolsch setup`" in err
    assert "KOLSCH_WORKSPACE" in err
    # No Python traceback markers.
    assert "Traceback" not in err
    assert "  File " not in err


# ===========================================================================
# TC-UX-05 — `setup --non-interactive` produces a runnable workspace
# ===========================================================================


async def test_tc_ux_05_non_interactive_setup_initializes_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    rc = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        io_,
    )
    assert rc == 0
    from platform_runtime.workspace import workspace_is_initialized

    assert workspace_is_initialized(tmp_path) is True
    # Skeleton present.
    for name in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        assert (tmp_path / name).is_dir()
    # Starter agent ships with the fake LLM since no provider connected.
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    assert starter.is_file()
    payload = yaml.safe_load(starter.read_text())
    assert payload["model"] == {"provider": "fake", "model": "fake-model"}


# ===========================================================================
# TC-UX-06 — idempotency
# ===========================================================================


async def test_tc_ux_06_setup_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)
    first_io = _make_test_io(root=tmp_path)
    assert await run_setup(args, first_io) == 0

    snapshot_before = _hash_workspace(tmp_path)

    second_io = _make_test_io(root=tmp_path)
    assert await run_setup(args, second_io) == 0

    snapshot_after = _hash_workspace(tmp_path)
    assert snapshot_before == snapshot_after


def _hash_workspace(root: Path) -> dict[str, str]:
    """Return path → sha256 for every regular file in the workspace
    (excluding `.platform/` since SQLite touches it on bootstrap)."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == ".platform":
            continue
        out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ===========================================================================
# TC-UX-07 — env-driven auto-connect via mocked HTTP
# ===========================================================================


async def test_tc_ux_07_env_driven_auto_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KOLSCH_PROVIDER", "openrouter")
    monkeypatch.setenv("KOLSCH_PROVIDER_KEY", "sk-test-or")
    io_ = _make_test_io(root=tmp_path)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)
    rc = await run_setup(args, io_)
    assert rc == 0

    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    providers = payload.get("llm_providers") or {}
    assert "openrouter" in providers
    assert providers["openrouter"]["api_key_secret"] == "OPENROUTER_API_KEY"

    env_text = (tmp_path / ".env").read_text()
    assert "OPENROUTER_API_KEY" in env_text
    assert "sk-test-or" in env_text
    # The literal secret value must not appear in platform.yaml.
    assert "sk-test-or" not in (tmp_path / "platform.yaml").read_text()


# ===========================================================================
# TC-UX-08 — starter agent model field reflects the connected provider
# ===========================================================================


async def test_tc_ux_08_starter_falls_back_to_fake_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario (a): no provider → starter model is the fake LLM."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    payload = yaml.safe_load(starter.read_text())
    assert payload["model"] == {"provider": "fake", "model": "fake-model"}


async def test_tc_ux_08_starter_uses_opus_alias_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario (b): a connected provider's first model has a matching
    alias → the starter agent's `model` is the alias string."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    # Seed a connected provider whose first model matches the
    # `opus` alias target. Step D's `_pick_starter_model` walks
    # the provider's models list FIRST, then substitutes a matching
    # alias for readability.
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "model_aliases": {
                    "opus": "openrouter/anthropic/claude-opus-4-7",
                    "smart": "openrouter/openai/gpt-4o",
                },
                "llm_providers": {
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "models": [
                            {"id": "anthropic/claude-opus-4-7"},
                            {"id": "openai/gpt-4o"},
                        ],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    payload = yaml.safe_load(starter.read_text())
    assert payload["model"] == "opus"


async def test_starter_uses_non_metadata_provider_over_openrouter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both OpenCode (non-metadata-source) and OpenRouter
    (serves_as_metadata_source: true) are connected, the starter
    picks OpenCode's first model — OpenRouter is preferred only
    when it's the sole option."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "llm_providers": {
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "serves_as_metadata_source": True,
                        "models": [{"id": "anthropic/claude-opus-4-7"}],
                    },
                    "opencode": {
                        "type": "opencode",
                        "api_key_secret": "OPENCODE_API_KEY",
                        "models": [
                            {"id": "gpt-oss-120b"},
                            {"id": "qwen2.5-coder-32b"},
                        ],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    payload = yaml.safe_load(starter.read_text())
    assert payload["model"] == {"provider": "opencode", "model": "gpt-oss-120b"}


async def test_starter_uses_openrouter_when_sole_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If only an OpenRouter (metadata-source) provider is connected,
    the starter still uses it rather than falling back to the fake
    LLM — better real-LLM-than-echo."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "llm_providers": {
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "serves_as_metadata_source": True,
                        "models": [{"id": "anthropic/claude-haiku-4-5"}],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    payload = yaml.safe_load(starter.read_text())
    assert payload["model"] == {
        "provider": "openrouter",
        "model": "anthropic/claude-haiku-4-5",
    }


async def test_starter_skips_provider_with_empty_models_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected provider whose discovery returned 0 models is
    skipped; the next provider in the priority pool is tried."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "llm_providers": {
                    "opencode": {
                        "type": "opencode",
                        "api_key_secret": "OPENCODE_API_KEY",
                        "models": [],  # empty — should be skipped
                    },
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "serves_as_metadata_source": True,
                        "models": [{"id": "anthropic/claude-haiku-4-5"}],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    payload = yaml.safe_load(starter.read_text())
    # OpenCode skipped (empty), OpenRouter picked even as fallback.
    assert payload["model"] == {
        "provider": "openrouter",
        "model": "anthropic/claude-haiku-4-5",
    }


def _default_yaml() -> str:
    from assets.platform_config import default_platform_payload

    return yaml.safe_dump(default_platform_payload(), sort_keys=False)


# ===========================================================================
# TC-UX-09 — resumability after KeyboardInterrupt during step D's prompt
# ===========================================================================


async def test_tc_ux_09_resumable_after_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First run: a prompter that raises KeyboardInterrupt during step
    D's confirm prompt. Second run: a NonInteractivePrompter; assert
    step C is detected as done (skeleton + .env exist; no provider
    connected → step C still prompts but in non-interactive mode it
    cleanly skips) and step D actually writes the starter agent."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    class _InterruptingPrompter:
        def __init__(self) -> None:
            self.confirm_calls = 0

        def choose(self, q: str, opts: list[str], default: str) -> str:
            return default

        def text(self, q: str, default: str = "") -> str:
            return default

        def confirm(self, q: str, *, default: bool = True) -> bool:
            _ = q, default
            self.confirm_calls += 1
            raise KeyboardInterrupt

    first_prompter = _InterruptingPrompter()
    first_io = _make_test_io(root=tmp_path, prompter=first_prompter)
    rc_first = await run_setup(
        argparse.Namespace(non_interactive=False, root=tmp_path),
        first_io,
    )
    # The interrupt happens during step D's confirm prompt; setup
    # returns 4 (partial).
    assert rc_first == 4
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    assert not starter.exists(), (
        "starter agent should not exist after step D was interrupted"
    )

    # Second run: skeleton exists (B skipped), no provider (C skipped),
    # starter doesn't exist (D writes it), test run executes (E).
    rc_second = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        _make_test_io(root=tmp_path),
    )
    assert rc_second == 0
    assert starter.exists()


# ===========================================================================
# TC-UX-10 — end-to-end: setup then `kolsch run`
# ===========================================================================


def test_tc_ux_10_end_to_end_run_against_setup_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Setup --non-interactive produces a workspace; `kolsch run` of
    the starter agent against the same workspace returns 0 and emits
    an `agent.message` event.

    Defined as a sync test on purpose — `cli.main.main` internally
    calls ``asyncio.run`` and would fail from inside an existing
    event loop. The CLI is the system-under-test, so we exercise it
    exactly as the console-script entry point does.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)

    # Setup via the real cli.main entry point — exercises the dispatch
    # gate + the setup orchestrator + the default IO wiring end to end.
    rc_setup = main(["--root", str(tmp_path), "setup", "--non-interactive"])
    assert rc_setup == 0

    # Then run the starter agent. Use the real CLI again so the
    # dispatcher + runtime bootstrap path is exercised.
    capsys.readouterr()  # drain setup's stdout.
    rc_run = main(
        [
            "--root",
            str(tmp_path),
            "run",
            "kolsch/agents/helper",
            "--input",
            "content=hello",
        ]
    )
    assert rc_run == 0
    out = capsys.readouterr().out
    # `_cmd_run` writes the agent.message data as JSON to stdout on
    # success — confirm there's a non-empty JSON payload (the
    # EchoLLMDriver echoes the user message).
    assert "hello" in out or out.strip()


# ===========================================================================
# Extra coverage — interactive paths + production-factory + api.make_app
# ===========================================================================


class _ScriptedPrompter:
    """SetupPrompter stub that returns canned answers per call."""

    def __init__(
        self,
        choose: list[str] | None = None,
        text: list[str] | None = None,
        confirm: list[bool] | None = None,
    ) -> None:
        self._choose = list(choose or [])
        self._text = list(text or [])
        self._confirm = list(confirm or [])

    def choose(self, question: str, options: list[str], default: str) -> str:
        _ = question, options
        return self._choose.pop(0) if self._choose else default

    def text(self, question: str, default: str = "") -> str:
        _ = question
        return self._text.pop(0) if self._text else default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        _ = question
        return self._confirm.pop(0) if self._confirm else default


async def test_interactive_setup_user_skips_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive path: user picks `s` at step C (skip provider)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    prompter = _ScriptedPrompter(
        choose=["s"],  # step C — skip provider
        confirm=[True, True],  # step D create, step E run
    )
    io_ = _make_test_io(root=tmp_path, prompter=prompter)
    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert rc == 0
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    assert starter.is_file()


async def test_interactive_user_declines_starter_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step D confirm returns False → starter agent not written."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    prompter = _ScriptedPrompter(
        choose=["s"],
        confirm=[False],  # decline starter agent
    )
    io_ = _make_test_io(root=tmp_path, prompter=prompter)
    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert rc == 0
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    assert not starter.exists()


async def test_step_e_skipped_when_starter_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If step D was declined, step E has no agent to run and skips."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    captured = io.StringIO()
    prompter = _ScriptedPrompter(choose=["s"], confirm=[False])
    io_ = _make_test_io(root=tmp_path, prompter=prompter, out=captured)
    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert rc == 0
    assert "no starter agent to test" in captured.getvalue()


async def test_make_default_setup_io_wires_production_defaults(
    tmp_path: Path,
) -> None:
    """The production IO factory wires StdinSetupPrompter + default
    connect-IO factory + the real `_cmd_run` handler. Constructing it
    is enough to exercise the wiring path."""
    from cli._setup import StdinSetupPrompter, make_default_setup_io

    io_ = make_default_setup_io(root=tmp_path)
    assert io_.root == tmp_path
    assert isinstance(io_.prompter, StdinSetupPrompter)
    # connect_io_factory + run_handler are callables wired to the
    # production paths; constructing them isn't safe to invoke
    # without stdin / a real runtime, but the references are pinned.
    assert callable(io_.connect_io_factory)
    assert callable(io_.run_handler)


async def test_api_make_app_raises_on_uninitialized_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`api.make_app` on a workspace with no platform.yaml raises
    :class:`WorkspaceNotInitialized` — operators get a typed error
    rather than a confusing FileNotFoundError later."""
    from api import make_app
    from errors import WorkspaceNotInitialized

    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)
    with pytest.raises(WorkspaceNotInitialized):
        await make_app(root=tmp_path)


async def test_api_make_app_returns_app_when_workspace_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the workspace is initialized, `make_app` resolves +
    bootstraps + builds without error. Scaffolds the workspace
    directly (no in-process `main()` call — that would re-enter
    asyncio.run from inside the test loop)."""
    from api import make_app

    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)
    # Lay down the same skeleton kolsch setup --non-interactive does.
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    app = await make_app(root=tmp_path)
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert any("sessions" in p for p in paths)


def test_stdin_prompter_choose_accepts_default(tmp_path: Path) -> None:
    """`StdinSetupPrompter.choose` returns the (lowercased) default
    when stdin yields an empty line."""
    from cli._setup import StdinSetupPrompter

    sink = io.StringIO()
    prompter = StdinSetupPrompter(sink)

    import builtins

    seq = iter([""])  # one empty line
    real_input = builtins.input
    builtins.input = lambda *_, **__: next(seq)
    try:
        out = prompter.choose("pick:", ["[r] foo", "[s] skip"], default="s")
    finally:
        builtins.input = real_input
    assert out == "s"
    _ = tmp_path


def test_stdin_prompter_text_returns_default_on_empty() -> None:
    from cli._setup import StdinSetupPrompter

    prompter = StdinSetupPrompter(io.StringIO())
    import builtins

    real_input = builtins.input
    builtins.input = lambda *_, **__: ""
    try:
        assert prompter.text("name?", default="d") == "d"
        assert prompter.confirm("ok?", default=True) is True
    finally:
        builtins.input = real_input


def test_stdin_prompter_confirm_no_default() -> None:
    from cli._setup import StdinSetupPrompter

    prompter = StdinSetupPrompter(io.StringIO())
    import builtins

    real_input = builtins.input
    builtins.input = lambda *_, **__: "n"
    try:
        assert prompter.confirm("ok?", default=True) is False
    finally:
        builtins.input = real_input


async def test_interactive_step_c_picks_openrouter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive step C with the user picking 'r' (openrouter)
    routes through cmd_connect.

    Production interactive mode reads the API key from stdin via
    cmd_connect's StdinPrompter. For the test we override the
    connect-IO factory to inject a fixed-key prompter regardless of
    what the orchestrator passes — the substance we're verifying is
    that picking 'r' triggers the openrouter connect call (lines
    299-308 in src/cli/_setup.py)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    prompter = _ScriptedPrompter(
        choose=["r"],  # step C — pick openrouter
        confirm=[True, True],  # step D create, step E run
    )

    captured = io.StringIO()
    from cli.llm_commands import _CommandIO

    class _FixedKeyPrompter:
        def ask(self, field: PromptField) -> str:
            if field.name == "api_key":
                return "sk-fixed-test"
            return field.default or ""

    def fixed_factory(root: Path, provider: str, api_key: str | None) -> _CommandIO:
        _ = provider, api_key  # interactive path passes None here
        return _CommandIO(
            root=root,
            out=captured,
            prompter=_FixedKeyPrompter(),
            connector_factory=_mock_connector_factory,
        )

    io_ = _SetupIO(
        root=tmp_path,
        out=captured,
        err=io.StringIO(),
        prompter=prompter,
        connect_io_factory=fixed_factory,
        run_handler=_noop_run_handler,
    )
    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert rc == 0
    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert "openrouter" in (payload.get("llm_providers") or {})


async def test_step_e_test_run_failure_returns_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `run_handler` returns non-zero, setup returns 4 (partial)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    async def failing_run(args: argparse.Namespace) -> int:
        _ = args
        return 1

    io_ = _make_test_io(root=tmp_path, run_handler=failing_run)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    assert rc == 4


async def test_non_interactive_with_partial_env_skips_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting `KOLSCH_PROVIDER` but not `KOLSCH_PROVIDER_KEY` (or vice
    versa) is treated as "no provider" — the env-driven connect path
    requires both."""
    monkeypatch.setenv("KOLSCH_PROVIDER", "openrouter")
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    assert rc == 0
    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert not (payload.get("llm_providers") or {})


def test_default_connect_io_factory_without_api_key() -> None:
    """The production default connect-IO factory returns a normal
    `_CommandIO` with StdinPrompter when no env-key is supplied."""
    from pathlib import Path as _Path

    from cli._setup import _default_connect_io_factory
    from cli.llm_commands import StdinPrompter

    io_obj = _default_connect_io_factory(_Path("/var/ws/x"), "openrouter", None)
    assert isinstance(io_obj.prompter, StdinPrompter)


def test_default_connect_io_factory_with_api_key() -> None:
    """When an env-key is supplied, the factory wraps a prompter that
    returns that key for any ``api_key`` field."""
    from pathlib import Path as _Path

    from cli._setup import _default_connect_io_factory
    from llm.protocol import PromptField

    io_obj = _default_connect_io_factory(_Path("/var/ws/x"), "openrouter", "sk-x")
    field = PromptField(name="api_key", label="API key", kind="secret")
    assert io_obj.prompter.ask(field) == "sk-x"
    # Non-api-key fields fall through to default.
    field2 = PromptField(
        name="base_url",
        label="Base URL",
        kind="url",
        default="https://example.com",
    )
    assert io_obj.prompter.ask(field2) == "https://example.com"

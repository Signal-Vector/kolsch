"""Credential resolution + workspace bootstrap for the console e2e suite.

See the module docstring on ``test_console_smoke.py`` for the full
setup instructions a developer needs before these tests do anything
other than skip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from cli.chat.session import _DEFAULT_AGENT_REF

if TYPE_CHECKING:
    from pathlib import Path

_PROVIDER_ENV = "E2E_LLM_PROVIDER"
_API_KEY_ENV = "E2E_LLM_API_KEY"
_KNOWN_PROVIDERS = ("claude_code", "openrouter", "opencode")
_SETUP_TIMEOUT_SECONDS = 60
_CLI_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class _ConsoleCredentials:
    provider: str
    key: str


@dataclass(frozen=True)
class ConsoleWorkspace:
    """A bootstrapped workspace + the provider it's wired to."""

    root: Path
    provider: str
    agent_ref: str


def _resolve_console_credentials() -> _ConsoleCredentials:
    """Resolve provider + key from env, or skip the whole test.

    Reuses the exact env var names ``tests/e2e/test_e2e.py`` already
    established (``E2E_LLM_PROVIDER`` / ``E2E_LLM_API_KEY``) so a
    developer sets credentials once for both e2e tiers.
    """
    provider = os.environ.get(_PROVIDER_ENV, "").strip()
    key = os.environ.get(_API_KEY_ENV, "").strip()
    if not provider or not key:
        pytest.skip(
            f"console e2e suite requires {_PROVIDER_ENV} + {_API_KEY_ENV}. "
            f"For claude_code, {_API_KEY_ENV} can be any non-empty "
            f"placeholder (subscription-authenticated via `claude auth "
            f"login`, not an API key). Example: "
            f"{_PROVIDER_ENV}=claude_code {_API_KEY_ENV}=unused "
            f"pytest -m e2e tests/e2e/console/"
        )
    if provider not in _KNOWN_PROVIDERS:
        pytest.skip(f"{_PROVIDER_ENV}={provider!r} is not one of {_KNOWN_PROVIDERS}")
    if provider == "claude_code" and shutil.which("claude") is None:
        pytest.skip("claude_code selected but `claude` binary not on PATH")
    return _ConsoleCredentials(provider=provider, key=key)


@pytest.fixture
def console_workspace(tmp_path: Path) -> ConsoleWorkspace:
    """A workspace bootstrapped via the real ``kolsch setup --non-interactive``.

    Shells out to the actual setup command with ``KOLSCH_PROVIDER`` /
    ``KOLSCH_PROVIDER_KEY`` pointed at the resolved real provider. This
    both wires up a genuinely connected provider through the real
    ``connect`` code path (``src/cli/llm_commands.py:cmd_connect``) and
    materializes the starter ``kolsch/agents/helper`` agent
    (``src/cli/_setup.py:_step_d_starter_agent``) wired to it — no
    hand-authored ``platform.yaml`` needed.
    """
    creds = _resolve_console_credentials()
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(tmp_path)
    env["KOLSCH_PROVIDER"] = creds.provider
    env["KOLSCH_PROVIDER_KEY"] = creds.key
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=_SETUP_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (
        f"`kolsch setup --non-interactive` failed:\n"
        f"stdout=\n{result.stdout}\nstderr=\n{result.stderr}"
    )
    return ConsoleWorkspace(
        root=tmp_path, provider=creds.provider, agent_ref=_DEFAULT_AGENT_REF
    )


def run_cli(
    workspace: ConsoleWorkspace, *args: str, timeout: int = _CLI_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run ``kolsch <args>`` as a plain subprocess against ``workspace``.

    No pty, no LLM call — for the CLI subcommands that are pure local
    dispatch (``sessions``, ``workflows``, ``skills``, ``providers``,
    ``refresh``, ``disconnect``, ``models``). Shared by every scenario
    that needs a cheap out-of-band check rather than driving the
    interactive console.
    """
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(workspace.root)
    return subprocess.run(
        [sys.executable, "-m", "cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def discover_model_ref(workspace: ConsoleWorkspace) -> str:
    """Return a valid ``<provider>/<model_id>`` ref for ``/model`` to switch to.

    Shells out to the real ``kolsch models <provider> --json`` (data
    source: `platform.yaml`'s pre-discovered models, populated by the
    `connect` step `console_workspace` already ran) rather than
    hardcoding a model id that may not exist for every provider.
    """
    result = run_cli(workspace, "models", workspace.provider, "--json")
    assert result.returncode == 0, (
        f"`kolsch models {workspace.provider} --json` failed:\n"
        f"stdout=\n{result.stdout}\nstderr=\n{result.stderr}"
    )
    payload = json.loads(result.stdout)
    models = payload[workspace.provider]["models"]
    assert models, f"no discovered models for provider {workspace.provider!r}"
    model_id: str = models[0]["id"]
    return f"{workspace.provider}/{model_id}"


def latest_session_id(workspace: ConsoleWorkspace) -> str:
    """Return the (only) session id in a freshly bootstrapped workspace.

    Parses ``kolsch sessions list``'s table (``SESSION_ID`` is the
    first column, full — not truncated — id) rather than scraping the
    truncated 8-char id the chat UI's info bar renders, since the
    truncated form isn't guaranteed to round-trip through ``--session``.
    """
    result = run_cli(workspace, "sessions", "list")
    assert result.returncode == 0, (
        f"`kolsch sessions list` failed:\nstdout=\n{result.stdout}\n"
        f"stderr=\n{result.stderr}"
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) >= 2, f"expected a header + at least one session row:\n{lines}"
    return lines[1].split()[0]

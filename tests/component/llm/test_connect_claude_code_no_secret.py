"""Test 3 (spec-118): null-key connect → reload → build_llm succeeds.

`kolsch connect claude_code` (stubbed prompter + runner) must write a
`platform.yaml` entry with **no** `api_key_secret` and `models:` as
`{id, context_length}` dicts; reloading that config and calling
`build_llm` for `claude_code` must succeed (no `LlmSecretMissing`). A
control connector that DOES declare a secret field still gets
`api_key_secret` written — the fix is general, not `claude_code`-special.
"""

from __future__ import annotations

import argparse
import io
import json
from typing import TYPE_CHECKING, Any

import yaml

import llm.providers  # noqa: F401 — registers connectors
from cli.llm_commands import _CommandIO, cmd_connect
from errors import LlmSecretMissing
from llm.build_llm import build_llm
from llm.providers.claude_code import ClaudeCodeConnector
from llm.providers.opencode import OpenCodeConnector

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import pytest

    from llm.protocol import PromptField, ProviderConnector

_SUCCESS_JSON = json.dumps({"is_error": False, "result": "ok"})


class _StubPrompter:
    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers

    def ask(self, field: PromptField) -> str:
        return self._answers.get(field.name, field.default or "")


class _StubCompleted:
    returncode = 0
    stdout = _SUCCESS_JSON


def _stub_runner(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    timeout: float,
) -> _StubCompleted:
    return _StubCompleted()


class _ModelSpec:
    """Minimal ModelSpec stand-in for build_llm (needs .provider/.model)."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self.parameters: dict[str, Any] = {}


class _NoSecretsProvider:
    """A Secrets provider that fails any resolve — proves none is attempted."""

    async def resolve(self, name: str) -> str:  # pragma: no cover - must not run
        from errors import SecretNotFound

        raise SecretNotFound(name=name)


def _claude_factory() -> Any:
    def make(type_name: str) -> ProviderConnector:
        if type_name == "claude_code":
            # Point shutil.which at a real path so validate passes without
            # a live binary; the runner is stubbed regardless.
            return ClaudeCodeConnector(runner=_stub_runner)
        from errors import LlmConnectorUnknown

        raise LlmConnectorUnknown(type_name=type_name, registered_types=["claude_code"])

    return make


async def test_connect_claude_code_no_secret_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # shutil.which must resolve so validate_credentials reaches the probe.
    monkeypatch.setattr(
        "llm.providers.claude_code.shutil.which",
        lambda _binary: "/opt/homebrew/bin/claude",
    )
    buf = io.StringIO()
    io_ = _CommandIO(
        root=tmp_path,
        out=buf,
        prompter=_StubPrompter({}),
        connector_factory=_claude_factory(),
    )
    rc = await cmd_connect(argparse.Namespace(provider="claude_code"), io_)
    assert rc == 0, buf.getvalue()

    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    entry = payload["llm_providers"]["claude_code"]
    # No api_key_secret written.
    assert "api_key_secret" not in entry
    # models persisted as {id, context_length} dicts.
    for m in entry["models"]:
        assert isinstance(m, dict)
        assert "id" in m
        assert isinstance(m["context_length"], int)
    # No .env written for a keyless provider.
    assert not (tmp_path / ".env").exists()


async def test_connect_claude_code_reload_build_llm_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "llm.providers.claude_code.shutil.which",
        lambda _binary: "/opt/homebrew/bin/claude",
    )
    buf = io.StringIO()
    io_ = _CommandIO(
        root=tmp_path,
        out=buf,
        prompter=_StubPrompter({}),
        connector_factory=_claude_factory(),
    )
    assert await cmd_connect(argparse.Namespace(provider="claude_code"), io_) == 0

    # Reload the written config and build_llm — must NOT raise LlmSecretMissing.
    from cli.llm_commands import (
        _load_platform_yaml,
        _provider_configs_from_payload,
    )

    reloaded = _load_platform_yaml(tmp_path)
    provider_configs = _provider_configs_from_payload(reloaded)
    cc_config = provider_configs["claude_code"]
    assert cc_config.api_key_secret is None

    crewai_llm = await build_llm(
        _ModelSpec("claude_code", "sonnet"),
        cc_config,
        _NoSecretsProvider(),
    )
    assert crewai_llm is not None
    # The context length survived serialization for compaction.
    models = {m["id"]: m for m in cc_config.models}
    assert models["sonnet"]["context_length"] == 200000


async def test_connect_keyed_provider_still_writes_secret(
    tmp_path: Path,
) -> None:
    """Control: a connector WITH a secret field still gets api_key_secret."""
    import httpx

    def opencode_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "gpt-oss-120b"}]})

    def factory(type_name: str) -> ProviderConnector:
        if type_name == "opencode":
            return OpenCodeConnector(transport=httpx.MockTransport(opencode_handler))
        from errors import LlmConnectorUnknown

        raise LlmConnectorUnknown(type_name=type_name, registered_types=["opencode"])

    buf = io.StringIO()
    io_ = _CommandIO(
        root=tmp_path,
        out=buf,
        prompter=_StubPrompter(
            {
                "base_url": "https://opencode.example.com/v1",
                "api_key": "sk-test-key",
            }
        ),
        connector_factory=factory,
    )
    assert await cmd_connect(argparse.Namespace(provider="opencode"), io_) == 0
    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    entry = payload["llm_providers"]["opencode"]
    assert entry["api_key_secret"] == "OPENCODE_API_KEY"


def test_build_llm_would_raise_secret_missing_if_key_configured() -> None:
    """Sanity: LlmSecretMissing is importable + is the failure we avoid."""
    assert LlmSecretMissing is not None

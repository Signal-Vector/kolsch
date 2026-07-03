"""Integration tests for `kolsch connect / disconnect / providers / refresh`.

Pins TC-LLM-07, TC-LLM-08, TC-LLM-10 from the test catalog Part 9.5.

These exercise the CLI command functions DIRECTLY (no subprocess) by
injecting:

- a stub :class:`Prompter` that returns canned answers,
- a stub ``connector_factory`` that hands back connectors carrying a
  :class:`httpx.MockTransport`,
- a writable temp platform root.

After each flow the on-disk `platform.yaml` and `.env` are inspected
to confirm: secrets stayed out of platform.yaml; models were augmented
when a metadata source was available; the augmentation step is silent
when no source is connected.
"""

from __future__ import annotations

import argparse
import io
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml

from cli.llm_commands import (
    Prompter,
    _CommandIO,
    cmd_connect,
    cmd_disconnect,
    cmd_providers,
    cmd_refresh,
)
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from llm.protocol import PromptField, ProviderConnector


# ===========================================================================
# Helpers
# ===========================================================================


_OPENCODE_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "gpt-oss-120b"},
        {"id": "qwen2.5-coder-32b"},
    ],
}

_OPENROUTER_PAYLOAD = {
    "data": [
        {
            "id": "opencode/gpt-oss-120b",
            "context_length": 131072,
            "supported_parameters": ["tools", "stream"],
            "pricing": {"prompt": "0.0", "completion": "0.0"},
        },
        {
            "id": "anthropic/claude-opus-4-7",
            "context_length": 200000,
            "supported_parameters": ["tools", "stream"],
            "pricing": {"prompt": "0.000015", "completion": "0.000075"},
        },
    ]
}

_OPENROUTER_AUTH = {
    "data": {"limit": 5.0, "usage": 0.0, "label": "test"},
}


class _StubPrompter:
    """Returns canned answers per field name."""

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers

    def ask(self, field: PromptField) -> str:
        return self._answers.get(field.name, field.default or "")


def _opencode_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_OPENCODE_PAYLOAD)


def _openrouter_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/auth/key"):
        return httpx.Response(200, json=_OPENROUTER_AUTH)
    if path.endswith("/models"):
        return httpx.Response(200, json=_OPENROUTER_PAYLOAD)
    return httpx.Response(404, json={"error": "unhandled"})


def _factory(
    opencode_handler: Callable[[httpx.Request], httpx.Response] = _opencode_handler,
    openrouter_handler: Callable[[httpx.Request], httpx.Response] = _openrouter_handler,
) -> Callable[[str], ProviderConnector]:
    """Build a connector factory that injects MockTransports per type."""

    def make(type_name: str) -> ProviderConnector:
        if type_name == "opencode":
            return OpenCodeConnector(transport=httpx.MockTransport(opencode_handler))
        if type_name == "openrouter":
            return OpenRouterConnector(
                transport=httpx.MockTransport(openrouter_handler)
            )
        from errors import LlmConnectorUnknown

        raise LlmConnectorUnknown(
            type_name=type_name, registered_types=["opencode", "openrouter"]
        )

    return make


def _command_io(
    root: Path,
    answers: dict[str, str],
    factory: Callable[[str], ProviderConnector] | None = None,
) -> tuple[_CommandIO, io.StringIO]:
    buf = io.StringIO()
    return (
        _CommandIO(
            root=root,
            out=buf,
            prompter=_StubPrompter(answers),
            connector_factory=factory or _factory(),
        ),
        buf,
    )


def _seed_openrouter_connected(root: Path) -> None:
    """Write a platform.yaml that already lists OpenRouter as connected
    + serves_as_metadata_source: true."""
    payload = {
        "llm_providers": {
            "openrouter": {
                "type": "openrouter",
                "api_key_secret": "OPENROUTER_API_KEY",
                "serves_as_metadata_source": True,
                "models": [],
            }
        }
    }
    (root / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


# ===========================================================================
# TC-LLM-07 — connect flow augments OpenCode discovery with OpenRouter metadata
# ===========================================================================


async def test_tc_llm_07_connect_augments_from_openrouter(
    tmp_path: Path,
) -> None:
    _seed_openrouter_connected(tmp_path)
    answers = {
        "base_url": "https://opencode.example.com/v1",
        "api_key": "sk-test-opencode-canary",
    }
    io_, buf = _command_io(tmp_path, answers)
    args = argparse.Namespace(provider="opencode")
    rc = await cmd_connect(args, io_)
    assert rc == 0

    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    entry = payload["llm_providers"]["opencode"]
    models = {m["id"]: m for m in entry["models"]}
    # gpt-oss-120b has a match in the seeded OpenRouter catalog under
    # opencode/gpt-oss-120b → augmented.
    assert models["gpt-oss-120b"]["context_length"] == 131072
    assert models["gpt-oss-120b"]["supports_tools"] is True
    # qwen2.5-coder-32b has no match → stays bare.
    assert "context_length" not in models["qwen2.5-coder-32b"]
    # Output mentions augmentation.
    assert "Augmenting" in buf.getvalue()


# ===========================================================================
# TC-LLM-08 — connect flow runs cleanly without OpenRouter
# ===========================================================================


async def test_tc_llm_08_connect_without_metadata_source(
    tmp_path: Path,
) -> None:
    # No OpenRouter seed — only OpenCode is being connected.
    answers = {
        "base_url": "https://opencode.example.com/v1",
        "api_key": "sk-test-opencode",
    }
    io_, buf = _command_io(tmp_path, answers)
    args = argparse.Namespace(provider="opencode")
    rc = await cmd_connect(args, io_)
    assert rc == 0

    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    models = payload["llm_providers"]["opencode"]["models"]
    # All entries have just `id`; no parameter fields populated.
    for m in models:
        assert "context_length" not in m
        assert "supports_tools" not in m
    # No augmentation step ran.
    assert "Augmenting" not in buf.getvalue()
    # No warning treating the missing source as failure.
    assert "error" not in buf.getvalue().lower()


# ===========================================================================
# TC-LLM-10 — API key value never appears in platform.yaml or events
# ===========================================================================


async def test_tc_llm_10_secret_value_never_lands_in_platform_yaml(
    tmp_path: Path,
) -> None:
    canary = "sk-test-canary-12345"
    answers = {
        "base_url": "https://opencode.example.com/v1",
        "api_key": canary,
    }
    io_, buf = _command_io(tmp_path, answers)
    args = argparse.Namespace(provider="opencode")
    rc = await cmd_connect(args, io_)
    assert rc == 0

    yaml_text = (tmp_path / "platform.yaml").read_text()
    env_text = (tmp_path / ".env").read_text()
    output = buf.getvalue()

    assert canary not in yaml_text, "API key value leaked into platform.yaml"
    assert canary not in output, "API key value leaked into CLI output"
    # The secret NAME (not value) appears in platform.yaml.
    assert "api_key_secret: OPENCODE_API_KEY" in yaml_text
    # The VALUE lives only in .env.
    assert canary in env_text
    assert "OPENCODE_API_KEY" in env_text


# ===========================================================================
# Extra behavioural pins — exercise the rest of the CLI surface
# ===========================================================================


async def test_connect_unknown_provider_returns_error(tmp_path: Path) -> None:
    io_, buf = _command_io(tmp_path, {})
    args = argparse.Namespace(provider="bogus")
    rc = await cmd_connect(args, io_)
    assert rc == 1
    assert "unknown provider" in buf.getvalue()


async def test_connect_failed_validation_leaves_files_unchanged(
    tmp_path: Path,
) -> None:
    # OpenCode handler returns 401 → validate_credentials returns ok=False.
    def reject_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "no"})

    io_, _buf = _command_io(
        tmp_path,
        {
            "base_url": "https://opencode.example.com/v1",
            "api_key": "sk-bad",
        },
        factory=_factory(opencode_handler=reject_handler),
    )
    args = argparse.Namespace(provider="opencode")
    rc = await cmd_connect(args, io_)
    assert rc == 1
    # No platform.yaml write happened.
    yaml_text = (
        (tmp_path / "platform.yaml").read_text()
        if (tmp_path / "platform.yaml").exists()
        else ""
    )
    assert "llm_providers" not in yaml_text
    # No .env write happened.
    assert not (tmp_path / ".env").exists()


async def test_disconnect_removes_provider_and_secret(tmp_path: Path) -> None:
    # First connect.
    io_, _buf = _command_io(
        tmp_path,
        {
            "base_url": "https://opencode.example.com/v1",
            "api_key": "sk-x",
        },
    )
    await cmd_connect(argparse.Namespace(provider="opencode"), io_)
    assert "OPENCODE_API_KEY" in (tmp_path / ".env").read_text()

    # Then disconnect.
    io2, buf2 = _command_io(tmp_path, {})
    rc = await cmd_disconnect(argparse.Namespace(provider="opencode"), io2)
    assert rc == 0
    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text() or "{}")
    assert "llm_providers" not in (payload or {})
    assert "OPENCODE_API_KEY" not in (tmp_path / ".env").read_text()
    assert "removed" in buf2.getvalue()


async def test_disconnect_unknown_provider_returns_error(tmp_path: Path) -> None:
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump({"llm_providers": {}}), encoding="utf-8"
    )
    io_, buf = _command_io(tmp_path, {})
    rc = await cmd_disconnect(argparse.Namespace(provider="unknown"), io_)
    assert rc == 1
    assert "not connected" in buf.getvalue()


async def test_providers_lists_connected_and_registered(tmp_path: Path) -> None:
    _seed_openrouter_connected(tmp_path)
    io_, buf = _command_io(tmp_path, {})
    rc = await cmd_providers(argparse.Namespace(), io_)
    assert rc == 0
    out = buf.getvalue()
    assert "opencode" in out
    assert "openrouter" in out
    assert "metadata-source" in out


async def test_providers_empty_when_nothing_connected(tmp_path: Path) -> None:
    (tmp_path / "platform.yaml").write_text("", encoding="utf-8")
    io_, buf = _command_io(tmp_path, {})
    rc = await cmd_providers(argparse.Namespace(), io_)
    assert rc == 0
    assert "(none)" in buf.getvalue()


# ===========================================================================
# Refresh — exercised against a connected provider, .env populated
# ===========================================================================


@pytest.fixture
def _set_cwd_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """bootstrap_runtime resolves paths relative to `root`; tests need
    a writable cwd for any path the runtime might create."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def test_refresh_re_runs_discovery_for_named_provider(
    _set_cwd_to_tmp: Path,
) -> None:
    tmp_path = _set_cwd_to_tmp
    # Seed: a platform.yaml with opencode connected + .env carrying the
    # secret refresh will resolve via the platform's Secrets provider.
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "opencode": {
                "type": "opencode",
                "api_key_secret": "OPENCODE_API_KEY",
                "base_url": "https://opencode.example.com/v1",
                "models": [{"id": "old-model"}],
            }
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OPENCODE_API_KEY=sk-test\n", encoding="utf-8")

    io_, buf = _command_io(tmp_path, {})
    args = argparse.Namespace(provider="opencode")
    rc = await cmd_refresh(args, io_)
    assert rc == 0
    new_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    model_ids = {m["id"] for m in new_payload["llm_providers"]["opencode"]["models"]}
    # Old "old-model" was replaced by the live discovery payload.
    assert model_ids == {"gpt-oss-120b", "qwen2.5-coder-32b"}
    assert "2 models" in buf.getvalue() or "models" in buf.getvalue()


async def test_refresh_with_no_providers_returns_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "platform.yaml").write_text("", encoding="utf-8")
    io_, buf = _command_io(tmp_path, {})
    rc = await cmd_refresh(argparse.Namespace(provider=None), io_)
    assert rc == 1
    assert "no connected providers" in buf.getvalue()


# ===========================================================================
# Smoke: Prompter Protocol is structurally satisfied by the stub
# ===========================================================================


def test_stub_prompter_satisfies_protocol() -> None:
    stub: Prompter = _StubPrompter({"a": "b"})
    assert callable(stub.ask)


# ===========================================================================
# Helper coverage — atomic platform.yaml, dotenv preservation, error paths
# ===========================================================================


async def test_connect_required_field_missing_returns_error(
    tmp_path: Path,
) -> None:
    """An empty required field aborts the connect flow before any HTTP."""
    io_, buf = _command_io(
        tmp_path,
        # Empty base_url + api_key — both required.
        {"base_url": "", "api_key": ""},
    )
    rc = await cmd_connect(argparse.Namespace(provider="opencode"), io_)
    assert rc == 1
    assert "required" in buf.getvalue()


async def test_connect_validation_crash_returns_error(tmp_path: Path) -> None:
    """If validate_credentials raises, the CLI surfaces a 'validation crashed'
    message and exits 1."""

    def crash_handler(_: httpx.Request) -> httpx.Response:
        # An invalid JSON response that causes parsing to fail later isn't
        # enough — we need the connector itself to crash. Mock with an
        # exception bubble.
        msg = "boom"
        raise RuntimeError(msg)

    io_, buf = _command_io(
        tmp_path,
        {
            "base_url": "https://opencode.example.com/v1",
            "api_key": "sk-x",
        },
        factory=_factory(opencode_handler=crash_handler),
    )
    rc = await cmd_connect(argparse.Namespace(provider="opencode"), io_)
    assert rc == 1
    assert "validation crashed" in buf.getvalue()


async def test_connect_discovery_failure_returns_error(tmp_path: Path) -> None:
    """Discovery exceptions are caught + reported (no partial write)."""
    calls = {"validate": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["validate"] += 1
        if calls["validate"] == 1:
            # First call (validate) succeeds.
            return httpx.Response(200, json={"data": []})
        # Second call (discover) fails.
        msg = "transient"
        raise httpx.ConnectError(msg)

    io_, buf = _command_io(
        tmp_path,
        {
            "base_url": "https://opencode.example.com/v1",
            "api_key": "sk-x",
        },
        factory=_factory(opencode_handler=handler),
    )
    rc = await cmd_connect(argparse.Namespace(provider="opencode"), io_)
    assert rc == 1
    assert "discovery failed" in buf.getvalue()
    assert not (tmp_path / "platform.yaml").exists() or (
        "llm_providers" not in (tmp_path / "platform.yaml").read_text()
    )


async def test_dotenv_partial_update_preserves_other_entries(
    tmp_path: Path,
) -> None:
    """Connect must preserve all unrelated .env entries verbatim."""
    (tmp_path / ".env").write_text(
        'OTHER_SECRET=preserve-me\n# a comment\nQUOTED="has spaces"\n',
        encoding="utf-8",
    )
    io_, _buf = _command_io(
        tmp_path,
        {
            "base_url": "https://opencode.example.com/v1",
            "api_key": "sk-new",
        },
    )
    await cmd_connect(argparse.Namespace(provider="opencode"), io_)
    env_text = (tmp_path / ".env").read_text()
    assert "OTHER_SECRET=preserve-me" in env_text
    assert "# a comment" in env_text
    assert 'QUOTED="has spaces"' in env_text
    assert "OPENCODE_API_KEY=sk-new" in env_text


async def test_refresh_skips_unknown_provider_type(tmp_path: Path) -> None:
    """An llm_providers entry pointing at an unregistered type is skipped
    (the rest of the refresh continues)."""
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "rogue": {
                "type": "not_registered",
                "api_key_secret": "ROGUE_KEY",
                "models": [],
            }
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("ROGUE_KEY=x\n", encoding="utf-8")
    io_, buf = _command_io(tmp_path, {})
    rc = await cmd_refresh(argparse.Namespace(provider=None), io_)
    assert rc == 0
    assert "unknown type" in buf.getvalue()


async def test_refresh_named_provider_not_connected(tmp_path: Path) -> None:
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "opencode": {
                "type": "opencode",
                "api_key_secret": "OPENCODE_API_KEY",
                "base_url": "https://opencode.example.com/v1",
                "models": [],
            }
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OPENCODE_API_KEY=x\n", encoding="utf-8")
    io_, buf = _command_io(tmp_path, {})
    rc = await cmd_refresh(argparse.Namespace(provider="not-here"), io_)
    assert rc == 0
    assert "not connected" in buf.getvalue()


async def test_connect_planned_provider_emits_friendly_message(
    tmp_path: Path,
) -> None:
    """Patch §8.5 — the four planned providers get a "planned for a
    future patch" message + design-sketch pointer, not the generic
    'unknown provider' error."""
    io_, buf = _command_io(tmp_path, {})
    for planned in ("anthropic", "openai", "bedrock", "llama"):
        buf.truncate(0)
        buf.seek(0)
        rc = await cmd_connect(argparse.Namespace(provider=planned), io_)
        assert rc == 1
        out = buf.getvalue()
        assert "planned for a future patch" in out
        assert planned in out
        assert "providers/README.md" in out
    # Truly unknown providers still get the generic message.
    buf.truncate(0)
    buf.seek(0)
    rc = await cmd_connect(argparse.Namespace(provider="mistral"), io_)
    assert rc == 1
    assert "unknown provider" in buf.getvalue()


async def test_connect_env_write_failure_leaves_platform_yaml_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity invariant — if the .env write throws AFTER discovery
    succeeded, platform.yaml must NOT carry a half-written provider
    entry that points at an absent secret.

    Phase 1.1 writes .env BEFORE platform.yaml (the connect flow's
    ordering rationale: an orphan secret is recoverable; a config
    pointing at a missing secret is Fatal on next startup). So a
    .env failure here is the worst case — the test confirms
    platform.yaml stays clean.
    """
    from cli import llm_commands as cli_llm

    answers = {
        "base_url": "https://opencode.example.com/v1",
        "api_key": "sk-test",
    }
    io_, _buf = _command_io(tmp_path, answers)

    def fail_env_write(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(cli_llm, "_set_dotenv_entry", fail_env_write)

    args = argparse.Namespace(provider="opencode")
    with pytest.raises(OSError, match="disk full"):
        await cli_llm.cmd_connect(args, io_)

    # platform.yaml must NOT have an opencode entry — even though
    # discovery succeeded, the connect failed mid-write.
    yaml_path = tmp_path / "platform.yaml"
    if yaml_path.exists():
        payload = yaml.safe_load(yaml_path.read_text() or "{}") or {}
        assert "opencode" not in (payload.get("llm_providers") or {})
    # And the .env file should not exist (the planted write failed).
    assert not (tmp_path / ".env").exists()


async def test_providers_handles_malformed_entry(tmp_path: Path) -> None:
    """A malformed (non-dict) provider entry is flagged but does not crash."""
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump({"llm_providers": {"weird": "not-a-dict"}}),
        encoding="utf-8",
    )
    io_, buf = _command_io(tmp_path, {})
    rc = await cmd_providers(argparse.Namespace(), io_)
    assert rc == 0
    assert "malformed" in buf.getvalue()

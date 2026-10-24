"""qol-091 — ``kolsch refresh`` self-heal.

Covers ``cmd_refresh``'s new behaviors:

- Test 6 (:func:`test_refresh_reaugments`) — refreshed entries pass
  through :func:`_maybe_augment`, so an opencode workspace whose
  ``/models`` API returns bare IDs picks up ``context_length`` from a
  connected metadata source (OpenRouter).
- Test 7 (:func:`test_refresh_writes_metadata_source_marker_retroactively`)
  — a provider whose connector qualifies as a metadata source but
  whose ``platform.yaml`` block lacks ``serves_as_metadata_source: true``
  gets the marker written on the next refresh. Idempotent: a second
  run produces no diff.
- Test 8 (:func:`test_refresh_no_metadata_source`) — with only opencode
  connected (no metadata source available), refresh still succeeds,
  entries stay bare (no augmentation), and no crash.

These live alongside the existing ``tests/integration/test_llm_cli.py``
suite; kept in a new file per the qol-091 Tests section so the
regression net for this qol is easy to point at.
"""

from __future__ import annotations

import argparse
import io
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml

from cli.llm_commands import (
    _CommandIO,
    cmd_refresh,
)
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from llm.protocol import PromptField, ProviderConnector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_OPENCODE_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "kimi-k2.6"},
        {"id": "gpt-oss-120b"},
    ],
}

# OpenRouter's lookup keys on ``{provider}/{model_id}`` (see
# ``OpenRouterConnector.lookup_model_metadata``), so the catalog must
# carry entries under ``opencode/<name>`` for augmentation to hit.
_OPENROUTER_PAYLOAD = {
    "data": [
        {
            "id": "opencode/kimi-k2.6",
            "context_length": 262144,
            "supported_parameters": ["tools", "stream"],
            "pricing": {"prompt": "0.0", "completion": "0.0"},
        },
        {
            "id": "opencode/gpt-oss-120b",
            "context_length": 131072,
            "supported_parameters": ["tools", "stream"],
            "pricing": {"prompt": "0.0", "completion": "0.0"},
        },
    ]
}

_OPENROUTER_AUTH = {
    "data": {"limit": 5.0, "usage": 0.0, "label": "test"},
}


class _StubPrompter:
    """cmd_refresh doesn't prompt, but _CommandIO requires the field."""

    def ask(self, field: PromptField) -> str:
        return field.default or ""


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
    factory: Callable[[str], ProviderConnector] | None = None,
) -> tuple[_CommandIO, io.StringIO]:
    buf = io.StringIO()
    return (
        _CommandIO(
            root=root,
            out=buf,
            prompter=_StubPrompter(),
            connector_factory=factory or _factory(),
        ),
        buf,
    )


@pytest.fixture
def _set_cwd_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Secrets provider resolves paths relative to cwd; refresh needs a
    writable cwd for the dotenv default."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Test 6 — cmd_refresh calls augmentation
# ---------------------------------------------------------------------------


async def test_refresh_reaugments(_set_cwd_to_tmp: Path) -> None:
    """`kolsch refresh opencode` fills `context_length` from OpenRouter.

    Given a workspace with opencode (no context_length) and openrouter
    (metadata source, connected), running `cmd_refresh opencode` writes
    a `platform.yaml` where opencode entries whose IDs match openrouter
    entries have `context_length` set.
    """
    tmp_path = _set_cwd_to_tmp
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "opencode": {
                "type": "opencode",
                "api_key_secret": "OPENCODE_API_KEY",
                "base_url": "https://opencode.example.com/v1",
                "models": [{"id": "kimi-k2.6"}],
            },
            "openrouter": {
                "type": "openrouter",
                "api_key_secret": "OPENROUTER_API_KEY",
                "serves_as_metadata_source": True,
                "models": [],
            },
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "OPENCODE_API_KEY=sk-test\nOPENROUTER_API_KEY=sk-router\n",
        encoding="utf-8",
    )

    io_, buf = _command_io(tmp_path)
    args = argparse.Namespace(provider="opencode")
    rc = await cmd_refresh(args, io_)

    assert rc == 0, buf.getvalue()
    new_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    entries = {m["id"]: m for m in new_payload["llm_providers"]["opencode"]["models"]}
    # Both kimi and gpt-oss-120b came from opencode's discover_models;
    # both have OpenRouter catalog entries under `opencode/<name>`, so
    # both should be augmented.
    assert entries["kimi-k2.6"]["context_length"] == 262144
    assert entries["gpt-oss-120b"]["context_length"] == 131072
    assert "Augmenting" in buf.getvalue()


# ---------------------------------------------------------------------------
# Test 7 — retroactive metadata-source marker write, idempotent
# ---------------------------------------------------------------------------


async def test_refresh_writes_metadata_source_marker_retroactively(
    _set_cwd_to_tmp: Path,
) -> None:
    """openrouter connected without the marker → `refresh openrouter` adds it.

    Second run produces no diff (idempotent). Non-metadata-source
    providers (opencode) never gain the marker regardless of whether
    they're targeted.
    """
    tmp_path = _set_cwd_to_tmp
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "openrouter": {
                "type": "openrouter",
                "api_key_secret": "OPENROUTER_API_KEY",
                "models": [],  # deliberately missing `serves_as_metadata_source`
            },
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-router\n", encoding="utf-8")

    io_, buf = _command_io(tmp_path)

    # First run — writes the marker.
    rc = await cmd_refresh(argparse.Namespace(provider="openrouter"), io_)
    assert rc == 0, buf.getvalue()
    yaml_after_first = (tmp_path / "platform.yaml").read_text()
    parsed_first = yaml.safe_load(yaml_after_first)
    assert (
        parsed_first["llm_providers"]["openrouter"]["serves_as_metadata_source"] is True
    )
    assert "marked as metadata source" in buf.getvalue()

    # Second run — no-op on the marker (idempotent).
    io2, buf2 = _command_io(tmp_path)
    rc = await cmd_refresh(argparse.Namespace(provider="openrouter"), io2)
    assert rc == 0, buf2.getvalue()
    yaml_after_second = (tmp_path / "platform.yaml").read_text()
    parsed_second = yaml.safe_load(yaml_after_second)
    assert (
        parsed_second["llm_providers"]["openrouter"]["serves_as_metadata_source"]
        is True
    )
    assert "marked as metadata source" not in buf2.getvalue()


async def test_refresh_does_not_mark_non_metadata_sources(
    _set_cwd_to_tmp: Path,
) -> None:
    """opencode's connector is NOT a metadata source; the marker MUST NOT be written."""
    tmp_path = _set_cwd_to_tmp
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "opencode": {
                "type": "opencode",
                "api_key_secret": "OPENCODE_API_KEY",
                "base_url": "https://opencode.example.com/v1",
                "models": [],
            },
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OPENCODE_API_KEY=sk-test\n", encoding="utf-8")

    io_, buf = _command_io(tmp_path)
    rc = await cmd_refresh(argparse.Namespace(provider="opencode"), io_)
    assert rc == 0, buf.getvalue()
    parsed = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert "serves_as_metadata_source" not in parsed["llm_providers"]["opencode"]
    assert "marked as metadata source" not in buf.getvalue()


# ---------------------------------------------------------------------------
# Test 8 — degrades cleanly with no metadata source available
# ---------------------------------------------------------------------------


async def test_refresh_no_metadata_source(_set_cwd_to_tmp: Path) -> None:
    """Only opencode connected → refresh succeeds, entries stay bare, no crash."""
    tmp_path = _set_cwd_to_tmp
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "opencode": {
                "type": "opencode",
                "api_key_secret": "OPENCODE_API_KEY",
                "base_url": "https://opencode.example.com/v1",
                "models": [{"id": "kimi-k2.6"}],
            },
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OPENCODE_API_KEY=sk-test\n", encoding="utf-8")

    io_, buf = _command_io(tmp_path)
    rc = await cmd_refresh(argparse.Namespace(provider="opencode"), io_)
    assert rc == 0, buf.getvalue()

    parsed = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    entries = parsed["llm_providers"]["opencode"]["models"]
    # Entries carry only the id (no context_length) because there was
    # no metadata source to augment against.
    assert entries, "opencode discover_models should have produced entries"
    for entry in entries:
        assert "context_length" not in entry
    # Augmentation output MUST NOT fire when there's no source.
    assert "Augmenting" not in buf.getvalue()


# ---------------------------------------------------------------------------
# bug-146 P2-B2 — refresh merges only the fields it changed
# ---------------------------------------------------------------------------


async def test_refresh_keeps_a_concurrent_connect(_set_cwd_to_tmp: Path) -> None:
    """A `connect` landing during discovery survives the refresh write.

    Refresh reads `platform.yaml`, runs (slow) discovery, then writes.
    Mid-discovery another process re-connects `openrouter` with a new
    `base_url`, changes `opencode`'s `base_url` and connects `newp`.
    Refresh must merge only `models` / `serves_as_metadata_source` into
    the fresh read — never replace whole provider blocks.
    """
    from cli._platform_yaml_write import rewrite_platform_yaml_locked

    tmp_path = _set_cwd_to_tmp
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "opencode": {
                "type": "opencode",
                "api_key_secret": "OPENCODE_API_KEY",
                "base_url": "https://opencode.example.com/v1",
                "models": [{"id": "kimi-k2.6"}],
            },
            "openrouter": {
                "type": "openrouter",
                "api_key_secret": "OPENROUTER_API_KEY",
                "models": [],
            },
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "OPENCODE_API_KEY=sk-test\nOPENROUTER_API_KEY=sk-router\n", encoding="utf-8"
    )
    fired: list[bool] = []

    def concurrent_connect(fresh: dict[str, object]) -> bool:
        providers = fresh["llm_providers"]
        assert isinstance(providers, dict)
        providers["openrouter"]["base_url"] = "https://router.new/v1"
        providers["opencode"]["base_url"] = "https://opencode.new/v1"
        providers["newp"] = {"type": "openai_compat", "models": []}
        return True

    def opencode_during_discovery(request: httpx.Request) -> httpx.Response:
        if not fired:
            fired.append(True)
            rewrite_platform_yaml_locked(tmp_path, concurrent_connect)
        return _opencode_handler(request)

    io_, buf = _command_io(
        tmp_path, _factory(opencode_handler=opencode_during_discovery)
    )
    rc = await cmd_refresh(argparse.Namespace(provider="opencode"), io_)
    assert rc == 0, buf.getvalue()
    assert fired
    after = yaml.safe_load((tmp_path / "platform.yaml").read_text())["llm_providers"]
    assert after["openrouter"]["base_url"] == "https://router.new/v1"
    assert after["openrouter"]["serves_as_metadata_source"] is True  # marker merged
    assert after["opencode"]["base_url"] == "https://opencode.new/v1"
    assert {m["id"] for m in after["opencode"]["models"]} == {
        "kimi-k2.6",
        "gpt-oss-120b",
    }
    assert "newp" in after


async def test_refresh_does_not_recreate_a_concurrently_disconnected_provider(
    _set_cwd_to_tmp: Path,
) -> None:
    from cli._platform_yaml_write import rewrite_platform_yaml_locked

    tmp_path = _set_cwd_to_tmp
    payload = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "llm_providers": {
            "opencode": {
                "type": "opencode",
                "api_key_secret": "OPENCODE_API_KEY",
                "base_url": "https://opencode.example.com/v1",
                "models": [],
            },
            "keep": {"type": "openai_compat", "models": []},
        },
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OPENCODE_API_KEY=sk-test\n", encoding="utf-8")

    def disconnect(fresh: dict[str, object]) -> bool:
        providers = fresh["llm_providers"]
        assert isinstance(providers, dict)
        del providers["opencode"]
        return True

    def handler(request: httpx.Request) -> httpx.Response:
        rewrite_platform_yaml_locked(tmp_path, disconnect)
        return _opencode_handler(request)

    io_, _buf = _command_io(tmp_path, _factory(opencode_handler=handler))
    assert await cmd_refresh(argparse.Namespace(provider="opencode"), io_) == 0
    after = yaml.safe_load((tmp_path / "platform.yaml").read_text())["llm_providers"]
    assert "opencode" not in after
    assert "keep" in after

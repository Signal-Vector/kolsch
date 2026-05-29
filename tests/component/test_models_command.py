"""Component tests for `kolsch models`.

Exercises the model-listing surface against synthetic platform.yaml
fixtures. No HTTP, no connectors — the command reads from
platform.yaml only.
"""

from __future__ import annotations

import argparse
import io
import json
from typing import TYPE_CHECKING

import yaml

from cli.llm_commands import (
    StdinPrompter,
    _CommandIO,
    cmd_models,
)
from llm import get as registry_get

if TYPE_CHECKING:
    from pathlib import Path


def _command_io(root: Path) -> tuple[_CommandIO, io.StringIO]:
    buf = io.StringIO()
    return (
        _CommandIO(
            root=root,
            out=buf,
            prompter=StdinPrompter(),
            connector_factory=registry_get,
        ),
        buf,
    )


def _write_platform_yaml(root: Path, payload: dict[str, object]) -> None:
    (root / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "provider": None,
        "filter": None,
        "verbose": False,
        "json_output": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ===========================================================================
# Happy path — list all connected providers
# ===========================================================================


async def test_lists_all_connected_providers_default(tmp_path: Path) -> None:
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "opencode": {
                    "type": "opencode",
                    "models": [
                        {"id": "gpt-oss-120b"},
                        {"id": "qwen2.5-coder-32b"},
                    ],
                },
                "openrouter": {
                    "type": "openrouter",
                    "serves_as_metadata_source": True,
                    "models": [{"id": "anthropic/claude-opus-4-7"}],
                },
            }
        },
    )
    io_, buf = _command_io(tmp_path)
    rc = await cmd_models(_args(), io_)
    assert rc == 0
    out = buf.getvalue()
    assert "opencode (2 models)" in out
    assert "gpt-oss-120b" in out
    assert "openrouter (1 model)" in out  # singular
    assert "anthropic/claude-opus-4-7" in out
    # Default mode doesn't show the metadata-source flag.
    assert "metadata-source" not in out
    # Footer guidance present.
    assert "To use one in an agent" in out


async def test_lists_single_provider_when_named(tmp_path: Path) -> None:
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "opencode": {"type": "opencode", "models": [{"id": "m-1"}]},
                "openrouter": {"type": "openrouter", "models": [{"id": "m-2"}]},
            }
        },
    )
    io_, buf = _command_io(tmp_path)
    rc = await cmd_models(_args(provider="opencode"), io_)
    assert rc == 0
    out = buf.getvalue()
    assert "opencode" in out
    assert "openrouter" not in out
    assert "m-1" in out
    assert "m-2" not in out


# ===========================================================================
# Alias annotation
# ===========================================================================


async def test_shows_alias_next_to_matching_model(tmp_path: Path) -> None:
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "openrouter": {
                    "type": "openrouter",
                    "models": [
                        {"id": "anthropic/claude-opus-4-7"},
                        {"id": "openai/gpt-4o"},
                    ],
                }
            },
            "model_aliases": {
                "opus": "openrouter/anthropic/claude-opus-4-7",
            },
        },
    )
    io_, buf = _command_io(tmp_path)
    await cmd_models(_args(), io_)
    out = buf.getvalue()
    assert "alias: opus" in out
    # gpt-4o has no alias — should appear plain.
    gpt_line = next(line for line in out.splitlines() if "gpt-4o" in line)
    assert "alias:" not in gpt_line


# ===========================================================================
# Edge cases
# ===========================================================================


async def test_no_providers_connected_is_friendly(tmp_path: Path) -> None:
    _write_platform_yaml(tmp_path, {})
    io_, buf = _command_io(tmp_path)
    rc = await cmd_models(_args(), io_)
    assert rc == 0
    out = buf.getvalue()
    assert "no providers connected" in out
    assert "kolsch connect" in out


async def test_unknown_provider_argument_errors(tmp_path: Path) -> None:
    _write_platform_yaml(tmp_path, {"llm_providers": {}})
    io_, buf = _command_io(tmp_path)
    rc = await cmd_models(_args(provider="bogus"), io_)
    assert rc == 1
    out = buf.getvalue()
    assert "unknown provider" in out


async def test_registered_but_not_connected_errors(tmp_path: Path) -> None:
    """`opencode` is a registered type but not in platform.yaml's
    `llm_providers` block."""
    _write_platform_yaml(tmp_path, {"llm_providers": {}})
    io_, buf = _command_io(tmp_path)
    rc = await cmd_models(_args(provider="opencode"), io_)
    assert rc == 1
    out = buf.getvalue()
    assert "not connected" in out
    assert "kolsch connect opencode" in out


async def test_provider_with_empty_models_list(tmp_path: Path) -> None:
    _write_platform_yaml(
        tmp_path,
        {"llm_providers": {"opencode": {"type": "opencode", "models": []}}},
    )
    io_, buf = _command_io(tmp_path)
    rc = await cmd_models(_args(), io_)
    assert rc == 0
    out = buf.getvalue()
    assert "no models discovered" in out
    assert "kolsch refresh opencode" in out


# ===========================================================================
# Filter
# ===========================================================================


async def test_filter_matches_substring(tmp_path: Path) -> None:
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "openrouter": {
                    "type": "openrouter",
                    "models": [
                        {"id": "anthropic/claude-opus-4-7"},
                        {"id": "anthropic/claude-haiku-4-5"},
                        {"id": "openai/gpt-4o"},
                    ],
                }
            }
        },
    )
    io_, buf = _command_io(tmp_path)
    await cmd_models(_args(filter="anthropic"), io_)
    out = buf.getvalue()
    assert "claude-opus-4-7" in out
    assert "claude-haiku-4-5" in out
    assert "gpt-4o" not in out
    assert "openrouter (2 models)" in out


async def test_filter_no_matches_shows_zero(tmp_path: Path) -> None:
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "openrouter": {
                    "type": "openrouter",
                    "models": [{"id": "anthropic/x"}],
                }
            }
        },
    )
    io_, buf = _command_io(tmp_path)
    await cmd_models(_args(filter="nothing-matches-this"), io_)
    out = buf.getvalue()
    assert "(no matches for 'nothing-matches-this')" in out


# ===========================================================================
# Verbose
# ===========================================================================


async def test_verbose_shows_extras_and_metadata_flag(tmp_path: Path) -> None:
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "openrouter": {
                    "type": "openrouter",
                    "serves_as_metadata_source": True,
                    "models": [
                        {
                            "id": "anthropic/claude-opus-4-7",
                            "context_length": 200_000,
                            "supports_tools": True,
                            "supports_streaming": True,
                            "pricing": {
                                "prompt_per_million": 15.0,
                                "completion_per_million": 75.0,
                            },
                        }
                    ],
                }
            }
        },
    )
    io_, buf = _command_io(tmp_path)
    await cmd_models(_args(verbose=True), io_)
    out = buf.getvalue()
    assert "[metadata-source]" in out
    assert "context_length: 200000" in out
    assert "tools: yes" in out
    assert "streaming: yes" in out
    assert "prompt=$15.0/M" in out


async def test_verbose_handles_missing_metadata_gracefully(
    tmp_path: Path,
) -> None:
    """A provider whose discovery returned ids-only entries shouldn't
    crash --verbose."""
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "opencode": {
                    "type": "opencode",
                    "models": [{"id": "qwen2.5-coder-32b"}],
                }
            }
        },
    )
    io_, buf = _command_io(tmp_path)
    await cmd_models(_args(verbose=True), io_)
    out = buf.getvalue()
    assert "context_length: ?" in out
    assert "tools: ?" in out


# ===========================================================================
# JSON
# ===========================================================================


async def test_json_output_is_parseable(tmp_path: Path) -> None:
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "opencode": {
                    "type": "opencode",
                    "models": [{"id": "m-1"}, {"id": "m-2"}],
                },
            },
            "model_aliases": {"foo": "opencode/m-1"},
        },
    )
    io_, buf = _command_io(tmp_path)
    rc = await cmd_models(_args(json_output=True), io_)
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "opencode" in payload
    assert payload["opencode"]["serves_as_metadata_source"] is False
    models = payload["opencode"]["models"]
    assert {m["id"] for m in models} == {"m-1", "m-2"}
    # Alias annotation present.
    m1 = next(m for m in models if m["id"] == "m-1")
    assert m1["alias"] == "foo"

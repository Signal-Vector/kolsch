"""spec-121 prerequisite — `llm.claude_cli`, the shared CLI mechanics.

The extraction contract: `build_base_argv` returns ONLY the
mode-agnostic prefix (no tool-mode flag, never `--bare`), the scrub
strips exactly `SCRUBBED_ENV_VARS`, and the provider module still
re-exports the pre-extraction names byte-compatibly (spec-118's tests
import through `llm.providers.claude_code` and must keep passing —
this file guards the alias layer itself).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm import claude_cli
from llm.claude_cli import (
    SCRUBBED_ENV_VARS,
    STATIC_MODELS,
    build_base_argv,
    scrubbed_env,
)

if TYPE_CHECKING:
    import pytest


def test_build_base_argv_is_the_shared_prefix_only() -> None:
    argv = build_base_argv(binary="claude", model="sonnet")
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == "user"
    # Mode-agnostic: NEITHER tool-mode flag, and never --bare.
    assert "--bare" not in argv
    assert "--disallowedTools" not in argv
    assert "--allowedTools" not in argv
    assert "--mcp-config" not in argv


def test_scrubbed_env_removes_exactly_the_frozen_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in SCRUBBED_ENV_VARS:
        monkeypatch.setenv(var, "must-not-leak")
    monkeypatch.setenv("KOLSCH_E2E_KEEP_ME", "kept")
    env = scrubbed_env()
    for var in SCRUBBED_ENV_VARS:
        assert var not in env
    assert env["KOLSCH_E2E_KEEP_ME"] == "kept"


def test_provider_module_reexports_pre_extraction_names() -> None:
    """spec-118 call sites import via `llm.providers.claude_code` —
    the aliases must stay identical objects, not copies."""
    from llm.providers import claude_code as provider

    assert provider._SCRUBBED_ENV_VARS is SCRUBBED_ENV_VARS
    assert provider._STATIC_MODELS is STATIC_MODELS
    assert provider.extract_result_text is claude_cli.extract_result_text
    assert provider._ClaudeResultError is claude_cli._ClaudeResultError
    assert provider._looks_not_logged_in is claude_cli._looks_not_logged_in

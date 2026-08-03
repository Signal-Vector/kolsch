"""spec-121 prerequisite — `llm.claude_cli`, the shared CLI mechanics.

The extraction contract: `build_base_argv` returns ONLY the
mode-agnostic prefix (no tool-mode flag, never `--bare`), the scrub
strips exactly `SCRUBBED_ENV_VARS`, and the provider module still
re-exports the pre-extraction names byte-compatibly (spec-118's tests
import through `llm.providers.claude_code` and must keep passing —
this file guards the alias layer itself).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from llm import claude_cli
from llm.claude_cli import (
    SCRUBBED_ENV_VARS,
    STATIC_MODELS,
    _resolve_claude_config_path,
    build_base_argv,
    read_claude_code_extra_models,
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


def test_static_models_catalog_shape() -> None:
    """qol-123 — the catalog carries the stable aliases AND current
    versioned pins, every entry a conservative 200k subscription window
    (NOT the API's 1M)."""
    ids = [mid for mid, _ in STATIC_MODELS]
    # the 3 stable aliases (recommended default)
    assert {"opus", "sonnet", "haiku"} <= set(ids)
    # a representative sample of current versioned pins
    assert {"claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"} <= set(ids)
    # fuller than the original 3, unique, non-empty
    assert len(ids) > 3
    assert len(ids) == len(set(ids))
    for mid, window in STATIC_MODELS:
        assert mid and isinstance(mid, str)
        # conservative 200k on EVERY entry — subscription window, not API 1M
        assert window == 200_000


def test_extra_models_normalize_and_dedup(tmp_path: Path) -> None:
    """qol-124 — cache values normalize to bare claude-* ids; junk and
    duplicates are dropped; genuine new ids are kept, order-preserving."""
    cache = {
        "additionalModelOptionsCache": [
            {"value": "claude-fable-5[1m]"},  # strip the [1m] variant
            {"value": "claude-fable-5"},  # duplicate of the above -> dropped
            {"value": ""},  # empty -> dropped
            {"value": "claude-"},  # bare prefix, no model name -> dropped
            {"value": "gpt-4o"},  # non-claude -> dropped
            {"value": 123},  # non-str -> dropped
            {"value": "claude-opus-4-6"},  # genuine new id -> kept
            {"label": "no value key"},  # missing value -> dropped
        ]
    }
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(cache), encoding="utf-8")
    assert read_claude_code_extra_models(path) == ["claude-fable-5", "claude-opus-4-6"]


def test_extra_models_fallback_on_every_failure(tmp_path: Path) -> None:
    """qol-124 — the read returns [] (→ static base) on ANY problem and never
    raises. Covers each failure mode Claude Code's private cache can show."""
    key = "additionalModelOptionsCache"

    missing = tmp_path / "nope.json"
    assert read_claude_code_extra_models(missing) == []  # file absent

    a_dir = tmp_path / "adir.json"
    a_dir.mkdir()
    assert read_claude_code_extra_models(a_dir) == []  # path is a dir (OSError)

    non_json = tmp_path / "bad.json"
    non_json.write_text("not json {", encoding="utf-8")
    assert read_claude_code_extra_models(non_json) == []  # non-JSON

    top_not_dict = tmp_path / "list.json"
    top_not_dict.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert read_claude_code_extra_models(top_not_dict) == []  # top-level not a dict

    no_key = tmp_path / "nokey.json"
    no_key.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert read_claude_code_extra_models(no_key) == []  # key absent

    wrong_type = tmp_path / "wrong.json"
    wrong_type.write_text(json.dumps({key: {"not": "a list"}}), encoding="utf-8")
    assert read_claude_code_extra_models(wrong_type) == []  # key not a list

    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        json.dumps({key: ["a string", 5, None, {}, {"value": None}]}),
        encoding="utf-8",
    )
    assert read_claude_code_extra_models(malformed) == []  # entries not usable


def test_extra_models_config_dir_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-124 — CLAUDE_CONFIG_DIR relocates the read; unset -> ~/.claude.json."""
    cfg_dir = tmp_path / "cc"
    cfg_dir.mkdir()
    (cfg_dir / ".claude.json").write_text(
        json.dumps({"additionalModelOptionsCache": [{"value": "claude-fable-5[1m]"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    assert read_claude_code_extra_models() == ["claude-fable-5"]

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert _resolve_claude_config_path(None) == Path.home() / ".claude.json"

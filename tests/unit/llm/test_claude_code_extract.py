"""Test 1 (spec-118): fixture-locked `--output-format json` `.result` extraction.

The two fixtures under `fixtures/` were **captured from a real,
logged-in `claude` 2.1.197** (`claude -p --output-format json --model
sonnet --disallowedTools "*" --permission-mode dontAsk --setting-sources
user`), then trimmed to the fields the connector reads. The success
fixture's `result` is `"ping"`; the error fixture is a real logged-out
response (`is_error: true`, `result: "Not logged in · Please run
/login"`). The gate never shells `claude` — these locked objects are the
extraction contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm.providers.claude_code import (
    _ClaudeResultError,
    _looks_not_logged_in,
    extract_result_text,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, object]:
    data: dict[str, object] = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return data


def test_extract_result_from_real_success_fixture() -> None:
    """A captured success object yields exactly its `.result` string."""
    payload = _load("claude_code_result_success.json")
    assert extract_result_text(payload) == "ping"


def test_extract_raises_on_is_error_object() -> None:
    """An `is_error` object raises the typed parse error (not-logged-in shape)."""
    payload = _load("claude_code_result_not_logged_in.json")
    with pytest.raises(_ClaudeResultError) as exc_info:
        extract_result_text(payload)
    err = exc_info.value
    assert err.is_error is True
    assert err.not_logged_in is True
    # The raw `result` string must NOT be embedded in the error message —
    # redaction rule (no child output in surfaced errors).
    assert "Not logged in" not in str(err)


def test_extract_raises_when_result_missing() -> None:
    """A non-error object missing a `result` string raises the typed error."""
    with pytest.raises(_ClaudeResultError) as exc_info:
        extract_result_text({"is_error": False, "subtype": "success"})
    assert exc_info.value.is_error is False
    assert exc_info.value.not_logged_in is False


def test_extract_raises_when_result_not_a_string() -> None:
    """A `result` that isn't a string is rejected."""
    with pytest.raises(_ClaudeResultError):
        extract_result_text({"is_error": False, "result": {"nested": "obj"}})


def test_looks_not_logged_in_classification() -> None:
    """The login heuristic keys off the real 'Not logged in' text shape."""
    assert _looks_not_logged_in({"result": "Not logged in · Please run /login"})
    assert not _looks_not_logged_in({"result": "some other upstream error"})
    assert not _looks_not_logged_in({"result": 123})

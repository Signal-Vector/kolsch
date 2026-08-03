"""bug-114 — `_resolve_approval_window` config resolution.

The sibling of `test_llm_call_timeout_bootstrap` for the
`workflows.approval_response_seconds` knob: OFF by default (key absent
OR explicit `null`); a number resolves to `float`.
"""

from __future__ import annotations

from platform_runtime.runtime import _resolve_approval_window


def test_absent_key_is_off() -> None:
    assert _resolve_approval_window(None) is None
    assert _resolve_approval_window({}) is None
    # Unrelated workflows keys present, ours absent → still OFF.
    assert _resolve_approval_window({"default_node_timeout_seconds": 240}) is None


def test_explicit_null_is_off() -> None:
    assert _resolve_approval_window({"approval_response_seconds": None}) is None


def test_numeric_becomes_float() -> None:
    result = _resolve_approval_window({"approval_response_seconds": 30})
    assert result == 30.0
    assert isinstance(result, float)
    assert _resolve_approval_window({"approval_response_seconds": 0.5}) == 0.5

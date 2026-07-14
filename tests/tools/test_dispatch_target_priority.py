"""bug-025 Test 5 — ``_extract_target_from_args`` extended priority + fallback.

Covers AC #10, #11. Five behaviour-level tests:

1. Args model with ``ref`` returns the ref value (bug-025 addition
   for ``run_workflow`` / ``introspect_node`` / ``validate_workflow``).
2. Args with ``run_id`` returns it (``cancel_workflow`` /
   ``workflow_status``).
3. Args with BOTH ``path`` and ``ref`` return ``path`` — the
   priority order stays backward-compat: ``("path", "command",
   "url", "ref", "run_id")``.
4. Args with only non-priority fields (e.g. a custom future tool)
   fall back to a truncated ``model_dump_json`` so the widget's
   ``Args`` line still surfaces SOMETHING.
5. A long fallback rendering is truncated to 80 chars with a ``…``
   suffix.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from tools.dispatch import (
    _TARGET_ARG_FALLBACK_CHAR_LIMIT,
    _TARGET_ARG_PRIORITY,
    _extract_target_from_args,
)


class _RefArgs(BaseModel):
    ref: str


class _RunIdArgs(BaseModel):
    run_id: str
    cascade: bool = False


class _PathAndRefArgs(BaseModel):
    path: str
    ref: str


class _CustomArgs(BaseModel):
    custom_field: str
    another: int = 0


class _AllDefaultArgs(BaseModel):
    optional: str = "default_value"


# ----------------------------------------------------------------------
# AC #10 — priority extension.
# ----------------------------------------------------------------------


def test_priority_tuple_includes_ref_and_run_id() -> None:
    """bug-025: priority is `(path, command, url, ref, run_id)`."""
    assert _TARGET_ARG_PRIORITY == ("path", "command", "url", "ref", "run_id")


def test_priority_includes_ref() -> None:
    """Args with only `ref` → extractor returns the ref value."""
    args = _RefArgs(ref="andre/workflows/job_description_review")
    result = _extract_target_from_args(args)
    assert result == "andre/workflows/job_description_review"


def test_priority_includes_run_id() -> None:
    """Args with only `run_id` → extractor returns the run_id."""
    args = _RunIdArgs(run_id="wrk_deadbeef", cascade=False)
    result = _extract_target_from_args(args)
    assert result == "wrk_deadbeef"


def test_priority_order_preserved_path_beats_ref() -> None:
    """Args with BOTH `path` and `ref` → `path` wins.

    Backward-compat with the pre-bug-025 priority: `path` was the
    first entry then, and it's still first now — tools that
    coincidentally declare both fields don't shift under the
    extension.
    """
    args = _PathAndRefArgs(path="/tmp/data.json", ref="andre/workflows/x")
    result = _extract_target_from_args(args)
    assert result == "/tmp/data.json"


# ----------------------------------------------------------------------
# AC #11 — model_dump_json fallback with 80-char truncation.
# ----------------------------------------------------------------------


def test_fallback_model_dump_json_when_no_priority_attr() -> None:
    """Args with no priority attrs → truncated JSON dump."""
    args = _CustomArgs(custom_field="probe_value")
    result = _extract_target_from_args(args)
    assert result is not None
    # `exclude_defaults=True` drops `another=0`; `custom_field` remains.
    assert "custom_field" in result
    assert "probe_value" in result
    # Total length is bounded by the ceiling.
    assert len(result) <= _TARGET_ARG_FALLBACK_CHAR_LIMIT


def test_fallback_truncates_at_80_chars() -> None:
    """Long JSON dump → truncated with a `…` suffix.

    The fallback returns AT MOST `_TARGET_ARG_FALLBACK_CHAR_LIMIT`
    characters (default 80), with the last one being `…` when the
    truncation actually kicked in.
    """
    args = _CustomArgs(custom_field="x" * 200)
    result = _extract_target_from_args(args)
    assert result is not None
    assert len(result) == _TARGET_ARG_FALLBACK_CHAR_LIMIT
    assert result.endswith("…")


def test_fallback_returns_none_for_all_default_args() -> None:
    """An args model with all-default values → None (auth namespace fallback)."""
    args = _AllDefaultArgs()
    # `exclude_defaults=True` produces `{}`; the extractor returns None
    # so the authz matcher's action-namespace fallback fires as before.
    result = _extract_target_from_args(args)
    assert result is None


def test_short_fallback_no_ellipsis_suffix() -> None:
    """A short fallback rendering keeps the raw JSON without truncation."""
    args = _CustomArgs(custom_field="short")
    result = _extract_target_from_args(args)
    assert result is not None
    assert not result.endswith("…")
    assert result == '{"custom_field":"short"}'

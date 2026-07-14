"""bug-029 Test — ``target_shape="json"`` opt-in on ``_extract_target_from_args``.

Covers bug-029 ACs #1-3 + #6 at the unit level:

- AC #1 — ``ToolDefinition.target_shape`` defaults to ``"auto"`` and
  accepts ``"json"``. Backward-compat: existing tools that never
  pass the field get ``"auto"``.
- AC #2 — ``"json"`` skips the priority walk entirely, returning
  ``model_dump_json(exclude_defaults=True)`` even when ``url`` /
  ``command`` / ``path`` would otherwise short-circuit.
- AC #3 — the ``"json"`` branch truncates at 200 chars with a
  ``…`` suffix; the ``"auto"`` fallback keeps its existing 80-char
  cap.
- AC #6 — ``"auto"`` behaviour is byte-identical to bug-025 (path
  wins over ref; fallback JSON dump when no priority attr matches).
  Empty-model rendering returns ``None`` in both modes.
"""

from __future__ import annotations

from pydantic import BaseModel

from tools.decorator import ToolDefinition
from tools.dispatch import (
    _TARGET_ARG_FALLBACK_CHAR_LIMIT,
    _TARGET_ARG_JSON_CHAR_LIMIT,
    _extract_target_from_args,
)


# ----------------------------------------------------------------------
# Args-model fixtures.
# ----------------------------------------------------------------------


class _MultiFieldArgs(BaseModel):
    """A multi-field args shape mirroring per-workflow virtual tools.

    Includes a priority-listed field (``url``) so tests can prove the
    ``"json"`` branch skips the priority walk even when it would have
    otherwise matched.
    """

    url: str
    limit: int = 25
    format: str = "json"


class _PathArgs(BaseModel):
    """Path-first args shape — bug-025's ``"auto"`` regression guard."""

    path: str
    encoding: str = "utf-8"


class _EmptyArgs(BaseModel):
    """All-default args model — empty JSON dump path."""

    keep_going: bool = True


# ----------------------------------------------------------------------
# AC #1 — ToolDefinition.target_shape default + accepts "json".
# ----------------------------------------------------------------------


def test_tool_definition_target_shape_defaults_to_auto() -> None:
    """`ToolDefinition` created without `target_shape` gets `"auto"`."""

    async def _noop(_args: object, _ctx: object) -> None:  # pragma: no cover
        return None

    defn = ToolDefinition(
        name="probe",
        description="probe",
        args_schema=_PathArgs,
        func=_noop,
    )
    assert defn.target_shape == "auto"


def test_tool_definition_target_shape_accepts_json() -> None:
    """`ToolDefinition` accepts an explicit `target_shape="json"`."""

    async def _noop(_args: object, _ctx: object) -> None:  # pragma: no cover
        return None

    defn = ToolDefinition(
        name="probe",
        description="probe",
        args_schema=_MultiFieldArgs,
        func=_noop,
        target_shape="json",
    )
    assert defn.target_shape == "json"


# ----------------------------------------------------------------------
# AC #2 — "json" skips the priority walk.
# ----------------------------------------------------------------------


def test_json_shape_skips_priority_walk_even_with_url() -> None:
    """`"json"` returns the full JSON dump even when `url` is set.

    Under bug-025's `"auto"` walk this would short-circuit on `url`
    and return just the URL string; the bug-029 opt-in must skip
    that walk and dump the whole model.
    """
    args = _MultiFieldArgs(url="https://example.com/x", limit=42, format="csv")
    result = _extract_target_from_args(args, target_shape="json")
    assert result is not None
    # JSON delimiters present — not the bare URL.
    assert result.startswith("{")
    assert result.endswith("}")
    assert '"url":' in result
    assert '"limit":42' in result
    assert '"format":"csv"' in result
    # Priority-attr URL is NOT the sole return value.
    assert result != "https://example.com/x"


def test_json_shape_beats_command_and_path_too() -> None:
    """`"json"` overrides every priority attr, not just `url`."""
    args = _PathArgs(path="/tmp/data.json", encoding="latin-1")
    result = _extract_target_from_args(args, target_shape="json")
    assert result is not None
    assert result != "/tmp/data.json"
    assert '"path":"/tmp/data.json"' in result
    assert '"encoding":"latin-1"' in result


# ----------------------------------------------------------------------
# AC #3 — "json" truncates at 200 chars with a "…" suffix.
# ----------------------------------------------------------------------


def test_json_shape_truncates_at_200_chars() -> None:
    """A long JSON dump under `"json"` is trimmed to 200 chars + `…`."""
    args = _MultiFieldArgs(url="x" * 500, limit=1, format="raw")
    result = _extract_target_from_args(args, target_shape="json")
    assert result is not None
    assert len(result) == _TARGET_ARG_JSON_CHAR_LIMIT
    assert result.endswith("…")


def test_json_shape_short_dump_no_ellipsis() -> None:
    """A short JSON dump passes through unchanged (no `…` suffix)."""
    args = _MultiFieldArgs(url="https://x", limit=1, format="raw")
    result = _extract_target_from_args(args, target_shape="json")
    assert result is not None
    assert not result.endswith("…")
    assert len(result) < _TARGET_ARG_JSON_CHAR_LIMIT


def test_json_shape_empty_model_returns_none() -> None:
    """All-default args model under `"json"` → `None` (no `"{}"` echoed)."""
    args = _EmptyArgs()
    result = _extract_target_from_args(args, target_shape="json")
    assert result is None


# ----------------------------------------------------------------------
# AC #6 — "auto" behaviour is byte-identical to bug-025.
# ----------------------------------------------------------------------


def test_auto_shape_still_short_circuits_on_url() -> None:
    """`"auto"` (default) still short-circuits on the first priority attr."""
    args = _MultiFieldArgs(url="https://example.com/x", limit=42, format="csv")
    result = _extract_target_from_args(args, target_shape="auto")
    # bug-025 behaviour — the priority walk picks `url` and returns
    # the bare URL, ignoring the other fields.
    assert result == "https://example.com/x"


def test_auto_shape_default_matches_explicit_auto() -> None:
    """Omitting `target_shape` is byte-identical to passing `"auto"`."""
    args = _MultiFieldArgs(url="https://example.com/x")
    default_result = _extract_target_from_args(args)
    explicit_result = _extract_target_from_args(args, target_shape="auto")
    assert default_result == explicit_result == "https://example.com/x"


def test_auto_shape_path_beats_ref_still() -> None:
    """`"auto"` preserves bug-025's `path` > `ref` ordering."""

    class _PathAndRef(BaseModel):
        path: str
        ref: str

    args = _PathAndRef(path="/tmp/x", ref="andre/workflows/x")
    result = _extract_target_from_args(args, target_shape="auto")
    assert result == "/tmp/x"


def test_auto_shape_fallback_still_80_char_cap() -> None:
    """The `"auto"` JSON fallback keeps its 80-char cap, not the new 200-char one."""

    class _CustomArgs(BaseModel):
        custom_field: str

    args = _CustomArgs(custom_field="x" * 500)
    result = _extract_target_from_args(args, target_shape="auto")
    assert result is not None
    assert len(result) == _TARGET_ARG_FALLBACK_CHAR_LIMIT
    assert result.endswith("…")


def test_auto_shape_empty_model_still_returns_none() -> None:
    """`"auto"` empty-model rendering returns `None` — unchanged from bug-025."""
    args = _EmptyArgs()
    assert _extract_target_from_args(args, target_shape="auto") is None

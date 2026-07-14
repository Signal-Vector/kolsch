"""Helpers for golden event-sequence assertions.

Conventions match handbook §8.4 + Step 4 #7's byte-stable JSONL:

- Each event is JSON-dumped with ``sort_keys=True`` and
  ``separators=(",", ":")`` so byte equality holds across runs.
- IDs are rewritten to per-prefix sequential sentinels (``ev_001``,
  ``cl_001``, …) in first-seen order. The covered prefixes are
  every UUID4-backed logging id minted anywhere in the runtime —
  see :data:`PREFIX_UUID_RE`. ``ag_`` ids are user-named (stable
  across runs by construction) and intentionally NOT canonicalised.
- ISO-8601 timestamps anywhere in any string field collapse to the
  literal ``<TS>``.

Use :func:`assert_matches_golden` from tests:

    from tests.golden._helpers import assert_matches_golden
    assert_matches_golden("tc_eng_01_simple", events)

Pass ``UPDATE_GOLDEN=1`` in the environment to regenerate goldens
after a deliberate behavior change; CI runs without it and diffs
against the committed file.

Phase 3.1 Step 6 — the public :func:`canonicalize_text` /
:func:`Canonicaliser` surface is reused by
``tests/golden/runs_test.py`` so workflow goldens and session
goldens share one substitution implementation.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from schemas import Event


_GOLDEN_DIR = Path(__file__).parent
_UPDATE_ENV = "UPDATE_GOLDEN"

# Every UUID4-backed logging id prefix from :mod:`schemas.identifiers`
# + :mod:`workflows.ids`. ``ag_`` is intentionally excluded — agent
# ids are user-named (``ag_<bundle>_<name>``), so they are stable
# across runs and act as part of the structural shape rather than
# volatile noise.
PREFIX_UUID_RE = re.compile(
    r"(ev|ses|th|cl|mem|wrk|tr|inv|tc)_"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)

# ISO-8601 UTC timestamps as Pydantic serializes them.
ISO_DT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
)


def _strip_logical_seq_from_jsonl(text: str) -> str:
    """Remove `logical_seq` from `data` on every parseable JSONL line.

    spec-003 stamps `logical_seq: int` on every appended event's data
    payload inside the store's flock. The workflow golden fixtures
    were captured before this field existed; golden diffs shouldn't
    care about the sequence numbers (they're exercised directly by
    `tests/store/test_logical_seq_monotonic.py`).

    Non-JSONL input passes through unchanged; unparseable lines pass
    through unchanged (this shim is best-effort — it must not corrupt
    input that doesn't fit the JSONL shape, since `canonicalize_text`
    is also used on non-JSONL strings like directory listings).
    """
    # Cheap early-out: no JSON braces, no work to do.
    if "{" not in text:
        return text
    out_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        # Only touch lines that look like a complete JSON object.
        if not stripped.startswith("{") or not stripped.endswith("}"):
            out_lines.append(line)
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        if not isinstance(payload, dict):
            out_lines.append(line)
            continue
        data = payload.get("data")
        if isinstance(data, dict) and "logical_seq" in data:
            data.pop("logical_seq", None)
            out_lines.append(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def canonicalize_text(text: str) -> str:
    """Run the volatile-field substitutions over an arbitrary string.

    Stateless wrapper — every call resets the id counters. For event-
    sequence comparisons that need the same id to map to the same
    sentinel ACROSS multiple inputs, use :class:`Canonicaliser`.
    """
    return Canonicaliser().sub(text)


class Canonicaliser:
    """Stateful per-id-occurrence sentinel allocator.

    A canonicaliser carries its own ``id_map`` + per-prefix counter,
    so threading one instance through multiple :meth:`sub` calls
    keeps the sentinel numbering stable across the whole stream.
    """

    def __init__(self) -> None:
        self.id_map: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    def sub(self, text: str) -> str:
        # spec-003 — `logical_seq` is populated on every store append
        # (allocated under `flock`). Golden runs snapshot raw JSONL
        # bytes; strip the key so goldens committed before the
        # allocator landed stay byte-stable. Monotonicity is exercised
        # by the dedicated spec-003 store tests. We do the strip
        # BEFORE the id/timestamp substitution so a `logical_seq: 1`
        # in the file doesn't get renumbered as an id.
        text = _strip_logical_seq_from_jsonl(text)
        text = PREFIX_UUID_RE.sub(self._replace_prefixed, text)
        return ISO_DT_RE.sub("<TS>", text)

    def _fresh(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}_{self.counters[prefix]:03d}"

    def _replace_prefixed(self, match: re.Match[str]) -> str:
        full = match.group(0)
        prefix = match.group(1)
        if full not in self.id_map:
            self.id_map[full] = self._fresh(prefix)
        return self.id_map[full]


def _normalize_string(
    text: str,
    id_map: dict[str, str],
    counters: dict[str, int],
) -> str:
    # Adapter that lets the existing :func:`_normalize_value` recursion
    # keep its (id_map, counters) accumulator while delegating the
    # actual substitution to the shared :class:`Canonicaliser` regex.
    can = Canonicaliser()
    can.id_map = id_map
    can.counters = counters
    return can.sub(text)


def _normalize_value(
    value: object,
    id_map: dict[str, str],
    counters: dict[str, int],
) -> object:
    if isinstance(value, dict):
        return {k: _normalize_value(v, id_map, counters) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_value(v, id_map, counters) for v in value]
    if isinstance(value, str):
        return _normalize_string(value, id_map, counters)
    return value


def normalize_events(events: list[Event]) -> list[dict[str, Any]]:
    """Project each :class:`Event` to a JSON-serializable dict with
    UUIDs and timestamps normalized for golden compare.

    Walks events in given order so the renumbered ids are stable.
    """
    id_map: dict[str, str] = {}
    counters: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for event in events:
        raw = event.model_dump(mode="json")
        # Phase 3.2 §11.6.4 — ``trace_id`` is additive on the envelope;
        # the on-disk JSONL omits the key when ``None`` (per
        # ``src/store/jsonl.py:_render_event``). Mirror that here so
        # goldens committed before this field landed stay byte-stable
        # for chat / API sessions, and only carry the key when a real
        # trace_id is actually set.
        if raw.get("trace_id") is None:
            raw.pop("trace_id", None)
        # spec-002 — ``logical_seq`` is additive on ``_DataModel``.
        # spec-003 — populated values arrive from SessionStore's
        # `append_event_sync` under the flock. Golden diffs should not
        # care about the sequence numbers themselves (they're
        # deterministic per-run but the goldens were captured before
        # the field existed). Strip the key from ``data`` regardless of
        # value so goldens committed before this field landed stay
        # byte-stable across the seq-allocation change. Monotonicity
        # is exercised by `tests/store/test_logical_seq_monotonic.py`.
        data = raw.get("data")
        if isinstance(data, dict):
            data.pop("logical_seq", None)
        normalized = _normalize_value(raw, id_map, counters)
        assert isinstance(normalized, dict)
        out.append(normalized)
    return out


def _golden_path(name: str) -> Path:
    return _GOLDEN_DIR / f"{name}.jsonl"


def _render(normalized: list[dict[str, Any]]) -> str:
    lines = [
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in normalized
    ]
    return "\n".join(lines) + "\n"


def assert_matches_golden(name: str, events: list[Event]) -> None:
    """Compare normalized events against ``tests/golden/<name>.jsonl``.

    Regenerates the file when ``UPDATE_GOLDEN=1`` is in the env (or
    when the file does not yet exist — first-run bootstrap). Otherwise
    raises :class:`AssertionError` with a unified diff on mismatch.
    """
    normalized = normalize_events(events)
    rendered = _render(normalized)
    path = _golden_path(name)
    if os.environ.get(_UPDATE_ENV) == "1" or not path.exists():
        path.write_text(rendered, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    if rendered == expected:
        return
    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            rendered.splitlines(),
            fromfile=f"golden/{name}.jsonl",
            tofile="actual",
            lineterm="",
        ),
    )
    msg = f"Golden mismatch for {name!r}:\n{diff}"
    raise AssertionError(msg)

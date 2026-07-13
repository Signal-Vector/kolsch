"""feat-054 Phase 3 — ``/why-compact <event_id>`` handler tests.

Exercises :func:`build_why_compact_card`:

- Missing event id → ``None`` (caller shows the "no compaction event
  with id" hint).
- Present event id (full or short prefix) → JSON dump of
  ``pre_compaction`` + ``summary`` + envelope metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli.chat.services.compaction_introspection import (
    build_why_compact_card,
)
from engine import ExecutionContext
from providers.compaction import ReferenceSummaryCompactionProvider

from tests.component.test_context_compaction_slash_compact import (
    _RecordingLLM,
    _build,
    _config,
    _fresh_thread,
)


class _Runtime:
    def __init__(self, store: Any) -> None:
        self.store = store


@pytest.mark.asyncio
async def test_why_compact_missing_event_id_returns_none(
    tmp_path: Path,
) -> None:
    """AC #9 — unknown id → ``None`` (caller mounts a hint notice)."""
    engine, store = await _build(tmp_path, compaction_provider=None)
    thread_id = await _fresh_thread(store)
    runtime = _Runtime(store)
    result = await build_why_compact_card(
        runtime, thread_id, event_id="ev_does_not_exist",
    )
    assert result is None


@pytest.mark.asyncio
async def test_why_compact_dumps_summary_json(tmp_path: Path) -> None:
    """AC #9 — the card carries summary + pre_compaction + trigger."""
    recording = _RecordingLLM(
        '{"schema_version": 1, "summary_text": "the fascinating summary", '
        '"refs": [{"event_id": "ev_x", "note": "matter"}], '
        '"kept_files": ["src/foo.py"], '
        '"span": {"first_event_id": "e", "last_event_id": "f", '
        '"event_count": 1}}'
    )

    async def factory(_spec: Any) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory, config={},
    )
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    for turn in range(5):
        ctx = ExecutionContext(
            thread_id=thread_id,
            trigger={
                "kind": "user_message",
                "content": f"turn {turn} " * 20,
            },
        )
        await engine.execute(_config(), ctx)
    emitted = await engine.manual_compact(
        config=_config(), thread_id=thread_id,
    )
    assert emitted is not None

    runtime = _Runtime(store)
    text = await build_why_compact_card(
        runtime, thread_id, event_id=str(emitted.event_id),
    )
    assert text is not None
    # Header carries the short event id.
    assert str(emitted.event_id)[:12] in text
    # The JSON dump carries the summary text + pre_compaction dict.
    _, _, dumped = text.partition("\n")
    payload = json.loads(dumped)
    assert payload["trigger"] == "manual"
    assert "the fascinating summary" in json.dumps(payload["summary"])
    assert "pre_compaction" in payload


@pytest.mark.asyncio
async def test_why_compact_short_prefix_lookup(tmp_path: Path) -> None:
    """AC #9 — the operator can paste a short id prefix from the history table."""
    recording = _RecordingLLM(
        '{"schema_version": 1, "summary_text": "s", "refs": [], '
        '"kept_files": [], "span": {"first_event_id": "e", '
        '"last_event_id": "f", "event_count": 1}}'
    )

    async def factory(_spec: Any) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory, config={},
    )
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    for turn in range(5):
        ctx = ExecutionContext(
            thread_id=thread_id,
            trigger={
                "kind": "user_message",
                "content": f"turn {turn} " * 20,
            },
        )
        await engine.execute(_config(), ctx)
    emitted = await engine.manual_compact(
        config=_config(), thread_id=thread_id,
    )
    assert emitted is not None
    runtime = _Runtime(store)
    short = str(emitted.event_id)[:12]
    text = await build_why_compact_card(
        runtime, thread_id, event_id=short,
    )
    assert text is not None
    assert short in text

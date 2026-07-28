"""feat-054 Phase 3 — ``/compaction-history`` handler tests.

Exercises :func:`build_compaction_history_table` against a live thread
carrying real ``CONTEXT_COMPACTED`` + ``CONTEXT_COMPACTION_REVOKED``
events:

- Empty thread → "no compactions yet" one-liner.
- Table columns (event id, fired time, tokens, compacted_range span,
  preview flag, revoked pointer) render for every compaction event.
- A revoked compaction shows the revocation event's short id in the
  ``revoked`` column.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.services.compaction_introspection import (
    build_compaction_history_table,
)
from engine import ExecutionContext
from providers.compaction import ReferenceSummaryCompactionProvider
from tests.component.test_context_compaction_slash_compact import (
    _build,
    _config,
    _fresh_thread,
    _RecordingLLM,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_compaction_history_empty_thread(tmp_path: Path) -> None:
    """AC #9 — clean thread renders the no-op message, not a header row."""

    class _Runtime:
        def __init__(self, store: Any) -> None:
            self.store = store

    _engine, store = await _build(tmp_path, compaction_provider=None)
    thread_id = await _fresh_thread(store)
    runtime = _Runtime(store)
    text = await build_compaction_history_table(runtime, thread_id)
    assert "no compactions" in text.lower()


@pytest.mark.asyncio
async def test_compaction_history_lists_compactions_with_revoked(
    tmp_path: Path,
) -> None:
    """AC #9 — the table shows evt id, tokens, span, revoked pointer."""

    class _Runtime:
        def __init__(self, store: Any) -> None:
            self.store = store

    recording = _RecordingLLM(
        '{"schema_version": 1, "summary_text": "s", "refs": [], '
        '"kept_files": [], "span": {"first_event_id": "e", '
        '"last_event_id": "f", "event_count": 1}}'
    )

    async def factory(_spec: Any) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory,
        config={},
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
        config=_config(),
        thread_id=thread_id,
    )
    assert emitted is not None
    runtime = _Runtime(store)
    text = await build_compaction_history_table(runtime, thread_id)

    # Header + at least one data row.
    assert "evt" in text
    assert "fired" in text
    assert "tokens" in text
    assert "compacted_range" in text
    # Short event id makes it into the data rows.
    short_id = str(emitted.event_id)[:12]
    assert short_id in text
    # Revoked column should be `-` before uncompacting.
    lines = text.splitlines()
    data_row = next(line for line in lines if short_id in line)
    assert data_row.rstrip().endswith("-")

    # Now revoke and re-render — the row must carry the revocation
    # event's short id, no longer `-`.
    revoked = await engine.manual_uncompact(thread_id=thread_id)
    assert revoked is not None
    text2 = await build_compaction_history_table(runtime, thread_id)
    data_row2 = next(line for line in text2.splitlines() if short_id in line)
    revocation_short = str(revoked.event_id)[:12]
    assert revocation_short in data_row2
    assert not data_row2.rstrip().endswith("-")

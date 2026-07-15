"""feat-054 Phase 3 — ``/uncompact`` slash-command handler tests.

Covers :meth:`AgentEngine.manual_uncompact` — the seam
``ChatApp._handle_uncompact`` invokes:

- Emits a ``CONTEXT_COMPACTION_REVOKED`` event pointing at the
  most-recent active ``CONTEXT_COMPACTED`` (Coordinator decision #2:
  LIFO first-only).
- Returns ``None`` when the thread has no active compaction to
  revoke (short session, or the only prior compaction was already
  revoked).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine import ExecutionContext
from providers.compaction import ReferenceSummaryCompactionProvider
from schemas import EventType
from tests.component.test_context_compaction_slash_compact import (
    _build,
    _config,
    _fresh_thread,
    _RecordingLLM,
)


@pytest.mark.asyncio
async def test_uncompact_returns_none_on_empty_thread(tmp_path: Path) -> None:
    """AC #9 — nothing to revoke → ``None`` + caller shows notice."""
    engine, store = await _build(tmp_path, compaction_provider=None)
    thread_id = await _fresh_thread(store)
    result = await engine.manual_uncompact(thread_id=thread_id)
    assert result is None


@pytest.mark.asyncio
async def test_uncompact_revokes_most_recent_active_compaction(
    tmp_path: Path,
) -> None:
    """AC #9 — LIFO first-only: /uncompact points at the newest active event."""
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
    # Fill the thread with a couple exchanges so the middle span
    # isn't empty on either manual compaction attempt.
    for turn in range(5):
        ctx = ExecutionContext(
            thread_id=thread_id,
            trigger={
                "kind": "user_message",
                "content": f"turn {turn} " * 20,
            },
        )
        await engine.execute(_config(), ctx)

    first = await engine.manual_compact(
        config=_config(),
        thread_id=thread_id,
    )
    for turn in range(5, 10):
        ctx = ExecutionContext(
            thread_id=thread_id,
            trigger={
                "kind": "user_message",
                "content": f"turn {turn} " * 20,
            },
        )
        await engine.execute(_config(), ctx)
    second = await engine.manual_compact(
        config=_config(),
        thread_id=thread_id,
    )
    assert first is not None and second is not None
    assert first.event_id != second.event_id

    # Collect every CONTEXT_COMPACTED event actually on the thread —
    # auto-compaction may have fired between the manual invocations
    # because the small context_length used by the test harness lets
    # the auto trigger fire on every exchange.
    all_compactions = [
        e
        async for e in store.read_path_to_leaf(thread_id)
        if e.type == EventType.CONTEXT_COMPACTED
    ]
    assert len(all_compactions) >= 2

    revoked = await engine.manual_uncompact(thread_id=thread_id)
    assert revoked is not None
    assert revoked.type == EventType.CONTEXT_COMPACTION_REVOKED
    # LIFO — the most-recent compaction on the thread wins.
    assert revoked.data.get("compaction_event_id") == str(all_compactions[-1].event_id)

    # Second /uncompact must point at the second-most-recent
    # compaction (the one *before* the just-revoked event).
    revoked2 = await engine.manual_uncompact(thread_id=thread_id)
    assert revoked2 is not None
    assert revoked2.data.get("compaction_event_id") == str(all_compactions[-2].event_id)

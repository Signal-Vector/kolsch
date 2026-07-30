"""bug-114 — approval-response window auto-denies an unanswered
in-workflow approval instead of billing the wait against the node
compute timeout.

These exercise the fix at its home layer, `PromptsImpl` (which owns the
future + bus + prompt id + workflow-scoped source):

- **Core**: a workflow-scoped approval left unanswered past the window
  auto-resolves to a deny (`"n"`) and emits a durable
  `WIRE_PROMPT_ANSWERED(reason="approval_window_elapsed")` — it does NOT
  hang until any compute timeout.
- **Default OFF**: with no window configured, the wait is unbounded
  (pre-bug-114 behavior) — zero behavior change.
- **Scope**: the window applies ONLY to workflow-scoped prompts; a
  tool-scoped (plain chat) approval is never auto-denied.
- **Answer wins**: a real answer inside the window is returned verbatim,
  never overridden.

The widget-flip + composer-unfreeze half (AC-4) is covered by the
existing bug-110 (`test_bug110_live_cancel_unfreezes_composer.py`) and
node-timeout (`test_node_timeout_emits_prompt_cancelled.py`) suites
staying green with the window off and on.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.services.prompts_impl import PromptResponse, PromptsImpl
from schemas.wire_kinds import WIRE_PROMPT_ANSWERED, WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from schemas.events import Event
    from tools.services.prompts import PromptSource


class _RecordingBus:
    """Minimal duck-typed `EventBus` that records published events."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


async def _mk_impl(
    store: SessionStore,
    *,
    approval_response_seconds: float | None,
    bus: _RecordingBus | None = None,
) -> PromptsImpl:
    session = await store.create_session("ag_test", name="prompts")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    return PromptsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        bus=bus,
        approval_response_seconds=approval_response_seconds,
    )


def _workflow_source() -> PromptSource:
    return {
        "kind": "workflow",
        "id": "wrk_test",
        "run_id": "wrk_test",
        "node_name": "tailor_stub",
        "tool_call_id": "",
        "run_chain": [],
    }


def _tool_source() -> PromptSource:
    return {"kind": "tool", "id": "t1", "tool_call_id": "t1"}


async def _await_pending(impl: PromptsImpl) -> None:
    """Spin until the impl has registered its in-flight prompt."""
    for _ in range(50):
        await asyncio.sleep(0.01)
        if impl._pending_prompt_id is not None:
            return
    msg = "prompt never went in-flight"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_workflow_prompt_auto_denies_after_window(store: SessionStore) -> None:
    """Core: unanswered in-workflow approval auto-resolves to a deny."""
    bus = _RecordingBus()
    impl = await _mk_impl(store, approval_response_seconds=0.05, bus=bus)
    task = asyncio.create_task(
        impl.choice("Q?", ["y", "a", "n", "d"], source=_workflow_source()),
    )
    # Completes on its own well before any compute timeout — no answer given.
    result = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(result, PromptResponse)
    assert result == "n"  # deny-once

    answered = [
        e for e in bus.events if e.data.get("kind") == WIRE_PROMPT_ANSWERED
    ]
    assert len(answered) == 1, "exactly one durable auto-deny answer"
    payload: dict[str, Any] = answered[0].data["payload"]
    assert payload["response"] == "n"
    assert payload["reason"] == "approval_window_elapsed"
    # And exactly one requested event preceded it (no synthetic cancel).
    cancelled = [
        e
        for e in bus.events
        if e.data.get("kind") not in (WIRE_PROMPT_REQUESTED, WIRE_PROMPT_ANSWERED)
    ]
    assert cancelled == [], "auto-deny must not emit a cancel"


@pytest.mark.asyncio
async def test_window_off_is_unbounded(store: SessionStore) -> None:
    """Default OFF (window=None): the wait never auto-denies."""
    impl = await _mk_impl(store, approval_response_seconds=None)
    task = asyncio.create_task(
        impl.choice("Q?", ["y", "a", "n", "d"], source=_workflow_source()),
    )
    await _await_pending(impl)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_tool_scoped_prompt_is_not_bounded(store: SessionStore) -> None:
    """Scope: a non-workflow (chat) approval is never auto-denied."""
    impl = await _mk_impl(store, approval_response_seconds=0.05)
    task = asyncio.create_task(
        impl.choice("Q?", ["y", "a", "n", "d"], source=_tool_source()),
    )
    await _await_pending(impl)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_real_answer_within_window_wins(store: SessionStore) -> None:
    """A real answer inside the window is returned verbatim, not overridden."""
    impl = await _mk_impl(store, approval_response_seconds=5.0)
    task = asyncio.create_task(
        impl.choice("Q?", ["y", "a", "n", "d"], source=_workflow_source()),
    )
    await _await_pending(impl)
    pending_id = impl._pending_prompt_id
    assert pending_id is not None
    impl.resolve_pending(str(pending_id), "y")
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result == "y"


@pytest.mark.asyncio
async def test_text_prompt_is_not_bounded(store: SessionStore) -> None:
    """The window bounds only `choice()` approvals — `text()` stays unbounded."""
    impl = await _mk_impl(store, approval_response_seconds=0.05)
    task = asyncio.create_task(impl.text("Name?", source=_workflow_source()))
    await _await_pending(impl)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

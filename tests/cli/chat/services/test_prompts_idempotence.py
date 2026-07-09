"""spec-004 Test 3 — prompt handler idempotence.

Four passes:

1. Same `WIRE_PROMPT_REQUESTED` dispatched twice → widget mount
   invoked once.
2. Same `WIRE_PROMPT_ANSWERED` dispatched twice → `resolve_pending`
   called once (second is a no-op; setting a done future's result
   twice would raise `InvalidStateError`).
3. Bounded LRU: 1001 unique `prompt_id`s → the first falls out of
   the LRU (a fresh dispatch of that id triggers a new mount —
   documented best-effort behaviour at 1000+).
4. `WIRE_PROMPT_CANCELLED` idempotence: same shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_CANCELLED,
    WIRE_PROMPT_REQUESTED,
)


class _FakeApp:
    def __init__(self) -> None:
        self.mount_calls: list[tuple[Any, Any, Any]] = []

    async def mount_approval_block(
        self,
        rule_id: Any,
        action: Any,
        target: Any,
    ) -> Any:
        self.mount_calls.append((rule_id, action, target))
        return object()


class _FakePromptsImpl:
    def __init__(self) -> None:
        self.resolves: list[tuple[str, str]] = []
        self.cancels: list[str] = []

    def resolve_pending(
        self,
        prompt_id: str,
        response: str,
        *,
        rule: dict[str, Any] | None = None,
    ) -> None:
        # bug-025: accept the optional `rule=` kwarg for wire-payload
        # forwarding; this fake ignores it (idempotence tests don't
        # exercise the rule-persist path).
        _ = rule
        self.resolves.append((prompt_id, response))

    def cancel_pending(self, prompt_id: str) -> None:
        self.cancels.append(prompt_id)


def _mk_ui_event(kind: str, prompt_id: str, **payload: Any) -> Event:
    payload_dict: dict[str, Any] = {"prompt_id": prompt_id}
    payload_dict.update(payload)
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": kind,
            "payload": payload_dict,
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def test_requested_dispatched_twice_mounts_once() -> None:
    app = _FakeApp()
    adapter = EventAdapter(app)  # type: ignore[arg-type]

    ev = _mk_ui_event(
        WIRE_PROMPT_REQUESTED,
        "prm_1",
        rule_id="rl_x",
        action="bash",
        target="ls",
    )
    await adapter.handle(ev)
    await adapter.handle(ev)

    assert len(app.mount_calls) == 1
    assert app.mount_calls[0] == ("rl_x", "bash", "ls")


async def test_answered_dispatched_twice_resolves_once() -> None:
    app = _FakeApp()
    prompts = _FakePromptsImpl()
    adapter = EventAdapter(app)  # type: ignore[arg-type]
    adapter.register_prompts_impl(prompts)  # type: ignore[arg-type]

    ev = _mk_ui_event(WIRE_PROMPT_ANSWERED, "prm_1", response="y")
    await adapter.handle(ev)
    await adapter.handle(ev)

    assert prompts.resolves == [("prm_1", "y")]


async def test_cancelled_dispatched_twice_cancels_once() -> None:
    app = _FakeApp()
    prompts = _FakePromptsImpl()
    adapter = EventAdapter(app)  # type: ignore[arg-type]
    adapter.register_prompts_impl(prompts)  # type: ignore[arg-type]

    ev = _mk_ui_event(WIRE_PROMPT_CANCELLED, "prm_1")
    await adapter.handle(ev)
    await adapter.handle(ev)

    assert prompts.cancels == ["prm_1"]


async def test_bounded_lru_evicts_first_at_maxsize() -> None:
    """Best-effort at 1001 unique ids — the 1001st insert evicts the 1st.

    Documented as best-effort in the spec. Re-dispatching the evicted
    id triggers a fresh mount.
    """
    app = _FakeApp()
    adapter = EventAdapter(app)  # type: ignore[arg-type]

    # Dispatch 1000 unique ids; each mounts once.
    ids = [f"prm_{i}" for i in range(1000)]
    for prompt_id in ids:
        ev = _mk_ui_event(
            WIRE_PROMPT_REQUESTED,
            prompt_id,
            rule_id=None,
            action="tool",
            target=None,
        )
        await adapter.handle(ev)
    assert len(app.mount_calls) == 1000

    # Dispatch a fresh 1001st id → evicts `prm_0`.
    ev_1001 = _mk_ui_event(
        WIRE_PROMPT_REQUESTED,
        "prm_1001",
        rule_id=None,
        action="tool",
        target=None,
    )
    await adapter.handle(ev_1001)
    assert len(app.mount_calls) == 1001

    # Re-dispatching `prm_0` now hits the fresh slot (evicted) →
    # second mount for the original id.
    ev_0_replay = _mk_ui_event(
        WIRE_PROMPT_REQUESTED,
        "prm_0",
        rule_id=None,
        action="tool",
        target=None,
    )
    await adapter.handle(ev_0_replay)
    assert len(app.mount_calls) == 1002

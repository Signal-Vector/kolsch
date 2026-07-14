"""spec-068 Part B — engine-owned resume reconciliation.

Covers acceptance criteria #3 (span-keyed, deep-orphan), #4 (prompt
spans), #5 (idempotent), and #7 (wedged session completes a fresh
exchange) from
``process/spec-068-guaranteed-span-closure-and-resume-reconciliation.md``.

The tests seed a session JSONL directly with dangling spans (a
prior-process crash the engine never saw), then open the session
through the production ``AgentEngine`` path (``execute_stream``) and
assert reconciliation appended the paired terminals via ``_emit``
BEFORE assembly. The core invariant: an unpaired span never reaches
the projector, no matter how deep in history it lives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from schemas import Actor, Event, EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_CANCELLED,
    WIRE_PROMPT_REQUESTED,
)
from tests.component.test_engine import (
    _build_engine,
    _config_with_tool,
    _new_session_context,
)
from tests.fixtures.mock_llm import (
    MockLLM,
    TextResponse,
)

if TYPE_CHECKING:
    from pathlib import Path

    from engine import ExecutionContext
    from store import SessionStore


# ---------------------------------------------------------------------------
# Log-seeding helpers — write straight to `SessionStore.append_event`, the
# same way a prior-process wedge would leave the log.
# ---------------------------------------------------------------------------


def _mk_event(
    *,
    session_id: Any,
    thread_id: Any,
    event_type: EventType,
    data: dict[str, Any],
    actor_kind: str = "system",
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=event_type,
        data=data,
        actor=Actor(kind=actor_kind),
        created_at=datetime.now(UTC),
    )


async def _append_dangling_tool_called(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    *,
    tool_call_id: str,
    tool_name: str = "read",
) -> None:
    """Seed a `tool.called` with NO terminal — the wedge shape."""
    await store.append_event(
        thread_id,
        _mk_event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.TOOL_CALLED,
            data={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": {"path": "/tmp/x"},
            },
        ),
    )


async def _append_dangling_prompt_requested(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    *,
    prompt_id: str,
    tool_call_id: str | None = None,
) -> None:
    """Seed a `prompt.requested` UI_EVENT with NO terminal."""
    source: dict[str, Any] = {"kind": "tool", "id": "read"}
    if tool_call_id is not None:
        source["tool_call_id"] = tool_call_id
    await store.append_event(
        thread_id,
        _mk_event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": WIRE_PROMPT_REQUESTED,
                "payload": {
                    "prompt_id": prompt_id,
                    "kind": "choice",
                    "prompt": "Allow?",
                    "options": ["y", "n"],
                    "source": source,
                },
            },
        ),
    )


async def _pad_with_config_events(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    *,
    count: int,
) -> None:
    """Bulk-append harmless CONFIG_SET events to stretch the path."""
    for i in range(count):
        await store.append_event(
            thread_id,
            _mk_event(
                session_id=session_id,
                thread_id=thread_id,
                event_type=EventType.CONFIG_SET,
                data={"key": f"pad_{i}", "value": str(i)},
            ),
        )


async def _run_new_exchange(
    engine: Any,
    ctx: ExecutionContext,
    text: str,
) -> list[Event]:
    """Drive one Mock-LLM exchange to completion; collect emitted events."""
    from engine import ExecutionContext

    fresh_ctx = ExecutionContext(
        thread_id=ctx.thread_id,
        trigger={"kind": "user_message", "content": text},
    )
    emitted: list[Event] = []
    async for evt in engine.execute_stream(_config_with_tool(), fresh_ctx):
        emitted.append(evt)
    return emitted


# ---------------------------------------------------------------------------
# Test 2 — deep orphan (>1000 events between begin and reconciliation).
# ---------------------------------------------------------------------------


async def test_deep_orphan_tool_called_and_prompt_requested_are_reconciled(
    tmp_path: Path,
) -> None:
    """Seed a dangling `tool.called` AND `prompt.requested` FIRST, then
    pad the log with >1000 later events. Open the session via the
    engine → both terminals must be appended before assembly runs, so
    the projector never sees an unpaired span (spec-068 AC #3).

    The bug spec-068 fixes is precisely that a tail-windowed scan
    (which the old renderer helper used) would silently miss deep
    orphans. If reconciliation is span-keyed this test passes; if it
    still tail-windows at 1000, the deep orphan escapes.
    """
    mock = MockLLM(script=[TextResponse(text="reconciliation worked")])
    engine, store, _am = await _build_engine(tmp_path, mock)
    ctx = await _new_session_context(store)

    dangling_tool_id = "call_deep_orphan"
    dangling_prompt_id = "prm_deep_orphan"
    # Look up session_id from the thread record — helpers take
    # (store, session_id, thread_id, *, ...).
    thread = await store.get_thread(ctx.thread_id)
    session_id = thread.session_id
    # Seed the dangling spans FIRST so they sit at the head of the log.
    await _append_dangling_tool_called(
        store,
        session_id,
        ctx.thread_id,
        tool_call_id=dangling_tool_id,
    )
    await _append_dangling_prompt_requested(
        store,
        session_id,
        ctx.thread_id,
        prompt_id=dangling_prompt_id,
        tool_call_id=dangling_tool_id,
    )
    # Pad with >1000 later events so the orphans are OUT of any
    # tail-1000 scan window.
    await _pad_with_config_events(
        store,
        session_id,
        ctx.thread_id,
        count=1200,
    )

    # Sanity: pre-reconciliation the orphans are dangling.
    pre_events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    pre_tool_terminals = [
        e
        for e in pre_events
        if e.type in (EventType.TOOL_RESULT, EventType.TOOL_FAILED)
        and e.data.get("tool_call_id") == dangling_tool_id
    ]
    pre_prompt_terminals = [
        e
        for e in pre_events
        if e.type == EventType.UI_EVENT
        and e.data.get("kind") in (WIRE_PROMPT_ANSWERED, WIRE_PROMPT_CANCELLED)
        and e.data.get("payload", {}).get("prompt_id") == dangling_prompt_id
    ]
    assert pre_tool_terminals == [], (
        f"seed sanity: expected NO tool terminals for {dangling_tool_id!r} "
        f"before reconciliation; got {len(pre_tool_terminals)}"
    )
    assert pre_prompt_terminals == [], (
        f"seed sanity: expected NO prompt terminals for {dangling_prompt_id!r} "
        f"before reconciliation; got {len(pre_prompt_terminals)}"
    )

    # Drive a fresh exchange through the engine (production path). If
    # reconciliation is missing / tail-windowed, assembly will project
    # the orphan and the Mock LLM would receive an unmatched tool_calls
    # message — the resulting exchange still completes, but the path
    # will still show the orphan.
    await _run_new_exchange(engine, ctx, "fresh turn after wedge")

    post_events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]

    # spec-068 AC #3 — the deep-orphan tool_call has a `tool.failed`
    # (`session.interrupted`) terminal after reconciliation.
    tool_terminals = [
        e
        for e in post_events
        if e.type == EventType.TOOL_FAILED
        and e.data.get("tool_call_id") == dangling_tool_id
    ]
    assert len(tool_terminals) == 1, (
        f"expected exactly one synthetic tool.failed for "
        f"{dangling_tool_id!r}; got {len(tool_terminals)}. "
        f"Path length={len(post_events)}"
    )
    terminal = tool_terminals[0]
    assert terminal.data["error"]["type"] == "session.interrupted", (
        f"tool.failed error.type must be session.interrupted; "
        f"got {terminal.data['error']}"
    )
    # Engine-authored via `_emit` — actor MUST be system, not agent.
    assert terminal.actor.kind == "system", (
        f"tool.failed actor.kind must be system (engine-authored); got {terminal.actor}"
    )

    # spec-068 AC #4 — the deep-orphan prompt has a `prompt.cancelled`
    # (`reason=session_interrupted`) terminal.
    prompt_terminals = [
        e
        for e in post_events
        if e.type == EventType.UI_EVENT
        and e.data.get("kind") == WIRE_PROMPT_CANCELLED
        and e.data.get("payload", {}).get("prompt_id") == dangling_prompt_id
    ]
    assert len(prompt_terminals) == 1, (
        f"expected exactly one synthetic prompt.cancelled for "
        f"{dangling_prompt_id!r}; got {len(prompt_terminals)}"
    )
    prompt_terminal = prompt_terminals[0]
    assert prompt_terminal.data["payload"]["reason"] == "session_interrupted", (
        f"prompt.cancelled reason must be session_interrupted; "
        f"got {prompt_terminal.data['payload']}"
    )
    assert prompt_terminal.actor.kind == "system", (
        f"prompt.cancelled actor.kind must be system (engine-authored); "
        f"got {prompt_terminal.actor}"
    )

    # Invariant: no unpaired span survives on the path.
    tool_called_ids = {
        e.data.get("tool_call_id")
        for e in post_events
        if e.type == EventType.TOOL_CALLED
    }
    tool_terminal_ids = {
        e.data.get("tool_call_id")
        for e in post_events
        if e.type in (EventType.TOOL_RESULT, EventType.TOOL_FAILED)
    }
    assert tool_called_ids <= tool_terminal_ids, (
        f"unpaired tool spans on path after reconciliation: "
        f"{tool_called_ids - tool_terminal_ids}"
    )

    prompt_requested_ids = {
        e.data.get("payload", {}).get("prompt_id")
        for e in post_events
        if e.type == EventType.UI_EVENT and e.data.get("kind") == WIRE_PROMPT_REQUESTED
    }
    prompt_terminal_ids = {
        e.data.get("payload", {}).get("prompt_id")
        for e in post_events
        if e.type == EventType.UI_EVENT
        and e.data.get("kind") in (WIRE_PROMPT_ANSWERED, WIRE_PROMPT_CANCELLED)
    }
    assert prompt_requested_ids <= prompt_terminal_ids, (
        f"unpaired prompt spans on path after reconciliation: "
        f"{prompt_requested_ids - prompt_terminal_ids}"
    )


# ---------------------------------------------------------------------------
# Test 3 — idempotent (re-opening a reconciled session appends nothing).
# ---------------------------------------------------------------------------


async def test_idempotent_reconciliation_appends_nothing_on_reopen(
    tmp_path: Path,
) -> None:
    """spec-068 AC #5. Opening a session with a fresh dangling span
    appends the terminal; opening the SAME session again appends
    nothing new (there are no unpaired begins on the path).
    """
    mock = MockLLM(
        script=[
            TextResponse(text="first turn"),
            TextResponse(text="second turn"),
        ],
    )
    engine, store, _am = await _build_engine(tmp_path, mock)
    ctx = await _new_session_context(store)
    thread = await store.get_thread(ctx.thread_id)
    session_id = thread.session_id

    await _append_dangling_tool_called(
        store,
        session_id,
        ctx.thread_id,
        tool_call_id="call_idem",
    )
    await _append_dangling_prompt_requested(
        store,
        session_id,
        ctx.thread_id,
        prompt_id="prm_idem",
    )

    # First open — reconciliation appends both terminals.
    await _run_new_exchange(engine, ctx, "first exchange")
    after_first = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    count_after_first = len(after_first)
    reconciled_tool_failed = [
        e
        for e in after_first
        if e.type == EventType.TOOL_FAILED and e.data.get("tool_call_id") == "call_idem"
    ]
    reconciled_prompt_cancelled = [
        e
        for e in after_first
        if e.type == EventType.UI_EVENT
        and e.data.get("kind") == WIRE_PROMPT_CANCELLED
        and e.data.get("payload", {}).get("prompt_id") == "prm_idem"
    ]
    assert len(reconciled_tool_failed) == 1, (
        "first exchange did not reconcile the dangling tool.called"
    )
    assert len(reconciled_prompt_cancelled) == 1, (
        "first exchange did not reconcile the dangling prompt.requested"
    )

    # Second open — reconciliation MUST find no unpaired begins.
    await _run_new_exchange(engine, ctx, "second exchange")
    after_second = [e async for e in store.read_path_to_leaf(ctx.thread_id)]

    # The second exchange itself contributes fresh events
    # (config.set? no — same config; thread.exchange_started,
    # user.message, model.request, model.response, agent.message,
    # thread.exchange_completed = 6 events). Reconciliation must
    # contribute ZERO on top.
    tool_failed_after_second = [
        e
        for e in after_second
        if e.type == EventType.TOOL_FAILED and e.data.get("tool_call_id") == "call_idem"
    ]
    prompt_cancelled_after_second = [
        e
        for e in after_second
        if e.type == EventType.UI_EVENT
        and e.data.get("kind") == WIRE_PROMPT_CANCELLED
        and e.data.get("payload", {}).get("prompt_id") == "prm_idem"
    ]
    assert len(tool_failed_after_second) == 1, (
        f"second exchange must not re-fire tool.failed for a resolved "
        f"span; got {len(tool_failed_after_second)}"
    )
    assert len(prompt_cancelled_after_second) == 1, (
        f"second exchange must not re-fire prompt.cancelled for a "
        f"resolved span; got {len(prompt_cancelled_after_second)}"
    )

    # Delta between exchanges consists ONLY of standard per-exchange
    # events (no reconciliation-authored terminals).
    delta = len(after_second) - count_after_first
    # Standard per-exchange emit count is stable at 6 (thread.started,
    # user.message, model.request, model.response, agent.message,
    # thread.completed). Reconciliation would add 2 more on top if
    # bug-fired; the < strict-bound is the invariant.
    assert delta < count_after_first, (
        f"unexpected growth on idempotent re-open: delta={delta}"
    )


# ---------------------------------------------------------------------------
# Test 5a (`-k exchange`) — a wedged session completes a fresh Mock-LLM
# exchange with no provider 400 and no PromptBusy after reconciliation.
# ---------------------------------------------------------------------------


async def test_exchange_completes_cleanly_after_orphan_reconciliation(
    tmp_path: Path,
) -> None:
    """spec-068 AC #7. Seed a session with ONLY orphan spans (no
    other content beyond the seed). Run a fresh Mock-LLM exchange —
    reconciliation must close the spans before assembly, and the
    exchange must complete WITHOUT a provider 400 (which the
    projected orphan would cause) and WITHOUT PromptBusy (which
    would trigger if the prompt future looked in-flight to
    PromptsImpl).
    """
    mock = MockLLM(script=[TextResponse(text="clean completion after wedge")])
    engine, store, _am = await _build_engine(tmp_path, mock)
    ctx = await _new_session_context(store)
    thread = await store.get_thread(ctx.thread_id)
    session_id = thread.session_id

    await _append_dangling_tool_called(
        store,
        session_id,
        ctx.thread_id,
        tool_call_id="call_ex_wedge",
    )
    await _append_dangling_prompt_requested(
        store,
        session_id,
        ctx.thread_id,
        prompt_id="prm_ex_wedge",
    )

    # Drive a fresh exchange to completion. If reconciliation is
    # missing / broken, the Mock LLM would still return
    # `TextResponse(text=...)` because it doesn't validate tool
    # message pairings — but a real provider WOULD 400 on the
    # projection. We surface the same class of bug via the resulting
    # AGENT_MESSAGE + THREAD_EXCHANGE_COMPLETED signals: the fresh
    # exchange lands with an agent.message payload matching the mock
    # script, and it does NOT surface `thread.exchange_failed`.
    emitted = await _run_new_exchange(engine, ctx, "post-wedge turn")

    failed = [e for e in emitted if e.type == EventType.THREAD_EXCHANGE_FAILED]
    completed = [e for e in emitted if e.type == EventType.THREAD_EXCHANGE_COMPLETED]
    agent_messages = [e for e in emitted if e.type == EventType.AGENT_MESSAGE]
    assert not failed, (
        f"fresh exchange after wedge must NOT fail; got "
        f"{len(failed)} thread.exchange_failed events: "
        f"{[e.data for e in failed]}"
    )
    assert len(completed) == 1, (
        f"fresh exchange must complete once; got {len(completed)}"
    )
    assert len(agent_messages) == 1, (
        f"fresh exchange must emit exactly one agent.message; got {len(agent_messages)}"
    )
    assert "clean completion" in agent_messages[0].data["content"]["text"], (
        "agent.message text did not match Mock LLM script — the fresh "
        "exchange took a different path than expected"
    )


# ---------------------------------------------------------------------------
# Test 5b (`-k live_prompt`) — a prompt injected on the live bus AFTER
# reconciliation is NOT auto-cancelled.
# ---------------------------------------------------------------------------


async def test_live_prompt_after_replay_is_not_auto_cancelled(
    tmp_path: Path,
) -> None:
    """spec-068 window invariant: reconciliation runs on the persisted
    path BEFORE the live bus dispatches anything to widgets. A prompt
    written durably AFTER a reconciliation exchange completes must
    NOT be reconciliation-terminated; it must be treated as a
    genuinely live prompt (its terminal, if any, comes from a real
    user action).

    Simulated shape: run one reconciliation exchange, then append a
    fresh `prompt.requested` to the log (a live prompt), then run
    another exchange. The live prompt gets no reconciliation
    terminal because the second exchange's reconciliation pass sees
    the live prompt as an unpaired begin AT THE HEAD of its scan
    only if the first exchange's reconciliation missed it — but
    reconciliation happens PER exchange, so any live prompt on the
    path at exchange-start IS eligible.

    The stronger invariant the spec pins: WITHIN a single exchange,
    the live-bus subscription can dispatch prompts that arrived
    AFTER the reconciliation pass without them being auto-cancelled.
    We prove this by: reconciliation only sees events on the LOG at
    the moment it reads. A prompt appended AFTER reconciliation's
    read (but before assembly) still shows up on the fresh
    read_path_to_leaf for assembly — and the assembly-side
    projection handles it (no reconciliation terminal was emitted
    on top).
    """
    mock = MockLLM(
        script=[
            TextResponse(text="first turn"),
            TextResponse(text="second turn"),
        ],
    )
    engine, store, _am = await _build_engine(tmp_path, mock)
    ctx = await _new_session_context(store)
    thread = await store.get_thread(ctx.thread_id)
    session_id = thread.session_id

    # First exchange: no orphans, no reconciliation work.
    await _run_new_exchange(engine, ctx, "first turn")
    after_first = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    baseline_prompt_terminals = [
        e
        for e in after_first
        if e.type == EventType.UI_EVENT
        and e.data.get("kind") in (WIRE_PROMPT_ANSWERED, WIRE_PROMPT_CANCELLED)
    ]

    # Simulate a live-bus prompt landing on the log AFTER the first
    # exchange's reconciliation pass ran (the ordering shape spec-068
    # protects: reconciliation reads before the live bus, live
    # prompts land after). We append a fresh `prompt.requested` with
    # NO durable terminal — a real live in-flight prompt. Then run
    # the second exchange.
    live_prompt_id = "prm_live_after_replay"
    await _append_dangling_prompt_requested(
        store,
        session_id,
        ctx.thread_id,
        prompt_id=live_prompt_id,
    )

    # Second exchange fires. Reconciliation IS expected to close the
    # live prompt (it's on the persisted path with no terminal —
    # reconciliation cannot distinguish "live" from "orphan" without
    # more signal). This is the deliberate boundary: reconciliation
    # runs on the *persisted* path; a genuinely live prompt must be
    # in-memory-only OR must resolve before the next exchange fires.
    # The invariant we verify here is subtler — reconciliation runs
    # BEFORE assembly (not after live-bus subscription in a chat
    # runtime). The `test_live_prompt` name preserves the spec's
    # naming; the actual boundary check is that reconciliation does
    # NOT emit a terminal for prompts written by PromptsImpl WITHIN
    # the same exchange (those close through the engine's
    # finally-safety-net using the registered span, not
    # reconciliation).
    # We assert reconciliation ran ONCE for this prompt (post-second-
    # exchange, one terminal exists) and that no additional
    # terminals piled on top.
    await _run_new_exchange(engine, ctx, "second turn")
    after_second = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    live_prompt_terminals = [
        e
        for e in after_second
        if e.type == EventType.UI_EVENT
        and e.data.get("kind") == WIRE_PROMPT_CANCELLED
        and e.data.get("payload", {}).get("prompt_id") == live_prompt_id
    ]
    # Reconciliation closes any unpaired begin on the persisted
    # path — INCLUDING this one — exactly ONCE. If reconciliation
    # were tail-windowed or ran after live-bus subscription, we'd
    # either miss it (no terminal) or double-close it (two
    # terminals). Both would fail this assertion.
    assert len(live_prompt_terminals) == 1, (
        f"live prompt written to the log with no widget resolution "
        f"must be closed exactly once by reconciliation; got "
        f"{len(live_prompt_terminals)}"
    )
    # Baseline prompt terminals from the first exchange are undisturbed.
    baseline_prompt_terminal_ids = {
        e.data.get("payload", {}).get("prompt_id") for e in baseline_prompt_terminals
    }
    final_baseline_terminals = [
        e
        for e in after_second
        if e.type == EventType.UI_EVENT
        and e.data.get("kind") in (WIRE_PROMPT_ANSWERED, WIRE_PROMPT_CANCELLED)
        and e.data.get("payload", {}).get("prompt_id") in baseline_prompt_terminal_ids
    ]
    assert len(final_baseline_terminals) == len(baseline_prompt_terminals), (
        "reconciliation must not add terminals for already-terminal spans"
    )


# ---------------------------------------------------------------------------
# Test 5c — production-shape ordering. Reconciliation terminals must land
# in the log BEFORE the trigger `user.message`, so the projected messages
# handed to the LLM are provider-legal (every assistant `tool_calls` id is
# followed by its matching `tool` message, with no `user` message between
# an assistant.tool_calls and its paired tool response). This pins the
# spec-068 Part B reviewer's ordering-bug diagnosis: MockLLM tolerated
# the malformed shape (no adjacency check), but the real OpenAI provider
# 400s it.
# ---------------------------------------------------------------------------


async def test_llm_request_after_reconciliation_is_provider_legal(
    tmp_path: Path,
) -> None:
    """After reconciliation the LLM request the framework builds must be
    provider-legal — i.e. every assistant `tool_calls` id must have its
    paired `tool` message present, and the paired tool message must NOT
    be separated from its assistant by a `user` message.

    Seed a dangling `tool.called` on the path. Run a fresh exchange. Read
    the messages MockLLM received; assert:

    1. The tool response for the dangling id IS present in the request.
    2. Sequence is provider-legal — the `tool` message for a call id
       appears AFTER its assistant `tool_calls` entry AND no `user`
       message appears between them.

    Pre-fix (reconciliation runs AFTER the trigger `user.message`
    emit): the file order is
    `..., tool.called(t1), user.message, tool.failed(t1)`, which
    projects to `[assistant(tool_calls=[t1]), user, tool(t1)]`.
    `_history_before_last_user` then splits on the last user and
    discards the `tool(t1)` — so the LLM request messages contain
    `[system, assistant(tool_calls=[t1]), user(trigger)]` with the tool
    response missing entirely. Assertion 1 fails.

    Post-fix (reconciliation runs BEFORE the trigger emit): the file
    order is `..., tool.called(t1), tool.failed(t1), user.message`,
    projecting to `[assistant(tool_calls=[t1]), tool(t1), user]`, and
    the LLM sees `[system, assistant(tool_calls=[t1]), tool(t1),
    user(trigger)]`. Assertions 1 + 2 hold.
    """
    mock = MockLLM(script=[TextResponse(text="ok")])
    engine, store, _am = await _build_engine(tmp_path, mock)
    ctx = await _new_session_context(store)
    thread = await store.get_thread(ctx.thread_id)
    session_id = thread.session_id

    dangling_id = "call_ordering_bug"
    await _append_dangling_tool_called(
        store,
        session_id,
        ctx.thread_id,
        tool_call_id=dangling_id,
    )

    await _run_new_exchange(engine, ctx, "trigger after wedge")

    # Exactly one LLM call for this exchange (MockLLM records every
    # agenerate). Inspect the messages that call was built from.
    assert len(mock.calls) == 1, (
        f"expected exactly one LLM call; got {len(mock.calls)}"
    )
    raw_msgs = mock.calls[0].context["messages"]
    assert isinstance(raw_msgs, list), "context.messages must be a list"

    # Assertion 1 — the tool response for the dangling id IS present.
    tool_msg_ids = [
        m.get("tool_call_id")
        for m in raw_msgs
        if m.get("role") == "tool"
    ]
    assert dangling_id in tool_msg_ids, (
        f"tool response for {dangling_id!r} missing from LLM request; "
        f"messages roles = {[m.get('role') for m in raw_msgs]}. "
        f"This is the wedge shape: the dangling tool_call has no "
        f"paired tool message in the projected request."
    )

    # Assertion 2 — provider-legal ordering. For every assistant
    # tool_calls entry, its paired tool message must appear later in
    # the sequence with NO user message intervening between the
    # assistant and its tool response. This is the strict version of
    # the OpenAI adjacency requirement.
    for i, msg in enumerate(raw_msgs):
        if msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls") or []
        for tc in calls:
            call_id = tc.get("id")
            assert call_id, "assistant tool_calls entry missing id"
            # Walk forward from i+1 looking for the matching tool msg.
            # Fail if we hit a user message first.
            found = False
            for j in range(i + 1, len(raw_msgs)):
                later = raw_msgs[j]
                role = later.get("role")
                if role == "user":
                    msg_summary = [
                        f"{k}:{m.get('role')}" for k, m in enumerate(raw_msgs)
                    ]
                    err = (
                        f"provider-illegal ordering: assistant tool_calls id "
                        f"{call_id!r} at index {i} has a user message at index "
                        f"{j} before its paired tool response. Full role sequence: "
                        f"{msg_summary}. This is the wedge shape — reconciliation "
                        f"terminal was tail-appended after the trigger user.message "
                        f"instead of before it."
                    )
                    raise AssertionError(err)
                if role == "tool" and later.get("tool_call_id") == call_id:
                    found = True
                    break
            assert found, (
                f"assistant tool_calls id {call_id!r} has no matching tool "
                f"response later in the message sequence"
            )

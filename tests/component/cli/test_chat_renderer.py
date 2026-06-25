"""Phase 2.1 Step 24c — renderer + engine-wiring component tests.

Three named cases per the build-doc's acceptance criterion:

- **TC-CHAT-01**: basic exchange. Feed "hello" to ``_run_input_loop``
  against the default-tier runtime (EchoLLMDriver + FakeFramework).
  Assert the renderer printed the agent's reply AND the JSONL
  contains the expected thread.exchange_* event sequence.
- **TC-CHAT-02**: tool-call block. Feed a crafted ``tool.called`` +
  ``tool.result`` pair to the renderer; assert the panel header
  carries the tool name + arguments and the body shows the
  truncated result. ``tool.failed`` renders in red.
- TC-CHAT-12 lives in ``test_chat_slash.py`` (it exercises the
  ``/tools`` slash command, not the renderer).

The renderer is a pure consumer (CHAT-R9 + AGENTS.md hard rule) —
its tests crafted events directly without bootstrapping the
engine. TC-CHAT-01 exercises the engine→renderer wiring; the
crafted-event tests exercise the per-event branches in
``Renderer._dispatch``.
"""

from __future__ import annotations

import io as _io
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from cli import chat
from cli._renderer import Renderer
from platform_runtime import bootstrap_runtime
from schemas import (
    Actor,
    Event,
    EventId,
    EventType,
    SessionId,
    ThreadId,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers — build crafted events + drive the renderer
# ---------------------------------------------------------------------------


def _make_event(
    type_: EventType,
    data: dict[str, Any],
    *,
    event_id: str | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
) -> Event:
    """Construct a minimal :class:`Event` for renderer-direct tests.

    The schema's prefixed-uuid identifiers reject ``ev_test``-shape
    strings; defaults below mint real uuids so callers don't have
    to think about it. The renderer cares about ``type`` + ``data``,
    not the identifier bodies."""
    return Event(
        event_id=EventId(event_id or f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=ThreadId(thread_id or f"th_{uuid4()}"),
        session_id=SessionId(session_id or f"ses_{uuid4()}"),
        type=type_,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def _iter_events(events: list[Event]) -> AsyncIterator[Event]:
    for event in events:
        yield event


async def _render_events(
    events: list[Event],
    *,
    agent_name: str = "Helper",
) -> str:
    """Run the renderer over the given crafted events; return what
    landed on stdout. Uses a width-narrowed Rich Console so panel
    text doesn't wrap unpredictably across CI."""
    out = _io.StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    renderer = Renderer(stdout=out, agent_name=agent_name, console=console)
    await renderer.render(_iter_events(events))
    return out.getvalue()


# ===========================================================================
# TC-CHAT-01 — basic exchange via real engine
# ===========================================================================


async def test_tc_chat_01_basic_exchange_routes_through_engine(
    chat_workspace: Path,
) -> None:
    """TC-CHAT-01: feed ``hello`` to the input loop; the renderer
    prints the EchoLLMDriver's reply and the session JSONL
    contains the expected ``thread.exchange_*`` event sequence.

    The renderer's job is to surface what the engine emitted —
    so the test asserts both: the rendered output AND the events
    persisted to the store. CHAT-R9 (renderer is a pure consumer
    of the engine event stream) is what makes the two consistent.
    """
    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="tc-chat-01",
    )
    threads = await runtime.store.list_threads(session.session_id)
    thread_id = threads[0].thread_id

    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        prompt_session = chat._build_prompt_session(
            input=pipe_input, output=DummyOutput()
        )
        pipe_input.send_text("hello\r")
        pipe_input.close()
        rc = await chat._run_input_loop(
            prompt_session,
            stdout=out,
            runtime=runtime,
            config=config,
            thread_id=thread_id,
        )

    assert rc == 0
    rendered = out.getvalue()
    # bug-chat-permissions-and-console-leak Step E: the chat no
    # longer echoes the user's submitted line to stdout itself.
    # In production prompt_toolkit's terminal echo lands "You:
    # hello" in the scrollback; in this harness DummyOutput
    # discards that echo, so the captured stdout MUST be free
    # of "You: hello". Pinning zero (rather than 1) is the
    # regression assertion for the duplicate-echo bug — see
    # also test_input_loop_does_not_explicitly_echo_user_input
    # below.
    assert rendered.count("You: hello") == 0
    # The renderer printed the agent's reply (EchoLLMDriver echoes).
    assert "Echo:" in rendered
    assert "hello" in rendered

    # The session JSONL has the expected event sequence — this is
    # the load-bearing CHAT-R9 check: what the chat showed is
    # derivable from what the engine emitted, not invented.
    events = [evt async for evt in runtime.store.read_path_to_leaf(thread_id)]
    event_types = [e.type for e in events]
    # The exchange landed at minimum a started → user.message →
    # model.request → model.response → agent.message → completed.
    assert EventType.THREAD_EXCHANGE_STARTED in event_types
    assert EventType.USER_MESSAGE in event_types
    assert EventType.MODEL_REQUEST in event_types
    assert EventType.MODEL_RESPONSE in event_types
    assert EventType.AGENT_MESSAGE in event_types
    assert EventType.THREAD_EXCHANGE_COMPLETED in event_types


# ===========================================================================
# TC-CHAT-02 — tool-call block (renderer-direct via crafted events)
# ===========================================================================


async def test_tc_chat_02_tool_call_panel_renders_with_header_and_result() -> None:
    """TC-CHAT-02: ``tool.called`` renders a panel with the tool
    name + arguments in its header; ``tool.result`` fills in
    the body. Panel header carries ``read_file(path='X')`` for
    the canonical case.
    """
    events = [
        _make_event(
            EventType.TOOL_CALLED,
            data={
                "tool_name": "read_file",
                "tool_call_id": "tc_001",
                "arguments": {"path": "README.md"},
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            data={
                "tool_call_id": "tc_001",
                "tool_name": "read_file",
                "result": {"content": "Kolsch is a local-first platform."},
            },
        ),
    ]
    rendered = await _render_events(events)
    # Panel header carries the tool name + the path argument.
    assert "read_file" in rendered
    assert "README.md" in rendered
    # Body carries the truncated result.
    assert "Kolsch is a local-first platform." in rendered


async def test_tc_chat_02_long_tool_result_truncates_to_20_lines() -> None:
    """Patch §2.3: tool-call panels truncate >20-line results to
    20 lines plus a ``[N more lines, /show <id> for full]`` suffix.
    """
    twenty_five_lines = "\n".join(f"line {i}" for i in range(25))
    events = [
        _make_event(
            EventType.TOOL_CALLED,
            data={
                "tool_name": "read_file",
                "tool_call_id": "tc_long",
                "arguments": {"path": "big.txt"},
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            data={
                "tool_call_id": "tc_long",
                "tool_name": "read_file",
                "result": {"content": twenty_five_lines},
            },
        ),
    ]
    rendered = await _render_events(events)
    assert "5 more lines" in rendered
    # The truncation suffix references the tool_call_id so the
    # user knows which block to expand.
    assert "tc_long" in rendered


async def test_tool_failed_renders_red_panel_with_error_details() -> None:
    """``tool.failed`` renders the panel in red with the error code
    + message visible (patch §2.2 row 5)."""
    events = [
        _make_event(
            EventType.TOOL_FAILED,
            data={
                "tool_call_id": "tc_err",
                "tool_name": "read_file",
                "error": {
                    "code": "file.not_found",
                    "message": "no such file: missing.md",
                },
            },
        ),
    ]
    rendered = await _render_events(events)
    assert "read_file" in rendered
    assert "file.not_found" in rendered
    assert "missing.md" in rendered


# ===========================================================================
# Per-event branches — exhaustive coverage of Renderer._dispatch
# ===========================================================================


async def test_thread_exchange_started_is_silent() -> None:
    rendered = await _render_events(
        [
            _make_event(
                EventType.THREAD_EXCHANGE_STARTED, data={"trigger_event_id": "ev_x"}
            ),
        ],
    )
    assert rendered == ""


async def test_user_message_is_silent() -> None:
    """The renderer treats ``user.message`` as a no-op (patch §3
    row 2). prompt_toolkit's terminal echo is the canonical
    scrollback "You: <text>" line; the renderer must NOT
    duplicate it. Verified jointly with
    test_input_loop_does_not_explicitly_echo_user_input — both
    silences are needed to keep the user's submission appearing
    exactly once."""
    rendered = await _render_events(
        [
            _make_event(EventType.USER_MESSAGE, data={"content": {"text": "hi"}}),
        ],
    )
    assert rendered == ""


async def test_input_loop_does_not_explicitly_echo_user_input(
    chat_workspace: Path,
) -> None:
    """bug-chat-permissions-and-console-leak Step E regression pin.

    Before the fix, ``_run_input_loop`` wrote
    ``f"You: {text}\\n"`` to its ``stdout`` parameter right after
    prompt_toolkit's submit. Combined with prompt_toolkit's own
    terminal echo of the finalized "You: <text>" line, real-TTY
    users saw the line twice. The fix deletes the explicit
    write; prompt_toolkit's echo is the sole source.

    This test exercises the input loop end-to-end against the
    EchoLLMDriver runtime with a DummyOutput-backed prompt
    session (prompt_toolkit's echo lands on the DummyOutput, not
    on the captured stdout). After the fix, the captured stdout
    contains NO "You: <text>" copy — pinning the bug as fixed.
    Before the fix, the captured stdout contained exactly one
    "You: <text>"; flipping back to one would fail this
    assertion.
    """
    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="step-e-regression",
    )
    threads = await runtime.store.list_threads(session.session_id)
    thread_id = threads[0].thread_id

    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        prompt_session = chat._build_prompt_session(
            input=pipe_input, output=DummyOutput()
        )
        pipe_input.send_text("regression-text\r")
        pipe_input.close()
        rc = await chat._run_input_loop(
            prompt_session,
            stdout=out,
            runtime=runtime,
            config=config,
            thread_id=thread_id,
        )

    assert rc == 0
    captured = out.getvalue()
    # The bug: chat.py wrote "You: regression-text\n" to stdout
    # in addition to prompt_toolkit's terminal echo. The fix
    # removes that write. Both the input-loop echo and the
    # renderer's user.message handler are silent — so the
    # captured stream has zero copies.
    assert "You: regression-text" not in captured
    # Agent reply still rendered (proves the loop ran, the
    # assertion above is not vacuous).
    assert "Echo: regression-text" in captured


async def test_model_request_prints_thinking_indicator() -> None:
    rendered = await _render_events(
        [
            _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_1"}),
        ],
    )
    # The indicator is subtle but non-empty. Defensive "<orphan>"
    # branch in _render_model_request: when no thread.exchange_started
    # has been seen yet (stream started mid-exchange), still render
    # once so the user isn't left with no indicator.
    assert rendered.strip() != ""


# ===========================================================================
# G2 — Per-exchange thinking-indicator gate
# bug-crewai-stdout-leak-investigation Step G2 / E-BUG-CREWAI-005
# ===========================================================================


_THINKING_GLYPH = "…"


async def test_thinking_indicator_fires_exactly_once_per_multi_tool_exchange() -> None:
    """G2 regression pin: the renderer's ``_THINKING_INDICATOR``
    must appear EXACTLY ONCE in a multi-tool-call exchange,
    regardless of how many ``model.request`` events the framework's
    tool-loop emits.

    Pre-fix (``bug-crewai-stdout-leak-investigation`` Finding F5),
    the indicator fired on every ``model.request``, producing N
    ellipsis lines between every panel — the user's reported
    "stray ellipses between event blocks" symptom. The fix tracks
    the current exchange id via ``thread.exchange_started`` and
    suppresses subsequent indicator fires within the same exchange.

    This test crafts the event sequence a framework's three-turn
    tool-call loop would emit (three ``model.request`` events
    interleaved with ``tool.called`` / ``tool.result``) and
    asserts ONE indicator in the captured output.
    """
    exchange_id = f"ev_{uuid4()}"
    events = [
        _make_event(
            EventType.THREAD_EXCHANGE_STARTED,
            data={"trigger_event_id": "ev_user"},
            event_id=exchange_id,
        ),
        _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_1"}),
        _make_event(
            EventType.TOOL_CALLED,
            data={
                "tool_name": "bash",
                "tool_call_id": "tc_1",
                "arguments": {"command": "ls"},
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            data={
                "tool_call_id": "tc_1",
                "tool_name": "bash",
                "result": {"stdout": "file1\nfile2\n", "stderr": "", "exit_code": 0},
            },
        ),
        _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_2"}),
        _make_event(
            EventType.TOOL_CALLED,
            data={
                "tool_name": "read_file",
                "tool_call_id": "tc_2",
                "arguments": {"path": "file1"},
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            data={
                "tool_call_id": "tc_2",
                "tool_name": "read_file",
                "result": {"content": "contents of file1"},
            },
        ),
        _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_3"}),
        _make_event(
            EventType.MODEL_RESPONSE,
            data={"content": {"text": "Here's what I found."}},
        ),
        _make_event(
            EventType.AGENT_MESSAGE,
            data={
                "content": {"text": "Here's what I found."},
                "exchange_id": exchange_id,
            },
        ),
        _make_event(
            EventType.THREAD_EXCHANGE_COMPLETED,
            data={
                "exchange_started_id": exchange_id,
                "agent_message_id": "ev_msg",
            },
        ),
    ]
    rendered = await _render_events(events)
    assert rendered.count(_THINKING_GLYPH) == 1, (
        f"thinking indicator fired {rendered.count(_THINKING_GLYPH)}x "
        f"in a three-turn tool-call exchange; G2's per-exchange gate "
        f"has regressed. Captured output:\n{rendered}"
    )


async def test_thinking_indicator_fires_again_for_next_exchange() -> None:
    """G2: the gate resets on ``thread.exchange_completed`` so the
    NEXT exchange's first ``model.request`` fires the indicator
    again. Drives two sequential exchanges through ONE renderer
    instance (the chat constructs a fresh Renderer per exchange
    in production, but the gate must work even if a renderer is
    re-used; coordinator D10 wires the gate via exchange_id,
    not via instance lifetime)."""
    exchange_1 = f"ev_{uuid4()}"
    exchange_2 = f"ev_{uuid4()}"
    events = [
        _make_event(
            EventType.THREAD_EXCHANGE_STARTED,
            data={"trigger_event_id": "ev_user1"},
            event_id=exchange_1,
        ),
        _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_1"}),
        _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_2"}),
        _make_event(
            EventType.MODEL_RESPONSE,
            data={"content": {"text": "first."}},
        ),
        _make_event(
            EventType.THREAD_EXCHANGE_COMPLETED,
            data={
                "exchange_started_id": exchange_1,
                "agent_message_id": "ev_msg1",
            },
        ),
        _make_event(
            EventType.THREAD_EXCHANGE_STARTED,
            data={"trigger_event_id": "ev_user2"},
            event_id=exchange_2,
        ),
        _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_3"}),
        _make_event(
            EventType.MODEL_RESPONSE,
            data={"content": {"text": "second."}},
        ),
        _make_event(
            EventType.THREAD_EXCHANGE_COMPLETED,
            data={
                "exchange_started_id": exchange_2,
                "agent_message_id": "ev_msg2",
            },
        ),
    ]
    rendered = await _render_events(events)
    # Two exchanges → two indicator fires (one per exchange,
    # despite the first exchange having two model.request events).
    assert rendered.count(_THINKING_GLYPH) == 2, (
        f"thinking indicator fired {rendered.count(_THINKING_GLYPH)}x "
        f"across two exchanges; expected exactly 2. "
        f"Captured output:\n{rendered}"
    )


async def test_thinking_indicator_resets_on_exchange_failed() -> None:
    """G2: ``thread.exchange_failed`` resets the gate same as
    ``thread.exchange_completed``. Otherwise a failed exchange
    would lock the gate against the recovery exchange's first
    ``model.request``."""
    exchange_1 = f"ev_{uuid4()}"
    exchange_2 = f"ev_{uuid4()}"
    events = [
        _make_event(
            EventType.THREAD_EXCHANGE_STARTED,
            data={"trigger_event_id": "ev_user1"},
            event_id=exchange_1,
        ),
        _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_1"}),
        _make_event(
            EventType.THREAD_EXCHANGE_FAILED,
            data={
                "exchange_started_id": exchange_1,
                "error": {"code": "model.timeout", "message": "timeout"},
            },
        ),
        _make_event(
            EventType.THREAD_EXCHANGE_STARTED,
            data={"trigger_event_id": "ev_user2"},
            event_id=exchange_2,
        ),
        _make_event(EventType.MODEL_REQUEST, data={"call_id": "ac_2"}),
        _make_event(
            EventType.MODEL_RESPONSE,
            data={"content": {"text": "recovered."}},
        ),
        _make_event(
            EventType.THREAD_EXCHANGE_COMPLETED,
            data={
                "exchange_started_id": exchange_2,
                "agent_message_id": "ev_msg",
            },
        ),
    ]
    rendered = await _render_events(events)
    # 1 indicator for exchange_1 + 1 for exchange_2 = 2 total.
    # If exchange_failed didn't reset the gate, the second
    # model.request would be suppressed and we'd see 1 only.
    assert rendered.count(_THINKING_GLYPH) == 2, (
        f"thinking indicator fired {rendered.count(_THINKING_GLYPH)}x; "
        f"expected 2 (one per exchange). G2's reset on "
        f"thread.exchange_failed has regressed. Output:\n{rendered}"
    )


async def test_model_response_renders_text_in_agent_capsule() -> None:
    """Per qol-chat-capsule-layout.md §Agent capsule — the agent's
    name appears in the capsule header ``<agent> · HH:MM:SS``;
    body lines carry the message text. The prior ``Helper:`` prose
    prefix is gone."""
    rendered = await _render_events(
        [
            _make_event(
                EventType.MODEL_RESPONSE,
                data={"content": {"text": "Sure thing."}},
            ),
        ],
        agent_name="Helper",
    )
    assert "Helper" in rendered
    assert "Sure thing." in rendered
    # Bar character marks the capsule.
    assert "│" in rendered


async def test_agent_message_dedupes_against_last_model_response() -> None:
    """If ``agent.message`` text matches what ``model.response``
    already rendered, ``agent.message`` no-ops (patch §3 row 9)."""
    same_text = "duplicate body."
    rendered = await _render_events(
        [
            _make_event(
                EventType.MODEL_RESPONSE,
                data={"content": {"text": same_text}},
            ),
            _make_event(
                EventType.AGENT_MESSAGE,
                data={
                    "content": {"text": same_text},
                    "exchange_id": "ev_x",
                },
            ),
        ],
    )
    # Body appears exactly once.
    assert rendered.count(same_text) == 1


async def test_agent_message_re_renders_when_differs_from_streamed() -> None:
    """When ``agent.message`` text DIFFERS from the streamed
    ``model.response``, the renderer re-prints (patch §3 row 9)."""
    rendered = await _render_events(
        [
            _make_event(
                EventType.MODEL_RESPONSE,
                data={"content": {"text": "draft"}},
            ),
            _make_event(
                EventType.AGENT_MESSAGE,
                data={
                    "content": {"text": "final answer"},
                    "exchange_id": "ev_x",
                },
            ),
        ],
    )
    assert "draft" in rendered
    assert "final answer" in rendered


async def test_thread_exchange_failed_engine_cancelled_renders_muted_suffix() -> None:
    """Patch §6.1: a cancellation specifically renders the
    in-progress agent block in muted styling with a
    ``[cancelled]`` suffix — distinct from the generic yellow
    "exchange failed" line that other failures get.

    The discriminator is the error code (``engine.cancelled``);
    other codes stay on the generic notice path.
    """
    # Pre-render a model.response so _last_agent_text is set;
    # the cancellation then surfaces the partial-block suffix.
    rendered = await _render_events(
        [
            _make_event(
                EventType.MODEL_RESPONSE,
                data={"content": {"text": "partial reply that"}},
            ),
            _make_event(
                EventType.THREAD_EXCHANGE_FAILED,
                data={
                    "exchange_started_id": "ev_x",
                    "error": {
                        "code": "engine.cancelled",
                        "message": "user cancelled",
                    },
                },
            ),
        ],
    )
    # Partial agent text + suffix both visible.
    assert "partial reply that" in rendered
    assert "[cancelled]" in rendered
    # NOT the generic exchange-failed yellow line.
    assert "exchange failed" not in rendered.lower()


async def test_thread_exchange_failed_engine_cancelled_without_partial_text() -> None:
    """Cancellation with no prior model.response — renderer
    still surfaces "[cancelled]" so the user sees explicit
    feedback rather than silence."""
    rendered = await _render_events(
        [
            _make_event(
                EventType.THREAD_EXCHANGE_FAILED,
                data={
                    "exchange_started_id": "ev_x",
                    "error": {
                        "code": "engine.cancelled",
                        "message": "cancelled before tokens",
                    },
                },
            ),
        ],
    )
    assert "[cancelled" in rendered


async def test_thread_exchange_failed_prints_system_notice() -> None:
    """Failed exchanges land as one-line system notices; the chat
    continues. Patch §6.3."""
    rendered = await _render_events(
        [
            _make_event(
                EventType.THREAD_EXCHANGE_FAILED,
                data={
                    "exchange_started_id": "ev_x",
                    "error": {
                        "code": "model.timeout",
                        "message": "upstream slow",
                    },
                },
            ),
        ],
    )
    assert "model.timeout" in rendered
    assert "exchange failed" in rendered.lower()


async def test_thread_exchange_completed_is_silent() -> None:
    rendered = await _render_events(
        [
            _make_event(
                EventType.THREAD_EXCHANGE_COMPLETED,
                data={
                    "exchange_started_id": "ev_x",
                    "agent_message_id": "ev_y",
                },
            ),
        ],
    )
    assert rendered == ""


async def test_model_fallback_renders_italic_notice() -> None:
    """Patch §3 row 12: ``model.fallback`` shows an italic notice
    naming the new model and the reason."""
    rendered = await _render_events(
        [
            _make_event(
                EventType.MODEL_FALLBACK,
                data={
                    "from": {"provider": "anthropic", "model": "opus"},
                    "to": {"provider": "anthropic", "model": "haiku"},
                    "reason": "model.rate_limited",
                    "attempt": 1,
                },
            ),
        ],
    )
    assert "haiku" in rendered
    assert "rate_limited" in rendered


async def test_diagnostic_events_are_silent() -> None:
    """``config.set`` / ``memory.*`` are diagnostic-only; the renderer
    never surfaces them. Patch §3 (last paragraph)."""
    rendered = await _render_events(
        [
            _make_event(
                EventType.CONFIG_SET,
                data={
                    "resolved_config": {"foo": "bar"},
                    "config_hash": "abc",
                },
            ),
        ],
    )
    assert rendered == ""


# ---------------------------------------------------------------------------
# Helpers — content-extraction defensive paths
# ---------------------------------------------------------------------------


async def test_renderer_handles_empty_agent_text_gracefully() -> None:
    """Empty content under ``model.response`` is treated as
    "nothing to render" rather than printing an empty agent block."""
    rendered = await _render_events(
        [
            _make_event(
                EventType.MODEL_RESPONSE,
                data={"content": {"text": ""}},
            ),
        ],
    )
    # No agent prefix was printed because there was nothing to render.
    assert "Helper:" not in rendered


async def test_renderer_stringifies_non_string_text_field() -> None:
    """Defensive: a numeric ``content.text`` stringifies rather
    than crashes the renderer."""
    rendered = await _render_events(
        [
            _make_event(
                EventType.MODEL_RESPONSE,
                data={"content": {"text": 42}},
            ),
        ],
    )
    assert "42" in rendered


async def test_renderer_handles_tool_result_with_stdout_key() -> None:
    """``bash``-style results land under ``stdout``; the renderer
    surfaces that key without falling through to the JSON dump."""
    events = [
        _make_event(
            EventType.TOOL_CALLED,
            data={
                "tool_name": "bash",
                "tool_call_id": "tc_b",
                "arguments": {"command": "echo hi"},
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            data={
                "tool_call_id": "tc_b",
                "tool_name": "bash",
                "result": {"stdout": "hi\n", "stderr": "", "exit_code": 0},
            },
        ),
    ]
    rendered = await _render_events(events)
    assert "hi" in rendered


async def test_renderer_falls_back_to_json_for_unknown_result_shape() -> None:
    """No recognised key → compact JSON dump so the user sees
    SOMETHING. The full payload always stays in the JSONL."""
    events = [
        _make_event(
            EventType.TOOL_CALLED,
            data={
                "tool_name": "weird",
                "tool_call_id": "tc_w",
                "arguments": {},
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            data={
                "tool_call_id": "tc_w",
                "tool_name": "weird",
                "result": {"unknown_key": ["a", "b"]},
            },
        ),
    ]
    rendered = await _render_events(events)
    assert "unknown_key" in rendered

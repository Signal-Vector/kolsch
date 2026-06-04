"""``/skill <ref>`` slash command — Step 3 force-invoke surface.

Three cases per the build prompt:

- Enabled ref → ``state.pending_force_skill`` carries the ref; the
  next submit's trigger flips to ``user_message_with_force_skill``.
- Not-enabled ref → friendly error; ``pending_force_skill`` stays
  empty so the next message is a regular ``user_message``.
- Two ``/skill`` calls before submit → both refs accumulate in
  declaration order; one exchange consumes them all.

Plus three "context shift drops pending" cases added per Step 3
review: ``/clear``, ``/new``, and ``/session <id>`` each drop
``pending_force_skill`` so a queued ref doesn't silently
force-load on the first message of the post-shift surface.

The handler + trigger builder are tested in isolation (no
``bootstrap_runtime``) for the chat-local cases; ``/new`` and
``/session`` use the ``chat_workspace`` fixture because they
mutate session state on a real :class:`platform_runtime.Store`.
"""

from __future__ import annotations

import io as _io
from typing import TYPE_CHECKING
from uuid import uuid4

from cli._slash import (
    ChatSessionState,
    ControlSignal,
    SlashContext,
    dispatch_slash,
)
from cli.chat import _build_user_trigger
from schemas import (
    AgentId,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    SessionId,
    SubsystemConfig,
    ThreadId,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures — tiny in-memory ChatSessionState + a config with two skills.
# ---------------------------------------------------------------------------


def _config_with_skills(enabled: list[str]) -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="1",
        agent_id=AgentId("ag_chat_skill"),
        name="test",
        description="test",
        model=ModelSpec(provider="fake", model="fake-model"),
        prompt="hi",
        io=IOSpec(mode="conversational"),
        subsystems={
            "skills": SubsystemConfig(
                variant="standard",
                config={"enabled": [{"ref": r} for r in enabled]},
            ),
        },
    )


def _state(config: ResolvedAgentConfig) -> ChatSessionState:
    # runtime is irrelevant for these tests — the /skill handler
    # never touches it. A None sentinel would force isinstance
    # checks in the handler; passing a sentinel object keeps the
    # dataclass invariant honest while staying inert.
    return ChatSessionState(
        runtime=object(),  # type: ignore[arg-type] — handler ignores it
        config=config,
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
    )


def _slash_ctx(state: ChatSessionState, out: _io.StringIO) -> SlashContext:
    return SlashContext(
        stdout=out,
        agent_config=state.config,
        agent_name=state.config.name,
        state=state,
    )


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


async def test_skill_enabled_ref_queues_for_next_message() -> None:
    """``/skill kolsch/X`` against an enabled ref appends to
    ``pending_force_skill``. The next submit's trigger flips to the
    force-invoke shape."""
    config = _config_with_skills(["kolsch/X"])
    state = _state(config)
    out = _io.StringIO()

    signal = await dispatch_slash("/skill kolsch/X", _slash_ctx(state, out))
    assert signal is ControlSignal.CONTINUE
    assert state.pending_force_skill == ["kolsch/X"]
    assert "queued kolsch/X" in out.getvalue()

    # Simulate the input loop's submit. The trigger MUST flip; the
    # source list MUST be cleared by the loop after consumption.
    trigger = _build_user_trigger("hello", state.pending_force_skill)
    state.pending_force_skill.clear()
    assert trigger == {
        "kind": "user_message_with_force_skill",
        "content": "hello",
        "force_skills": ["kolsch/X"],
    }
    assert state.pending_force_skill == []


async def test_skill_unknown_ref_prints_friendly_error_no_queue() -> None:
    """``/skill <not-enabled>`` prints a friendly error and leaves
    ``pending_force_skill`` empty. The next submit is a regular
    ``user_message`` — no force-invoke kicked off behind the user's
    back. This is the slash-site validation Step 3 §3 calls out
    explicitly: the engine would otherwise raise
    SkillForcedNotEnabled at pre-flight, which is Fatal."""
    config = _config_with_skills(["kolsch/X"])
    state = _state(config)
    out = _io.StringIO()

    signal = await dispatch_slash("/skill kolsch/Y", _slash_ctx(state, out))
    assert signal is ControlSignal.CONTINUE
    assert state.pending_force_skill == []
    rendered = out.getvalue()
    assert "not in this agent's enabled skills" in rendered
    assert "kolsch/X" in rendered  # the enabled list shown for context

    # Next submit goes through the regular user_message shape.
    trigger = _build_user_trigger("hello", state.pending_force_skill)
    assert trigger == {"kind": "user_message", "content": "hello"}


async def test_two_skill_calls_accumulate_in_declaration_order() -> None:
    """``/skill X`` then ``/skill Y`` before the next submit
    populates ``pending_force_skill = [X, Y]``. One exchange
    consumes both refs; the trigger preserves order so the engine's
    pre-flight emits the synthetic reads in the same order."""
    config = _config_with_skills(["kolsch/X", "kolsch/Y"])
    state = _state(config)
    out = _io.StringIO()
    ctx = _slash_ctx(state, out)

    await dispatch_slash("/skill kolsch/X", ctx)
    await dispatch_slash("/skill kolsch/Y", ctx)
    assert state.pending_force_skill == ["kolsch/X", "kolsch/Y"]

    trigger = _build_user_trigger("go", state.pending_force_skill)
    state.pending_force_skill.clear()
    assert trigger == {
        "kind": "user_message_with_force_skill",
        "content": "go",
        "force_skills": ["kolsch/X", "kolsch/Y"],
    }


async def test_bare_skill_command_prints_pending_or_help() -> None:
    """``/skill`` alone with nothing pending prints the usage line;
    with refs pending it surfaces the queue. Useful UX — lets the
    user check what they queued without re-typing the ref."""
    config = _config_with_skills(["kolsch/X"])
    state = _state(config)

    out = _io.StringIO()
    await dispatch_slash("/skill", _slash_ctx(state, out))
    assert "/skill <ref>" in out.getvalue()

    state.pending_force_skill.append("kolsch/X")
    out2 = _io.StringIO()
    await dispatch_slash("/skill", _slash_ctx(state, out2))
    assert "pending force-invoke" in out2.getvalue()
    assert "kolsch/X" in out2.getvalue()


async def test_skill_without_state_prints_clear_error() -> None:
    """``/skill`` requires a chat runtime — the SlashContext built by
    a test that omits ``state`` (e.g., the test surface for /help,
    /clear) gets a clear error rather than a crash. Mirrors the
    /new / /model / /thinking pattern in test_chat_slash.py."""
    out = _io.StringIO()
    signal = await dispatch_slash("/skill foo", SlashContext(stdout=out))
    assert signal is ControlSignal.CONTINUE
    assert "requires a chat runtime" in out.getvalue()


# ---------------------------------------------------------------------------
# Context-shift drops — /clear, /new, /session each reset pending refs.
# ---------------------------------------------------------------------------


async def test_clear_drops_pending_force_skill() -> None:
    """``/clear`` is a context-reset signal. Any /skill ref queued
    before the user clears the scrollback would silently force-load
    on their next message — they wouldn't see it in the visible
    state. Dropping pending refs keeps the in-memory state aligned
    with what's on screen."""
    config = _config_with_skills(["kolsch/X"])
    state = _state(config)
    state.pending_force_skill.append("kolsch/X")

    out = _io.StringIO()
    signal = await dispatch_slash("/clear", _slash_ctx(state, out))
    assert signal is ControlSignal.CONTINUE
    assert state.pending_force_skill == []


async def test_clear_without_state_does_not_crash() -> None:
    """The /clear handler must tolerate a state-less SlashContext —
    test surfaces for /help, /exit, etc. construct SlashContext
    without state. Pending-clear behaviour gracefully no-ops when
    state is absent."""
    out = _io.StringIO()
    # No state on the context; /clear must still succeed.
    signal = await dispatch_slash("/clear", SlashContext(stdout=out))
    assert signal is ControlSignal.CONTINUE
    # The ANSI clear sequence still landed.
    assert "\x1b[H\x1b[2J\x1b[3J" in out.getvalue()


async def test_new_session_drops_pending_force_skill(chat_workspace: Path) -> None:
    """``/new`` is a session-boundary shift. A /skill ref queued in
    the prior session must not silently force-load on the first
    message of the freshly-opened one. The clear happens inside
    ``_open_new_session`` alongside the session_id rebind."""
    from cli.chat import _open_new_session
    from platform_runtime import bootstrap_runtime

    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/helper")
    session = await runtime.store.create_session(
        str(config.agent_id), name="force-skill-new-pre"
    )
    threads = await runtime.store.list_threads(session.session_id)
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    # Queue a ref that's structurally valid (no enabled-list gating
    # at this layer — the handler does the validation; here we
    # just need a non-empty list to prove the clear happened).
    state.pending_force_skill.append("kolsch/some-ref")

    out = _io.StringIO()
    await _open_new_session(state, stdout=out, agent_ref="kolsch/helper")

    assert state.pending_force_skill == []
    # Sanity: the session actually shifted.
    assert state.session_id != session.session_id


async def test_switch_session_drops_pending_force_skill(
    chat_workspace: Path,
) -> None:
    """``/session <id>`` is the same kind of context shift as /new —
    a queued ref from the prior conversation must not leak into the
    one being switched to. The clear lands in ``_switch_session``
    next to the session_id rebind."""
    from cli.chat import _switch_session
    from platform_runtime import bootstrap_runtime

    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/helper")
    session_a = await runtime.store.create_session(
        str(config.agent_id), name="force-skill-switch-a"
    )
    session_b = await runtime.store.create_session(
        str(config.agent_id), name="force-skill-switch-b"
    )
    threads_a = await runtime.store.list_threads(session_a.session_id)
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session_a.session_id,
        thread_id=threads_a[0].thread_id,
    )
    state.pending_force_skill.append("kolsch/some-ref")

    out = _io.StringIO()
    await _switch_session(
        state, target_session_id=session_b.session_id, stdout=out
    )

    assert state.pending_force_skill == []
    assert state.session_id == session_b.session_id

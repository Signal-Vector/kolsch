"""spec-132 Step 4 — tests for `cli.chat.widgets.model_picker.open_model_picker`.

`open_model_picker` is the caller-side half of Step 4's selection
wiring: it pushes `ModelPickerScreen`, and if the user selected a row
(as opposed to Escape), converts the chosen `ModelChoice` into a
`ModelSpec` and calls the shared `_apply_model` (Step 1) — the same
function `/model <ref>` uses. `ModelPickerScreen` itself never touches
`_apply_model` or a `SlashContext` (see its class docstring); these
tests cover the layer that does.

Two mechanisms are tested per the spec's testing note:

- Selection: spy/mock `_apply_model` (patched at `cli._slash._apply_model`
  — `open_model_picker` resolves it via a lazy
  `from cli._slash import _apply_model` *inside* the function body, so
  the name is looked up on `cli._slash` fresh at call time; patching
  `cli._slash._apply_model` is what actually intercepts the call, not
  patching an attribute on `cli.chat.widgets.model_picker`, which
  never binds `_apply_model` at module scope) and assert it's called
  with the expected `ModelSpec(provider=..., model=...)` — built
  straight from the selected row's `ModelChoice`, no string
  round-trip.
- Escape: assert `_apply_model` is never called and `ctx.state` is
  untouched.

No real chat session is needed — `SlashContext`/`ChatSessionState` are
constructed directly (mirroring `test_model_handler_apply.py`'s
fixtures), and the picker itself is driven headlessly via a minimal
Textual `App.run_test()` harness (mirroring
`tests/cli/chat/widgets/test_model_picker.py`'s `_PickerHostApp`
pattern) so selection/Escape are real keyboard-driven Pilot
interactions, not hand-called internals.
"""

from __future__ import annotations

import io
from typing import Any, cast
from unittest import mock
from uuid import uuid4

import pytest
from textual.app import App

from cli._model_catalog import ModelChoice
from cli._slash import ChatSessionState, SlashContext
from cli.chat.widgets.model_picker import ModelPickerScreen, open_model_picker
from llm.protocol import ModelEntry
from schemas import AgentId, IOSpec, ModelSpec, ResolvedAgentConfig, SessionId, ThreadId


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="openrouter", model="anthropic/claude-3"),
        prompt="You are an agent.",
        io=IOSpec(mode="conversational"),
    )


def _state() -> ChatSessionState:
    return ChatSessionState(
        runtime=cast("Any", None),  # unused by open_model_picker/_apply_model
        config=_config(),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
    )


def _ctx(state: ChatSessionState) -> SlashContext:
    return SlashContext(stdout=io.StringIO(), state=state)


def _fixture_choices() -> list[ModelChoice]:
    return [
        ModelChoice(
            provider="claude_code",
            entry=ModelEntry(id="claude-sonnet-5", context_length=200_000),
        ),
        ModelChoice(
            provider="openrouter",
            entry=ModelEntry(id="gpt-5", context_length=400_000),
        ),
        ModelChoice(
            provider="opencode",
            entry=ModelEntry(id="local-model", context_length=None),
        ),
    ]


class _OpenPickerHostApp(App[None]):
    """Drives `open_model_picker` inside a real Textual app/worker.

    `open_model_picker` calls `app.push_screen_wait`, which (like
    `ModelPickerScreen`'s own Step 4 tests found) must run inside a
    Textual worker — `on_mount` schedules it via `run_worker` rather
    than awaiting it directly.
    """

    def __init__(self, ctx: SlashContext, choices: list[ModelChoice]) -> None:
        super().__init__()
        self._ctx = ctx
        self._choices = choices
        self.done = False

    def on_mount(self) -> None:
        self.run_worker(self._run(), exclusive=True)

    async def _run(self) -> None:
        await open_model_picker(self._ctx, self, self._choices)
        self.done = True


# ---------------------------------------------------------------------------
# Selection applies the model via the shared _apply_model function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selecting_a_row_calls_apply_model_with_expected_spec() -> None:
    """Confirming the highlighted row calls `_apply_model` with a `ModelSpec`
    built directly from that row's `ModelChoice` — no string round-trip."""
    choices = _fixture_choices()
    state = _state()
    ctx = _ctx(state)

    with mock.patch("cli._slash._apply_model", autospec=True) as mock_apply:
        app = _OpenPickerHostApp(ctx, choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ModelPickerScreen)

            # Move to row 1 (gpt-5 / openrouter) and confirm.
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

    mock_apply.assert_called_once_with(
        ctx, ModelSpec(provider="openrouter", model="gpt-5")
    )


@pytest.mark.asyncio
async def test_selecting_a_row_updates_session_state_like_slash_model_ref() -> None:
    """End-to-end (no `_apply_model` mock): `ChatSessionState.model_override`
    reflects the selected row's provider/model — identical to what typing
    the equivalent `/model <ref>` would produce (spec-132's verifiable
    data for Step 4)."""
    choices = _fixture_choices()
    state = _state()
    ctx = _ctx(state)

    app = _OpenPickerHostApp(ctx, choices)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # confirm the default-highlighted row (index 0)
        await pilot.pause()

    expected = ModelSpec(provider="claude_code", model="claude-sonnet-5")
    assert state.model_override == expected
    assert state.config.model == expected


# ---------------------------------------------------------------------------
# Escape never calls _apply_model and leaves state untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escape_never_calls_apply_model() -> None:
    choices = _fixture_choices()
    state = _state()
    ctx = _ctx(state)

    with mock.patch("cli._slash._apply_model", autospec=True) as mock_apply:
        app = _OpenPickerHostApp(ctx, choices)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ModelPickerScreen)

            await pilot.press("escape")
            await pilot.pause()

    mock_apply.assert_not_called()


@pytest.mark.asyncio
async def test_escape_leaves_session_state_unchanged() -> None:
    choices = _fixture_choices()
    state = _state()
    original_override = state.model_override
    original_config_model = state.config.model
    ctx = _ctx(state)

    app = _OpenPickerHostApp(ctx, choices)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert state.model_override is original_override
    assert state.config.model == original_config_model

"""spec-132 Step 5 — tests for `/model` (no-arg) opening the picker.

Before this step, `_handler_model`'s no-arg branch just printed the
current `provider/model` as text (covered by
`test_model_handler_apply.py`, pre-Step-5). This step replaces that
with opening `ModelPickerScreen`, populated by
`cli._model_catalog.assemble_model_choices()` over the session's
actually-configured providers (`ctx.state.runtime.llm_providers_config`)
— not a hardcoded list — and wired through `open_model_picker` so a
selection applies via the same `_apply_model` helper `/model <ref>`
uses (Step 1/4).

Two things need a real, running Textual `App` to exercise honestly:

- `_handler_model` resolves the app to push the modal onto via
  `textual._context.active_app.get()` — there is no `App` reference on
  `SlashContext` (`SlashContext` is deliberately narrow — see its
  docstring: "no store, no event bus, no engine handle"). This mirrors
  Textual's own idiom for "the currently running app" (used
  internally by `textual/_callback.py`, `textual/message_pump.py`,
  etc.) rather than threading an `App` parameter through the handler
  registry's `(arg_text, ctx) -> ControlSignal` signature that every
  other slash handler shares.
- `open_model_picker` itself calls `app.push_screen_wait`, which (per
  `test_open_model_picker.py`'s own host-app docstring) must run
  inside a Textual worker.

`assemble_model_choices` is patched at its source
(`cli._model_catalog.assemble_model_choices`) rather than injected via
its `connector_factory`/`credentials_resolver` seam — `_handler_model`
does a lazy `from cli._model_catalog import assemble_model_choices`
*inside* its own body (mirroring `open_model_picker`'s lazy
`_apply_model` import, per `test_open_model_picker.py`'s note), so the
name is looked up on `cli._model_catalog` fresh at call time; patching
the source module's attribute is what actually intercepts the call.

Covers:
- non-empty `llm_providers_config` + a stubbed catalog → the picker
  mounts, populated with the stub's rows (verifiable data for this
  step).
- the "current" marker passed to the picker is built from
  `ctx.state.config.model` (`provider`/`id` only).
- empty `llm_providers_config` → short-circuits to a plain message,
  no picker opened, `assemble_model_choices` never called (this
  step's chosen edge-case handling — see `_handler_model`'s
  docstring).
"""

from __future__ import annotations

import io
from typing import Any, cast
from unittest import mock
from uuid import uuid4

import pytest
from textual.app import App

from cli._model_catalog import ModelChoice
from cli._slash import ChatSessionState, ControlSignal, SlashContext, _handler_model
from cli.chat.widgets.model_picker import ModelPickerScreen
from llm.protocol import ModelEntry
from schemas import AgentId, IOSpec, ModelSpec, ResolvedAgentConfig, SessionId, ThreadId


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="claude_code", model="claude-sonnet-5"),
        prompt="You are an agent.",
        io=IOSpec(mode="conversational"),
    )


class _StubRuntime:
    """Minimal runtime stand-in exposing only `llm_providers_config` —
    the one field `_handler_model`'s no-arg branch reads directly (the
    rest of provider resolution happens inside the patched
    `assemble_model_choices`, so this stub never needs real
    `ProviderConfig`/`secrets` machinery)."""

    def __init__(self, llm_providers_config: dict[str, Any]) -> None:
        self.llm_providers_config = llm_providers_config


def _state(*, llm_providers_config: dict[str, Any] | None = None) -> ChatSessionState:
    runtime = _StubRuntime(llm_providers_config or {})
    return ChatSessionState(
        runtime=cast("Any", runtime),
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


class _ModelHandlerHostApp(App[None]):
    """Drives `_handler_model("", ctx)` inside a real Textual app/worker.

    `_handler_model`'s no-arg branch resolves `active_app.get()` and
    calls `open_model_picker`, which itself calls `app.push_screen_wait`
    — both need a real running app + worker context, mirroring
    `test_open_model_picker.py`'s `_OpenPickerHostApp`.
    """

    def __init__(self, ctx: SlashContext) -> None:
        super().__init__()
        self._ctx = ctx
        self.signal: ControlSignal | None = None

    def on_mount(self) -> None:
        self.run_worker(self._run(), exclusive=True)

    async def _run(self) -> None:
        self.signal = await _handler_model("", self._ctx)


# ---------------------------------------------------------------------------
# Non-empty providers → picker opens, populated from the stubbed catalog.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_no_arg_opens_picker_populated_from_stub() -> None:
    """`/model` with no argument, stubbed `llm_providers_config` +
    stubbed `assemble_model_choices`, mounts `ModelPickerScreen`
    populated with exactly the stub's rows."""
    state = _state(llm_providers_config={"claude_code": object()})
    ctx = _ctx(state)
    choices = _fixture_choices()

    with mock.patch(
        "cli._model_catalog.assemble_model_choices",
        autospec=True,
        return_value=choices,
    ) as mock_assemble:
        app = _ModelHandlerHostApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            # The picker was built with exactly the stub's choices, in order.
            assert screen._choices == choices

            await pilot.press("escape")
            await pilot.pause()

    mock_assemble.assert_awaited_once_with(ctx)
    assert app.signal is ControlSignal.CONTINUE


@pytest.mark.asyncio
async def test_model_no_arg_marks_current_model_from_session_config() -> None:
    """The `current` marker handed to the picker is built from
    `ctx.state.config.model` (`provider`/`id` only) — the row matching
    `_config()`'s `claude_code/claude-sonnet-5` renders as active."""
    state = _state(llm_providers_config={"claude_code": object()})
    ctx = _ctx(state)
    choices = _fixture_choices()

    with mock.patch(
        "cli._model_catalog.assemble_model_choices",
        autospec=True,
        return_value=choices,
    ):
        app = _ModelHandlerHostApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            expected_current = ModelChoice(
                provider="claude_code",
                entry=ModelEntry(id="claude-sonnet-5"),
            )
            assert screen._current == expected_current

            await pilot.press("escape")
            await pilot.pause()


@pytest.mark.asyncio
async def test_model_no_arg_selecting_a_row_applies_it() -> None:
    """End-to-end: selecting a row through the no-arg `/model` path
    updates session state exactly like the equivalent `/model <ref>`
    would (same `_apply_model` call Step 1/4 already cover)."""
    state = _state(llm_providers_config={"claude_code": object()})
    ctx = _ctx(state)
    choices = _fixture_choices()

    with mock.patch(
        "cli._model_catalog.assemble_model_choices",
        autospec=True,
        return_value=choices,
    ):
        app = _ModelHandlerHostApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Move to row 1 (openrouter/gpt-5) and confirm.
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

    expected = ModelSpec(provider="openrouter", model="gpt-5")
    assert state.model_override == expected
    assert state.config.model == expected
    assert app.signal is ControlSignal.CONTINUE


# ---------------------------------------------------------------------------
# Zero configured providers → short-circuit message, no picker opened.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_no_arg_with_zero_providers_never_opens_picker() -> None:
    """Empty `llm_providers_config` short-circuits to a plain message
    instead of a picker with nothing to show; `assemble_model_choices`
    is never called."""
    state = _state(llm_providers_config={})
    ctx = _ctx(state)

    with mock.patch(
        "cli._model_catalog.assemble_model_choices", autospec=True
    ) as mock_assemble:
        signal = await _handler_model("", ctx)

    mock_assemble.assert_not_called()
    assert signal is ControlSignal.CONTINUE
    assert (
        ctx.stdout.getvalue()
        == "/model: no providers configured; nothing to pick from.\n"
    )
    assert state.model_override is None

"""spec-132 Step 6 — loading-state UX + failure-surfacing tests.

Covers the acceptance bar for Step 6: opening `/model` never appears
to hang or freeze the UI during discovery, even when one provider's
discovery is slow or fails, and a per-provider discovery failure
doesn't prevent the picker from showing the other providers' models.

- A deliberately slow stub `assemble_catalog` (gated on an
  `asyncio.Event` the test controls): the UI stays responsive
  (`ModelPickerLoadingScreen` is mounted and its spinner ticks) for as
  long as assembly hasn't resolved, then swaps to the real picker once
  it has — never a frozen-looking gap.
- A deliberately failing stub `assemble_catalog` (mirroring what
  `cli._model_catalog.assemble_catalog` itself would return for a
  provider whose discovery raised): the OTHER providers' entries still
  populate the picker, and the failure is surfaced inline (not
  silently dropped) via `ModelPickerScreen`'s failure-note rendering.
- All-configured-providers-fail (zero `ModelChoice` rows, N
  `ProviderFailure`s): the picker still opens, with an inline
  "No models available" explanation rather than a silently empty list
  — Step 5 review carry-forward #1.
- `_handler_model`'s no-arg branch specifically catches `LookupError`
  from `textual._context.active_app.get()` (carried-forward item #2)
  and prints a clear message instead of leaking the raw `ContextVar`
  repr through the broad `except Exception` handler.

Mirrors `test_model_handler_opens_picker.py`'s fixtures/host-app
pattern (real Textual `App`/worker, `assemble_catalog` patched at its
`cli._model_catalog` source).
"""

from __future__ import annotations

import asyncio
import io
from typing import Any, cast
from unittest import mock
from uuid import uuid4

import pytest
from textual.app import App

from cli._model_catalog import ModelChoice, ProviderFailure
from cli._slash import ChatSessionState, ControlSignal, SlashContext, _handler_model
from cli.chat.widgets.model_picker import ModelPickerLoadingScreen, ModelPickerScreen
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
    the rest of provider resolution happens inside the patched
    `assemble_catalog`, so this stub never needs real
    `ProviderConfig`/`secrets` machinery."""

    def __init__(self, llm_providers_config: dict[str, Any]) -> None:
        self.llm_providers_config = llm_providers_config


def _state(*, llm_providers_config: dict[str, Any] | None = None) -> ChatSessionState:
    runtime = _StubRuntime(llm_providers_config or {"claude_code": object()})
    return ChatSessionState(
        runtime=cast("Any", runtime),
        config=_config(),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
    )


def _ctx(state: ChatSessionState) -> SlashContext:
    return SlashContext(stdout=io.StringIO(), state=state)


class _ModelHandlerHostApp(App[None]):
    """Drives `_handler_model("", ctx)` inside a real Textual app/worker.

    Mirrors `test_model_handler_opens_picker.py`'s host app.
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
# Slow discovery — the picker shows a loading screen, not a frozen gap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_discovery_shows_loading_screen_before_picker() -> None:
    """While a slow `assemble_catalog` hasn't resolved yet,
    `ModelPickerLoadingScreen` is the mounted screen — not a frozen
    gap and not (yet) the real picker. Releasing the gate swaps to the
    fully-populated `ModelPickerScreen`."""
    state = _state()
    ctx = _ctx(state)
    choices = [
        ModelChoice(
            provider="claude_code",
            entry=ModelEntry(id="claude-sonnet-5", context_length=200_000),
        ),
    ]
    release = asyncio.Event()

    async def _slow_assemble(
        _ctx: SlashContext,
    ) -> tuple[list[ModelChoice], list[ProviderFailure]]:
        await release.wait()
        return choices, []

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        side_effect=_slow_assemble,
    ):
        app = _ModelHandlerHostApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Assembly is still gated — the loading screen is up, the
            # real picker is not, and the app is still alive/pumping
            # messages (not blocked on a synchronous call).
            assert isinstance(app.screen, ModelPickerLoadingScreen)
            assert not isinstance(app.screen, ModelPickerScreen)

            # The spinner is genuinely ticking (proof the event loop
            # keeps running — a truly frozen UI wouldn't advance this).
            loading_screen = app.screen
            assert isinstance(loading_screen, ModelPickerLoadingScreen)
            first_frame = loading_screen._spinner_index
            await pilot.pause(0.2)
            assert isinstance(app.screen, ModelPickerLoadingScreen), (
                "still gated — must still be the loading screen"
            )
            assert loading_screen._spinner_index != first_frame, (
                "spinner must have advanced while assembly is in flight"
            )

            # Release the gate — assembly resolves, picker swaps in.
            release.set()
            await pilot.pause()
            assert isinstance(app.screen, ModelPickerScreen)
            assert app.screen._choices == choices

            await pilot.press("escape")
            await pilot.pause()

    assert app.signal is ControlSignal.CONTINUE


# ---------------------------------------------------------------------------
# One provider fails — the others still populate; failure is surfaced.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_provider_failure_still_shows_others_and_surfaces_note() -> None:
    """A partial failure (1 of 2 providers) doesn't hide the surviving
    provider's models, and the failure appears as an inline note —
    not silently dropped."""
    state = _state(llm_providers_config={"openrouter": object(), "broken": object()})
    ctx = _ctx(state)
    good_choices = [
        ModelChoice(
            provider="openrouter",
            entry=ModelEntry(id="gpt-5", context_length=400_000),
        ),
    ]
    failures = [ProviderFailure(provider="broken", reason="network down")]

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        return_value=(good_choices, failures),
    ):
        app = _ModelHandlerHostApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            # The surviving provider's model is still there.
            assert screen._choices == good_choices
            # The failure is carried through for inline display, not lost.
            assert screen._failures == failures

            await pilot.press("escape")
            await pilot.pause()

    assert app.signal is ControlSignal.CONTINUE


@pytest.mark.asyncio
async def test_all_providers_fail_picker_still_opens_with_explanation() -> None:
    """N configured providers, all N failed → zero `ModelChoice` rows,
    but the picker still opens (not a silent empty picker) with an
    inline explanation — Step 5 review carry-forward #1, distinct from
    the pre-existing "0 providers configured" short-circuit (that one
    never opens a picker at all; this one does, with a note)."""
    state = _state(llm_providers_config={"openrouter": object(), "opencode": object()})
    ctx = _ctx(state)
    failures = [
        ProviderFailure(provider="openrouter", reason="401 unauthorized"),
        ProviderFailure(provider="opencode", reason="connection refused"),
    ]

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        return_value=([], failures),
    ):
        app = _ModelHandlerHostApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            # The picker DID open (not the "zero providers configured"
            # short-circuit, which never opens anything).
            assert isinstance(screen, ModelPickerScreen)
            assert screen._choices == []
            assert screen._failures == failures

            await pilot.press("escape")
            await pilot.pause()

    assert app.signal is ControlSignal.CONTINUE
    # No "not available here" / "requires a chat runtime" style bail-out.
    assert ctx.stdout.getvalue() == ""


# ---------------------------------------------------------------------------
# LookupError from active_app.get() — carried-forward item #2.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_running_app_prints_clear_message_not_contextvar_repr() -> None:
    """Outside a running Textual App, `active_app.get()` raises
    `LookupError`. Before Step 6 this fell through to the broad
    `except Exception` and printed the raw `ContextVar` object repr
    (e.g. "could not open picker: <ContextVar name='active_app' ...>")
    — ugly and unhelpful. Step 6 catches `LookupError` specifically
    and prints a clear, actionable message instead."""
    state = _state()
    ctx = _ctx(state)

    with mock.patch(
        "cli._model_catalog.assemble_catalog", autospec=True
    ) as mock_assemble:
        # No `app.run_test()` here at all — genuinely no running App,
        # so `textual._context.active_app.get()` raises LookupError
        # for real (not mocked), exactly like the carried-forward
        # finding describes.
        signal = await _handler_model("", ctx)

    assert signal is ControlSignal.CONTINUE
    assert ctx.stdout.getvalue() == "/model: requires a running chat session\n"
    # Must fail before ever reaching catalog assembly.
    mock_assemble.assert_not_called()

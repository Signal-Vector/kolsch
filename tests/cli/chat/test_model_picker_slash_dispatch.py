"""`/model` (no-arg) end-to-end through the REAL `ChatApp` dispatch path.

Post-ship bug fix: `_handler_model`'s no-arg branch used to try to open
the picker itself (spec-132 Steps 5/6), which needs
`App.push_screen_wait` — Textual hard-requires that to run inside a
worker. Every test covering this before drove `_handler_model`
directly from a synthetic host app that happened to ALSO wrap the call
in its own `run_worker(...)`, so every test passed while the real
`ChatApp` (which never wraps slash-command dispatch in a worker) raised
`NoActiveWorker` — "push_screen must be run from a worker when
wait_for_dismiss is True" — on every real `/model` invocation.

A first fix attempt wrapped the call in `app.run_worker(...)` and
*awaited* its result from inside `_handler_model` — which deadlocks:
`_handler_model` runs synchronously inside `ChatApp.on_composer_submitted`,
one of the App's own message handlers, and Textual's message pump
dispatches handlers strictly sequentially
(`textual/message_pump.py:654`, `_process_messages_loop` awaits
`_dispatch_message` fully before fetching the next message) — so a
handler that awaits a worker's completion blocks the very message
processing (the keypress that dismisses the picker) the worker needs to
finish. Confirmed via a minimal repro before this file was written:
awaiting `Worker.wait()` from a message handler hangs forever; a
DETACHED worker (started but never awaited by the caller) resolves
correctly.

The real fix: `/model` with no argument is now intercepted in
`ChatApp._handle_slash` (like `/export`, `/why`, `/reload`, `/compact`
— see the "Textual-local intercepts" comment there) and routed to
`ChatApp._handle_model_picker`, which fires `_run_model_picker` as a
DETACHED worker (`self.run_worker(...)`, never awaited) — matching this
codebase's existing `_run_exchange` (`@work(exclusive=True)`)
fire-and-forget pattern. `_handler_model` itself no longer attempts to
open the picker at all (see its docstring in `cli._slash`); `/model
<ref>` (WITH an argument) is untouched, still dispatched generically.

This file therefore drives everything through a REAL `ChatApp`, posting
real `Composer.Submitted("/model")` messages exactly like
`tests/cli/chat/test_composer_submitted_exit_verbs.py` does — the
strongest available proof this doesn't deadlock, since it exercises the
actual production call path (`on_composer_submitted` -> `_handle_slash`
-> `_handle_model_picker`) rather than a synthetic stand-in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from cli._model_catalog import ModelChoice, ProviderFailure
from cli.chat.app import ChatApp
from cli.chat.widgets.composer import Composer
from cli.chat.widgets.model_picker import ModelPickerLoadingScreen, ModelPickerScreen
from cli.chat.widgets.notice import NoticeWidget
from llm.protocol import ModelEntry
from schemas import AgentId, IOSpec, ModelSpec, ResolvedAgentConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


async def _empty_async_gen(
    *_args: Any,
    **_kwargs: Any,
) -> AsyncGenerator[Any, None]:
    """Async generator that yields nothing — same pattern as
    `test_composer_submitted_exit_verbs.py`."""
    return
    yield  # type: ignore[unreachable]  # pragma: no cover


class _MockStore:
    read_path_to_leaf = staticmethod(_empty_async_gen)


class _MockAuthz:
    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    execute_stream = staticmethod(_empty_async_gen)


class _MockRuntime:
    """Extends `test_composer_submitted_exit_verbs.py`'s minimal mock
    with `llm_providers_config` — the one field `_handle_model_picker`
    reads directly (the rest of provider resolution happens inside
    `assemble_catalog`, patched at its source in every test below, so
    this mock never needs real `ProviderConfig`/`secrets` machinery)."""

    def __init__(self, llm_providers_config: dict[str, Any] | None = None) -> None:
        self.store = _MockStore()
        self.authz = _MockAuthz()
        self.engine = _MockEngine()
        self.llm_providers_config = (
            llm_providers_config
            if llm_providers_config is not None
            else {"claude_code": object()}
        )


@dataclass
class _MockConfig:
    name: str = "TestAgent"
    agent_id: str = "ag_mock"
    model: Any = None


@dataclass
class _MockBundle:
    runtime: Any
    config: Any
    session_id: str
    thread_id: str
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="claude_code", model="claude-sonnet-5"),
        prompt="You are an agent.",
        io=IOSpec(mode="conversational"),
    )


def _make_app(
    tmp_path: Path, *, llm_providers_config: dict[str, Any] | None = None
) -> ChatApp:
    runtime = _MockRuntime(llm_providers_config)
    bundle = _MockBundle(
        runtime=runtime,
        config=_config(),
        session_id="ses_test",
        thread_id="th_test",
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(session=bundle, engine=runtime.engine, full_history=False)


async def _submit(pilot: Any, text: str) -> None:
    """Post a real `Composer.Submitted(text)` message and pump the loop —
    the exact same path a real keystroke drives in production."""
    composer = pilot.app.query_one(Composer)
    composer.post_message(Composer.Submitted(text))
    await pilot.pause()


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


def _notice_texts(app: ChatApp) -> list[str]:
    transcript = app.query_one("#transcript")
    return [str(n.text) for n in transcript.query(NoticeWidget)]


# ---------------------------------------------------------------------------
# The core regression: /model opens the picker with zero deadlock, driven
# through the real, un-worker-wrapped ChatApp dispatch path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_no_arg_opens_picker_through_real_dispatch(tmp_path: Path) -> None:
    """Submitting `/model` through the real `Composer.Submitted` ->
    `on_composer_submitted` -> `_handle_slash` -> `_handle_model_picker`
    chain opens the picker, populated from the stub — and, critically,
    does not hang. Before the fix this either raised `NoActiveWorker`
    (original bug) or deadlocked (first fix attempt); this test would
    time out under either regression."""
    app = _make_app(tmp_path, llm_providers_config={"claude_code": object()})
    choices = _fixture_choices()

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        return_value=(choices, []),
    ) as mock_assemble:
        async with app.run_test() as pilot:
            await asyncio.wait_for(_submit(pilot, "/model"), timeout=5)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            assert screen._choices == choices

            await pilot.press("escape")
            await pilot.pause()

    mock_assemble.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_no_arg_marks_current_model_from_session_config(
    tmp_path: Path,
) -> None:
    """The `current` marker handed to the picker is built from
    `ctx.state.config.model` (`provider`/`id` only) — the row matching
    `_config()`'s `claude_code/claude-sonnet-5` renders as active."""
    app = _make_app(tmp_path, llm_providers_config={"claude_code": object()})
    choices = _fixture_choices()

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        return_value=(choices, []),
    ):
        async with app.run_test() as pilot:
            await asyncio.wait_for(_submit(pilot, "/model"), timeout=5)
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
async def test_model_no_arg_selecting_a_row_applies_it(tmp_path: Path) -> None:
    """End-to-end: selecting a row through the real `/model` no-arg path
    updates session state exactly like the equivalent `/model <ref>`
    would (same `_apply_model` call Step 1/4 already cover)."""
    app = _make_app(tmp_path, llm_providers_config={"claude_code": object()})
    choices = _fixture_choices()

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        return_value=(choices, []),
    ):
        async with app.run_test() as pilot:
            await asyncio.wait_for(_submit(pilot, "/model"), timeout=5)
            await pilot.pause()
            # Move to row 1 (openrouter/gpt-5) and confirm.
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

    expected = ModelSpec(provider="openrouter", model="gpt-5")
    assert app._chat_state is not None
    assert app._chat_state.model_override == expected
    assert app._chat_state.config.model == expected


@pytest.mark.asyncio
async def test_model_no_arg_escape_leaves_state_unchanged(tmp_path: Path) -> None:
    """Escape closes the picker with no `_apply_model` call and no
    state mutation."""
    app = _make_app(tmp_path, llm_providers_config={"claude_code": object()})
    choices = _fixture_choices()

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        return_value=(choices, []),
    ):
        async with app.run_test() as pilot:
            await asyncio.wait_for(_submit(pilot, "/model"), timeout=5)
            await pilot.pause()
            assert isinstance(app.screen, ModelPickerScreen)

            await pilot.press("escape")
            await pilot.pause()

    assert app._chat_state is not None
    assert app._chat_state.model_override is None


# ---------------------------------------------------------------------------
# Zero configured providers -> notice, no picker opened.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_no_arg_with_zero_providers_never_opens_picker(
    tmp_path: Path,
) -> None:
    """Empty `llm_providers_config` mounts a plain transcript notice
    instead of opening a picker with nothing to show;
    `assemble_catalog` is never called."""
    app = _make_app(tmp_path, llm_providers_config={})

    with mock.patch(
        "cli._model_catalog.assemble_catalog", autospec=True
    ) as mock_assemble:
        async with app.run_test() as pilot:
            await asyncio.wait_for(_submit(pilot, "/model"), timeout=5)
            await pilot.pause()
            assert not isinstance(app.screen, ModelPickerScreen)
            assert (
                "/model: no providers configured; nothing to pick from."
                in _notice_texts(app)
            )

    mock_assemble.assert_not_called()


# ---------------------------------------------------------------------------
# Worker error path -> transcript notice, not a swallowed/lost failure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_no_arg_catalog_assembly_failure_mounts_notice(
    tmp_path: Path,
) -> None:
    """An unexpected exception escaping `assemble_catalog` (a genuine
    bug, not a per-provider failure — those are already caught and
    turned into `ProviderFailure`s inside `assemble_catalog` itself) is
    reported via a mounted transcript notice, since `_run_model_picker`
    runs detached and nothing else is left to catch it."""
    app = _make_app(tmp_path, llm_providers_config={"claude_code": object()})

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        side_effect=RuntimeError("boom"),
    ):
        async with app.run_test() as pilot:
            await asyncio.wait_for(_submit(pilot, "/model"), timeout=5)
            await pilot.pause()

            assert not isinstance(app.screen, ModelPickerScreen)
            notices = _notice_texts(app)
            assert any(
                "/model: could not open picker:" in text and "boom" in text
                for text in notices
            )


# ---------------------------------------------------------------------------
# Step 6 loading-state UX + failure surfacing, relocated from
# tests/cli/_slash/test_model_handler_loading.py (same assertions, driven
# through the real dispatch path instead of a synthetic host app).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_discovery_shows_loading_screen_before_picker(
    tmp_path: Path,
) -> None:
    """While a slow `assemble_catalog` hasn't resolved yet,
    `ModelPickerLoadingScreen` is the mounted screen — not a frozen gap
    and not (yet) the real picker. Releasing the gate swaps to the
    fully-populated `ModelPickerScreen`."""
    app = _make_app(tmp_path, llm_providers_config={"claude_code": object()})
    choices = [
        ModelChoice(
            provider="claude_code",
            entry=ModelEntry(id="claude-sonnet-5", context_length=200_000),
        ),
    ]
    release = asyncio.Event()

    async def _slow_assemble(_ctx: Any) -> tuple[list[ModelChoice], list[Any]]:
        await release.wait()
        return choices, []

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        side_effect=_slow_assemble,
    ):
        async with app.run_test() as pilot:
            await _submit(pilot, "/model")
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


@pytest.mark.asyncio
async def test_one_provider_failure_still_shows_others_and_surfaces_note(
    tmp_path: Path,
) -> None:
    """A partial failure (1 of 2 providers) doesn't hide the surviving
    provider's models, and the failure appears as an inline note — not
    silently dropped."""
    app = _make_app(
        tmp_path,
        llm_providers_config={"openrouter": object(), "broken": object()},
    )
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
        async with app.run_test() as pilot:
            await asyncio.wait_for(_submit(pilot, "/model"), timeout=5)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            # The surviving provider's model is still there.
            assert screen._choices == good_choices
            # The failure is carried through for inline display, not lost.
            assert screen._failures == failures

            await pilot.press("escape")
            await pilot.pause()


@pytest.mark.asyncio
async def test_all_providers_fail_picker_still_opens_with_explanation(
    tmp_path: Path,
) -> None:
    """N configured providers, all N failed -> zero `ModelChoice` rows,
    but the picker still opens (not a silent empty picker) with an
    inline explanation — distinct from the "0 providers configured"
    short-circuit (that one never opens a picker at all; this one
    does, with a note)."""
    app = _make_app(
        tmp_path,
        llm_providers_config={"openrouter": object(), "opencode": object()},
    )
    failures = [
        ProviderFailure(provider="openrouter", reason="401 unauthorized"),
        ProviderFailure(provider="opencode", reason="connection refused"),
    ]

    with mock.patch(
        "cli._model_catalog.assemble_catalog",
        autospec=True,
        return_value=([], failures),
    ):
        async with app.run_test() as pilot:
            await asyncio.wait_for(_submit(pilot, "/model"), timeout=5)
            await pilot.pause()
            screen = app.screen
            # The picker DID open (not the "zero providers configured"
            # short-circuit, which never opens anything).
            assert isinstance(screen, ModelPickerScreen)
            assert screen._choices == []
            assert screen._failures == failures
            # No "chat runtime not ready" / "no providers configured"
            # bail-out notice — the picker itself opened.
            assert not any(
                "no providers configured" in text for text in _notice_texts(app)
            )

            await pilot.press("escape")
            await pilot.pause()

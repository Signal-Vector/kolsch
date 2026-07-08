"""bug-023 — `ChatApp.on_composer_submitted` bare exit-verb branch.

Users coming from ``python`` / ``bash`` / ``sqlite3`` / ``psql`` /
``node`` / ``ipython`` reflexively type ``exit`` or ``quit``
(without a leading slash) to leave the REPL. Before bug-023 that
prose was routed straight to the model as a ``USER_MESSAGE``.

This test file spies on ``_handle_slash`` and ``_run_exchange`` to
prove:

- ``exit`` / ``quit`` / ``:q`` / ``:wq`` (any case, with
  surrounding whitespace) → ``_handle_slash("/exit")`` runs and
  ``_run_exchange`` does NOT.
- Prose like ``"I exit the room"`` is NOT treated as a quit;
  ``_run_exchange`` runs normally.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cli.chat.app import ChatApp
from cli.chat.widgets.composer import Composer


async def _empty_async_gen(
    *_args: Any, **_kwargs: Any,
) -> AsyncGenerator[Any, None]:
    """Async generator that yields nothing.

    Same pattern as ``tests/cli/chat/test_approval_flow_live.py`` —
    the ``yield`` after a bare ``return`` is what makes this a
    generator (Python's syntactic rule) rather than a coroutine.
    """
    return
    yield  # type: ignore[unreachable]  # pragma: no cover


class _FakeStore:
    read_path_to_leaf = staticmethod(_empty_async_gen)


class _FakeAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


class _FakeEngine:
    execute_stream = staticmethod(_empty_async_gen)


class _FakeRuntime:
    def __init__(self) -> None:
        self.store = _FakeStore()
        self.authz = _FakeAuthz()
        self.engine = _FakeEngine()


@dataclass
class _FakeConfig:
    name: str = "TestAgent"
    agent_id: str = "ag_fake"
    model: Any = None


@dataclass
class _FakeBundle:
    runtime: Any
    config: Any
    session_id: str
    thread_id: str
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


def _make_app(tmp_path: Path) -> ChatApp:
    bundle = _FakeBundle(
        runtime=_FakeRuntime(),
        config=_FakeConfig(),
        session_id="ses_test",
        thread_id="th_test",
        agent_ref="kolsch/agents/fake",
        root=tmp_path,
        model_label="fake/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle, engine=bundle.runtime.engine, full_history=False,
    )


def _install_spies(app: ChatApp) -> tuple[list[str], list[str]]:
    """Replace `_handle_slash` and `_run_exchange` with recording spies.

    Both spies short-circuit (do NOT call originals) so tests can
    assert routing without the app trying to actually exit / dispatch
    a slash / talk to the engine. Returns the two capture lists.
    """
    slash_captured: list[str] = []
    exchange_captured: list[str] = []

    async def _spy_slash(text: str) -> None:
        slash_captured.append(text)

    def _spy_exchange(text: str) -> None:
        exchange_captured.append(text)

    app._handle_slash = _spy_slash  # type: ignore[method-assign]
    # `_run_exchange` is wrapped by `@work`, which flips its declared
    # return type to `Worker[...]`; the spy is a plain
    # `Callable[[str], None]` (mypy complains about the assignment
    # type). The behaviour we want to observe is only that the spy
    # RUNS at all — for the test scope that's a sound override.
    app._run_exchange = _spy_exchange  # type: ignore[method-assign,assignment]
    return slash_captured, exchange_captured


async def _submit(pilot: Any, text: str) -> None:
    """Post a `Composer.Submitted(text)` message and pump the loop."""
    composer = pilot.app.query_one(Composer)
    composer.post_message(Composer.Submitted(text))
    await pilot.pause()


@pytest.mark.asyncio
async def test_submitting_exit_calls_handle_slash_exit(
    tmp_path: Path,
) -> None:
    """A bare ``exit`` submission routes to ``_handle_slash("/exit")``."""
    app = _make_app(tmp_path)
    slash_captured, exchange_captured = _install_spies(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, "exit")
    assert slash_captured == ["/exit"]
    assert exchange_captured == []


@pytest.mark.asyncio
async def test_submitting_quit_calls_handle_slash_exit(
    tmp_path: Path,
) -> None:
    """A bare ``quit`` submission routes to ``_handle_slash("/exit")``."""
    app = _make_app(tmp_path)
    slash_captured, exchange_captured = _install_spies(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, "quit")
    assert slash_captured == ["/exit"]
    assert exchange_captured == []


@pytest.mark.asyncio
async def test_submitting_colon_q_calls_handle_slash_exit(
    tmp_path: Path,
) -> None:
    """Both vim-style verbs (``:q`` and ``:wq``) route to ``/exit``."""
    app = _make_app(tmp_path)
    slash_captured, exchange_captured = _install_spies(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, ":q")
        await _submit(pilot, ":wq")
    assert slash_captured == ["/exit", "/exit"]
    assert exchange_captured == []


@pytest.mark.asyncio
async def test_submitting_exit_capitalized(tmp_path: Path) -> None:
    """Case-insensitive + whitespace-tolerant matching.

    ``EXIT``, ``  Exit  ``, ``QuIt`` — all resolve to ``/exit``.
    """
    app = _make_app(tmp_path)
    slash_captured, exchange_captured = _install_spies(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, "EXIT")
        await _submit(pilot, "  Exit  ")
        await _submit(pilot, "QuIt")
    assert slash_captured == ["/exit", "/exit", "/exit"]
    assert exchange_captured == []


@pytest.mark.asyncio
async def test_submitting_exit_prose_still_sent(tmp_path: Path) -> None:
    """Prose containing ``exit`` is NOT treated as a quit verb.

    Only exact matches after ``strip()`` + ``casefold()`` count.
    Anything longer — including ``"I exit the room"`` and
    ``"quit playing games"`` — falls through to
    ``_run_exchange`` (the normal engine path).
    """
    app = _make_app(tmp_path)
    slash_captured, exchange_captured = _install_spies(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, "I exit the room")
        await _submit(pilot, "quit playing games")
        await _submit(pilot, "exit stage left")
    assert slash_captured == []
    assert exchange_captured == [
        "I exit the room",
        "quit playing games",
        "exit stage left",
    ]

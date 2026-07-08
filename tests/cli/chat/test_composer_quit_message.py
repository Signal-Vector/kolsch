"""bug-023 — `ChatApp.on_composer_quit_requested` dispatch test.

The composer posts ``Composer.QuitRequested`` when the user hits
``ctrl+d`` on an empty composer (see
``test_composer_exit_key.py``). This test asserts that the
message is picked up by the app's handler AND that the handler
delegates to ``action_quit_if_idle`` (the single-quit-site
invariant — the handler does NOT call ``self.exit(0)`` directly,
so the "only quit when idle" guard stays in one place).
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


def _make_bundle(tmp_path: Path) -> _FakeBundle:
    return _FakeBundle(
        runtime=_FakeRuntime(),
        config=_FakeConfig(),
        session_id="ses_test",
        thread_id="th_test",
        agent_ref="kolsch/agents/fake",
        root=tmp_path,
        model_label="fake/test",
        is_resume=False,
    )


@pytest.mark.asyncio
async def test_quit_requested_message_calls_action_quit_if_idle(
    tmp_path: Path,
) -> None:
    """Posting `Composer.QuitRequested` fires `action_quit_if_idle` once.

    Spies on the action before boot so the pre- and post-message
    call counts are unambiguous. The empty composer at test start
    means the action WILL run ``self.exit(0)`` — so we also assert
    the app is no longer running once the pilot finishes.
    """
    bundle = _make_bundle(tmp_path)
    app = ChatApp(
        session=bundle, engine=bundle.runtime.engine, full_history=False,
    )

    call_count = 0
    original_action = app.action_quit_if_idle

    def _spy() -> None:
        nonlocal call_count
        call_count += 1
        original_action()

    app.action_quit_if_idle = _spy  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        # Sanity: the composer is present and empty.
        composer = pilot.app.query_one(Composer)
        assert composer.text == ""
        assert call_count == 0

        composer.post_message(Composer.QuitRequested())
        await pilot.pause()

    assert call_count == 1
    # App exited (empty composer → action_quit_if_idle → self.exit(0)).
    assert not app.is_running

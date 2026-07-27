"""Test 13 — headless construction (CI smoke).

Constructs ``ChatApp`` with mocks, drives it via ``Pilot``, and
presses ``ctrl+d`` to assert the app exits cleanly without a real
TTY.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.app import ChatApp

if TYPE_CHECKING:
    from pathlib import Path


class _MockStore:
    async def read_path_to_leaf(self, thread_id: Any) -> Any:
        if False:
            yield  # pragma: no cover
        return


class _MockAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None
    ) -> None:
        return None

    def clear_prompt_callback(self) -> None:
        return None


class _MockRuntime:
    def __init__(self) -> None:
        self.store = _MockStore()
        self.authz = _MockAuthz()
        self.engine = _MockEngine()


class _MockEngine:
    async def execute_stream(self, config: Any, ctx: Any):
        if False:
            yield  # pragma: no cover


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


@pytest.mark.asyncio
async def test_chat_app_can_be_constructed_and_quit(tmp_path: Path) -> None:
    """Pilot presses ctrl+d on an idle composer; app exits cleanly."""
    bundle = _MockBundle(
        runtime=_MockRuntime(),
        config=_MockConfig(),
        session_id="ses_test",
        thread_id="th_test",
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+d")
        await pilot.pause()
    assert not app.is_running

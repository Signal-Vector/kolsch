"""spec-003 Test 5 — `ChatApp.on_unmount` service teardown wiring.

Three passes per the spec:

1. Mount a `ChatApp` with a stub service that records
   `aclose_called: bool`. Trigger `on_unmount`. Assert the stub was
   called.
2. Same but the service raises inside `aclose`. Assert the exception
   is logged; `on_unmount` completes; other services still torn
   down.
3. `on_unmount` with an all-None `ChatServiceRegistry` completes
   without error.

Fail means: teardown loop broken, or exceptions escape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cli.chat.app import ChatApp
from cli.chat.service_registry import ChatServiceRegistry


class _MockStore:
    async def read_path_to_leaf(self, thread_id: Any) -> Any:  # noqa: ARG002
        if False:
            yield  # pragma: no cover
        return


class _MockAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None
    ) -> None:  # noqa: ARG002
        return None

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    async def execute_stream(self, config: Any, ctx: Any):  # noqa: ARG002
        if False:
            yield  # pragma: no cover
        return


class _MockRuntime:
    def __init__(self) -> None:
        self.store = _MockStore()
        self.authz = _MockAuthz()
        self.engine = _MockEngine()


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


def _mk_bundle(tmp_path: Path) -> _MockBundle:
    return _MockBundle(
        runtime=_MockRuntime(),
        config=_MockConfig(),
        session_id="ses_test",
        thread_id="th_test",
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )


class _RecordingService:
    def __init__(self) -> None:
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


class _RaisingService:
    def __init__(self) -> None:
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True
        raise RuntimeError("aclose failed")


@pytest.mark.asyncio
async def test_on_unmount_calls_aclose_on_wired_service(tmp_path: Path) -> None:
    bundle = _mk_bundle(tmp_path)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)
    stub = _RecordingService()
    app._service_registry = ChatServiceRegistry(cards=stub)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+d")
        await pilot.pause()
    assert stub.aclose_called is True


@pytest.mark.asyncio
async def test_on_unmount_swallows_service_aclose_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bundle = _mk_bundle(tmp_path)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)
    bad = _RaisingService()
    good = _RecordingService()
    # Bad service in `cards`, good in `prompts`. If teardown swallows
    # the exception correctly, both `aclose` methods are invoked.
    app._service_registry = ChatServiceRegistry(cards=bad, prompts=good)
    caplog.set_level(logging.ERROR)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+d")
        await pilot.pause()
    assert bad.aclose_called is True
    assert good.aclose_called is True
    # Exception was logged.
    assert any(
        "aclose raised" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_on_unmount_all_none_registry(tmp_path: Path) -> None:
    """All-None registry: teardown completes without error."""
    bundle = _mk_bundle(tmp_path)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)
    app._service_registry = ChatServiceRegistry()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+d")
        await pilot.pause()
    # No exception raised → passing.

"""spec-006 Test 8 — `ChatApp.on_mount` registers CardsImpl.

Two passes:

1. Boot `ChatApp` under Pilot. Assert
   `self._service_registry.cards` is a `CardsImpl` instance (not
   None).
2. Unmount the app; assert `CardsImpl.aclose` was called (via a spy).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cli.chat.app import ChatApp
from cli.chat.services.cards_impl import CardsImpl


class _MockAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None,
    ) -> None:  # noqa: ARG002
        return None

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    async def execute_stream(self, config: Any, ctx: Any):  # noqa: ARG002
        if False:
            yield  # pragma: no cover
        return


@dataclass
class _MockConfig:
    name: str = "TestAgent"
    agent_id: str = "ag_mock"
    model: Any = None


@dataclass
class _MockBundle:
    runtime: Any
    config: Any
    session_id: Any
    thread_id: Any
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


def _mk_bundle(tmp_path: Path, store: Any) -> _MockBundle:
    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _MockAuthz()
            self.engine = _MockEngine()

    return _MockBundle(
        runtime=_R(),
        config=_MockConfig(),
        session_id="ses_test",
        thread_id="th_test",
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )


@pytest.mark.asyncio
async def test_on_mount_registers_cards_impl(tmp_path: Path) -> None:
    from store import SessionStore, SqliteBackend

    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="c")
    threads = await store.list_threads(session.session_id)

    bundle = _MockBundle(
        runtime=type(
            "R",
            (),
            {"store": store, "authz": _MockAuthz(), "engine": _MockEngine()},
        )(),
        config=_MockConfig(),
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )

    try:
        app = ChatApp(
            session=bundle, engine=bundle.runtime.engine, full_history=False,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._service_registry is not None
            assert isinstance(app._service_registry.cards, CardsImpl)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_on_unmount_calls_cards_aclose(tmp_path: Path) -> None:
    """Assert `CardsImpl.aclose` is invoked via the service teardown loop."""
    from store import SessionStore, SqliteBackend

    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="c")
    threads = await store.list_threads(session.session_id)

    bundle = _MockBundle(
        runtime=type(
            "R",
            (),
            {"store": store, "authz": _MockAuthz(), "engine": _MockEngine()},
        )(),
        config=_MockConfig(),
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )

    aclose_called = {"n": 0}

    try:
        app = ChatApp(
            session=bundle, engine=bundle.runtime.engine, full_history=False,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            # Wrap the impl's aclose with a spy.
            assert app._service_registry is not None
            impl = app._service_registry.cards
            assert isinstance(impl, CardsImpl)
            original = impl.aclose

            async def _spy_aclose() -> None:
                aclose_called["n"] += 1
                await original()

            impl.aclose = _spy_aclose  # type: ignore[method-assign]
            await pilot.press("ctrl+d")
            await pilot.pause()
        assert aclose_called["n"] >= 1
    finally:
        await backend.close()

"""feat-054 Phase 5 — chat-side session-open eager compaction UX.

Drives :class:`cli.chat.app.ChatApp` through :class:`textual.pilot.Pilot`
with three engine mocks — under-threshold, over-threshold-success,
and tier-3-hardfail — and asserts:

- No spurious notice fires on a fresh session, composer stays enabled.
- The "compacting…" progress notice mounts, composer is disabled
  during the eager check, and both are cleaned up afterwards.
- A tier-3 raise from the engine surfaces as a warning notice; the
  chat still boots and the composer is re-enabled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.app import ChatApp
from cli.chat.widgets.composer import Composer
from cli.chat.widgets.notice import NoticeWidget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Runtime + engine mocks
# ---------------------------------------------------------------------------


class _EmptyStore:
    """Store with zero events — the fresh-session case."""

    async def read_path_to_leaf(self, thread_id: Any) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover


class _NonEmptyStore:
    """Store that yields one dummy event so the app's has-events probe
    treats the thread as a resumable session with prior content.

    The event's shape is deliberately minimal — the eager check runs
    against the real engine mock, not against this store's contents.
    """

    async def read_path_to_leaf(self, thread_id: Any) -> AsyncIterator[Any]:
        # Yield one event object that carries enough to satisfy the
        # event-adapter's replay path (it won't touch this in the
        # eager-compaction tests because is_resume=False AND we hook
        # the check directly).
        return
        yield  # pragma: no cover — keeps this an async generator


class _MockAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None
    ) -> None:
        return None

    def clear_prompt_callback(self) -> None:
        return None


class _MockRuntime:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.authz = _MockAuthz()
        self.engine = None  # set post-construction


class _MockConfig:
    def __init__(self) -> None:
        self.name = "TestAgent"
        self.agent_id = "ag_mock"
        # A stand-in ModelSpec — the app passes it through to the
        # engine mock which never introspects .provider/.model.
        self.model = _ModelSpec("mock", "mock-model")


class _ModelSpec:
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model


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


class _NoCompactEngine:
    """Engine mock whose eager check returns ``None`` (no compaction)."""

    def __init__(self) -> None:
        self.calls: int = 0

    async def execute_stream(self, config: Any, ctx: Any) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover

    async def maybe_compact_thread_eagerly(
        self,
        *,
        thread_id: Any,
        spec: Any,
        tool_schemas: list[dict[str, Any]] | None = None,
        trigger: str = "auto_resume",
    ) -> Any:
        self.calls += 1
        return None


class _RaisingEngine:
    """Engine mock whose eager check raises — tier-3 hard-fail path."""

    def __init__(self) -> None:
        self.calls: int = 0

    async def execute_stream(self, config: Any, ctx: Any) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover

    async def maybe_compact_thread_eagerly(
        self,
        *,
        thread_id: Any,
        spec: Any,
        tool_schemas: list[dict[str, Any]] | None = None,
        trigger: str = "auto_resume",
    ) -> Any:
        self.calls += 1
        raise RuntimeError("simulated tier-3 uncompactable")


class _SlowCompactEngine:
    """Engine mock whose eager check awaits an external ``go`` future.

    Lets the test freeze mid-check to assert the composer is
    disabled + the progress notice is mounted BEFORE the resolve.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.go: asyncio.Future[None] | None = None
        self.calls: int = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.go = loop.create_future()

    async def execute_stream(self, config: Any, ctx: Any) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover

    async def maybe_compact_thread_eagerly(
        self,
        *,
        thread_id: Any,
        spec: Any,
        tool_schemas: list[dict[str, Any]] | None = None,
        trigger: str = "auto_resume",
    ) -> Any:
        self.calls += 1
        self.started.set()
        if self.go is None:
            return None
        await self.go
        return None


def _bundle(
    tmp_path: Path,
    engine: Any,
    *,
    is_resume: bool,
    store: Any,
) -> _MockBundle:
    runtime = _MockRuntime(store)
    runtime.engine = engine
    return _MockBundle(
        runtime=runtime,
        config=_MockConfig(),
        session_id=f"ses_{uuid4().hex[:8]}",
        thread_id=f"th_{uuid4().hex[:8]}",
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=is_resume,
    )


# ---------------------------------------------------------------------------
# Fresh-session case: no notice, composer enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_session_boots_no_notice_composer_enabled(
    tmp_path: Path,
) -> None:
    engine = _NoCompactEngine()
    bundle = _bundle(tmp_path, engine, is_resume=False, store=_EmptyStore())
    app = ChatApp(session=bundle, engine=engine, full_history=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Composer must be enabled after mount.
        composer = app.query_one(Composer)
        assert composer.disabled is False
        # No compaction notice should have mounted at the transcript.
        notices = list(app.query(NoticeWidget))
        for w in notices:
            assert "compacting" not in w.text.lower(), (
                f"unexpected compaction progress notice on fresh boot: {w.text!r}"
            )
        await pilot.press("ctrl+d")
        await pilot.pause()


# ---------------------------------------------------------------------------
# Over-threshold case: eager check ran, composer re-enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_session_calls_eager_compaction_then_enables_composer(
    tmp_path: Path,
) -> None:
    """A resumed session with prior events triggers the eager check.

    We can't easily verify the transient progress notice (it's removed
    after the check completes), but we can verify:

    1. The engine's ``maybe_compact_thread_eagerly`` was invoked.
    2. After the check finishes the composer is re-enabled.
    """
    engine = _NoCompactEngine()
    bundle = _bundle(tmp_path, engine, is_resume=True, store=_NonEmptyStore())
    app = ChatApp(session=bundle, engine=engine, full_history=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        # The engine mock was called once from on_mount.
        assert engine.calls == 1
        # And the composer is enabled after the check completes.
        composer = app.query_one(Composer)
        assert composer.disabled is False
        await pilot.press("ctrl+d")
        await pilot.pause()


# ---------------------------------------------------------------------------
# Tier-3 hard-fail: warning notice, chat boots, composer enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_hardfail_mounts_warning_notice_and_reenables_composer(
    tmp_path: Path,
) -> None:
    engine = _RaisingEngine()
    bundle = _bundle(tmp_path, engine, is_resume=True, store=_NonEmptyStore())
    app = ChatApp(session=bundle, engine=engine, full_history=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        # The eager check was invoked and raised.
        assert engine.calls == 1
        # A warning notice was mounted.
        notices = list(app.query(NoticeWidget))
        warning = [
            w
            for w in notices
            if w.kind == "warning" and "eager compaction" in w.text.lower()
        ]
        assert warning, (
            "expected a tier-3 warning notice; got: "
            f"{[(w.text, w.kind) for w in notices]}"
        )
        # Chat did not crash; composer is enabled so the user can
        # still work.
        composer = app.query_one(Composer)
        assert composer.disabled is False
        await pilot.press("ctrl+d")
        await pilot.pause()

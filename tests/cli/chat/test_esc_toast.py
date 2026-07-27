"""feat-019 Test 3 — Esc-Esc toast state machine (Pilot).

Six passes covering the ``ChatApp.on_key`` state machine + StatusBar
integration:

1. Butler turn active + no workflow → Esc cancels the turn; no toast
   fires.
2. Butler turn active + workflow live → Esc cancels the turn AND the
   3s toast appears on the StatusBar.
3. Second Esc within the window → oldest workflow cancels; toast
   dismisses.
4. Waiting past the 3s window → toast auto-dismisses; second Esc
   returns to FIFO.
5. No butler turn + workflow live → first Esc cancels the workflow
   directly (no toast).
6. Multiple workflows + butler turn → first Esc cancels butler turn;
   second Esc cancels the OLDEST workflow (started_at ASC).

Fixtures use a stub ``WorkflowsImpl`` so the state machine can be
exercised without booting a real workflow executor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from textual import events

from cli.chat.app import _ESC_TOAST_TEXT, ChatApp
from cli.chat.service_registry import ChatServiceRegistry
from cli.chat.services.workflows_impl import WorkflowRunRecord
from cli.chat.widgets.status_bar import StatusBar
from schemas.identifiers import SessionId, ThreadId
from tools.services.workflows import NodeCounts
from workflows.ids import WorkflowRunId

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime.now(tz=UTC)


class _MockAuthz:
    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        return None

    def clear_prompt_callback(self) -> None:
        return None


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
    session_id: Any
    thread_id: Any
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


@dataclass
class _MockStore:
    """Minimal store stub — read_path_to_leaf yields nothing."""

    async def read_path_to_leaf(self, _thread_id: Any):
        if False:
            yield  # pragma: no cover

    async def append_event(self, _thread_id: Any, _event: Any) -> None:
        return None


class _StubWorkflowsImpl:
    """Bare-bones stand-in for :class:`WorkflowsImpl`.

    Exposes just enough surface for :meth:`ChatApp._active_workflows`
    / :meth:`ChatApp._find_oldest_running_workflow` /
    :meth:`ChatApp._has_live_workflow` — the ``_workflow_registry``
    dict plus an async ``cancel(run_id)`` method the state machine
    awaits.
    """

    def __init__(self) -> None:
        self._workflow_registry: dict[WorkflowRunId, WorkflowRunRecord] = {}
        self.cancel_calls: list[tuple[WorkflowRunId, bool]] = []

    def add_record(
        self,
        *,
        suffix: str,
        phase: str = "running",
        started_offset: int = 0,
    ) -> WorkflowRunRecord:
        rid = WorkflowRunId(
            f"wrk_00000000-0000-0000-0000-0000000000{suffix}",
        )
        rec = WorkflowRunRecord(
            run_id=rid,
            ref="test/ref",
            phase=phase,  # type: ignore[arg-type]
            card=None,
            created_at=_NOW,
            started_at=_NOW - timedelta(seconds=started_offset),
            node_counts=NodeCounts(total=3, completed=1),
        )
        self._workflow_registry[rid] = rec
        return rec

    async def cancel(
        self,
        run_id: WorkflowRunId,
        *,
        cascade: bool = False,
    ) -> bool:
        self.cancel_calls.append((run_id, cascade))
        rec = self._workflow_registry.get(run_id)
        if rec is not None:
            rec.phase = "cancelling"  # type: ignore[assignment]
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def bundle(tmp_path: Path) -> _MockBundle:
    class _R:
        def __init__(self) -> None:
            self.store = _MockStore()
            self.authz = _MockAuthz()
            self.engine = _MockEngine()

    return _MockBundle(
        runtime=_R(),
        config=_MockConfig(),
        session_id=SessionId("ses_00000000-0000-0000-0000-000000000001"),
        thread_id=ThreadId("th_00000000-0000-0000-0000-000000000001"),
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )


async def _make_app(
    bundle: _MockBundle,
    workflows: _StubWorkflowsImpl,
) -> ChatApp:
    """Construct a ChatApp with the stub workflows pre-registered."""
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )
    # Pre-seed the service registry so on_mount preserves our stub.
    app._service_registry = ChatServiceRegistry(
        cards=None,
        prompts=None,
        workflows=workflows,
    )
    return app


async def _press_escape(app: ChatApp) -> None:
    """Fire an ``escape`` key event through the app's on_key handler."""
    event = events.Key(key="escape", character=None)
    await app.on_key(event)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_esc_with_butler_only_no_toast(bundle: _MockBundle) -> None:
    """Butler turn + no workflows → Esc cancels turn; no toast."""
    workflows = _StubWorkflowsImpl()
    app = await _make_app(bundle, workflows)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Simulate an active butler turn.
        class _MockToken:
            def cancel(self) -> None:
                pass

        app.active_cancel = _MockToken()

        await _press_escape(app)
        await pilot.pause()

        assert not app._esc_toast_active
        assert workflows.cancel_calls == []
        # Butler turn cancel fired: last_cancel_reason set.
        assert app.last_cancel_reason == "interrupted"


@pytest.mark.asyncio
async def test_esc_with_butler_and_workflow_shows_toast(
    bundle: _MockBundle,
) -> None:
    """Butler + workflow → Esc cancels turn AND the toast appears."""
    workflows = _StubWorkflowsImpl()
    workflows.add_record(suffix="a1", phase="running", started_offset=10)
    app = await _make_app(bundle, workflows)
    async with app.run_test() as pilot:
        await pilot.pause()

        class _MockToken:
            def cancel(self) -> None:
                pass

        app.active_cancel = _MockToken()

        await _press_escape(app)
        await pilot.pause()

        assert app._esc_toast_active
        # StatusBar picked up the toast text.
        status_bar = app.query_one("#status-bar", StatusBar)
        assert status_bar.toast_text == _ESC_TOAST_TEXT
        # Workflow was NOT yet cancelled.
        assert workflows.cancel_calls == []


@pytest.mark.asyncio
async def test_second_esc_within_window_cancels_workflow(
    bundle: _MockBundle,
) -> None:
    """Second Esc inside the 3s window cancels the oldest workflow."""
    workflows = _StubWorkflowsImpl()
    workflows.add_record(suffix="b1", phase="running", started_offset=5)
    app = await _make_app(bundle, workflows)
    async with app.run_test() as pilot:
        await pilot.pause()

        class _MockToken:
            def cancel(self) -> None:
                pass

        app.active_cancel = _MockToken()

        # First Esc — cancels butler; toast on.
        await _press_escape(app)
        await pilot.pause()
        assert app._esc_toast_active

        # Second Esc within window — cancels workflow; toast off.
        await _press_escape(app)
        await pilot.pause()
        assert not app._esc_toast_active
        assert len(workflows.cancel_calls) == 1


@pytest.mark.asyncio
async def test_toast_auto_dismisses_after_window(
    bundle: _MockBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After 3s the toast auto-dismisses via the timer callback."""
    # Shorten the window for the test.
    from cli.chat import app as app_module

    monkeypatch.setattr(app_module, "_ESC_TOAST_SECONDS", 0.05)

    workflows = _StubWorkflowsImpl()
    workflows.add_record(suffix="c1", phase="running", started_offset=5)
    app = await _make_app(bundle, workflows)
    async with app.run_test() as pilot:
        await pilot.pause()

        class _MockToken:
            def cancel(self) -> None:
                pass

        app.active_cancel = _MockToken()

        # First Esc — toast fires.
        await _press_escape(app)
        await pilot.pause()
        assert app._esc_toast_active

        # Wait past the shortened window.
        await asyncio.sleep(0.10)
        await pilot.pause()

        assert not app._esc_toast_active
        # Second Esc AFTER the window — no butler turn now
        # (still active_cancel), but toast is off so we take the FIFO
        # branch (butler first). Simulate no butler active this time by
        # clearing the token first.
        app.active_cancel = None
        await _press_escape(app)
        await pilot.pause()
        # Expect direct workflow cancel via FIFO.
        assert len(workflows.cancel_calls) == 1


@pytest.mark.asyncio
async def test_esc_with_workflow_only_cancels_directly(
    bundle: _MockBundle,
) -> None:
    """No butler turn + workflow live → Esc cancels workflow (no toast)."""
    workflows = _StubWorkflowsImpl()
    workflows.add_record(suffix="d1", phase="running", started_offset=8)
    app = await _make_app(bundle, workflows)
    async with app.run_test() as pilot:
        await pilot.pause()

        # No active butler turn.
        assert app.active_cancel is None

        await _press_escape(app)
        await pilot.pause()

        assert not app._esc_toast_active
        assert len(workflows.cancel_calls) == 1


@pytest.mark.asyncio
async def test_multiple_workflows_second_esc_cancels_oldest(
    bundle: _MockBundle,
) -> None:
    """Multiple workflows: second Esc cancels the OLDEST (started_at ASC)."""
    workflows = _StubWorkflowsImpl()
    # Youngest first — 5s ago.
    workflows.add_record(suffix="e1", phase="running", started_offset=5)
    # Oldest — 30s ago.
    r_old = workflows.add_record(
        suffix="e2",
        phase="running",
        started_offset=30,
    )
    # Middle — 15s ago.
    workflows.add_record(suffix="e3", phase="running", started_offset=15)
    app = await _make_app(bundle, workflows)
    async with app.run_test() as pilot:
        await pilot.pause()

        class _MockToken:
            def cancel(self) -> None:
                pass

        app.active_cancel = _MockToken()

        # First Esc — cancels butler; toast on.
        await _press_escape(app)
        await pilot.pause()

        # Second Esc — cancels OLDEST.
        await _press_escape(app)
        await pilot.pause()

        assert len(workflows.cancel_calls) == 1
        assert workflows.cancel_calls[0][0] == r_old.run_id

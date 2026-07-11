"""``ForceKillConfirm`` — modal confirmation for the ``x`` force-kill (feat-019).

Mounted by :meth:`cli.chat.widgets.card_widget.CardWidget.action_force_kill`
when a focused workflow card has been in ``cancelling`` state for more
than 5 seconds and the user presses ``x`` — the executor is unwinding
but seems stuck, and the user wants to escalate to a cascade cancel.

State machine (feat-019 §Scope §`x` force-kill sketch + §Coordinator
Q4 lock):

- On mount, renders ``Cancel this workflow AND N related runs? [y/n]``.
- ``y`` — calls ``Workflows.cancel(run_id, cascade=True)`` and schedules
  a 500ms fallback that emits a synthetic
  ``workflow.run.aborted(reason="force")`` if the executor hasn't
  produced a terminal event by then. Unmount happens after the cancel
  fires (before the fallback awaits).
- ``n`` / ``escape`` — unmounts without cancelling.

Coordinator Q4 lock: the 500ms fallback is a separate scheduled task
inside the widget, NOT a check against ``time_in_state`` — the timing
lives here so the escalation path is self-contained.

The widget is a plain ``Vertical`` so it composes into the transcript's
mount surface without a modal screen. Sizing / border driven by
DEFAULT_CSS; the widget is small (~1 question row + hint) and doesn't
need a scroll.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

if TYPE_CHECKING:
    from tools.services.workflows import WorkflowRunId


_LOGGER = logging.getLogger(__name__)


_FALLBACK_ABORT_SECONDS = 0.5
"""Delay before emitting the synthetic ``workflow.run.aborted`` fallback.

Coordinator Q4 lock: scheduled here from the widget so the escalation
path is self-contained. Post-cancel, wait ``0.5s`` for the executor's
own terminal event to land; if it hasn't, synthesise the aborted
event so the card closes cleanly (avoids indefinitely-stuck cards).
"""


class ForceKillConfirm(Vertical):
    """Modal-ish confirm widget for cascade cancel of a stuck workflow.

    Constructor:
        run_id: The focused card's workflow run id — passed to
            ``workflows.cancel(cascade=True)`` and used to synthesise
            the fallback event.
        n_related: Cardinality shown in the question — ``len(cascade_tree)
            - 1`` computed by the caller before the mount.
        workflows_impl: The ``WorkflowsImpl`` handle; the widget calls
            ``cancel(run_id, cascade=True)`` on ``y``. Kept as ``Any``
            so headless tests can pass a stub.
        session_store: Optional handle used to synthesise the
            ``workflow.run.aborted`` fallback event when the executor
            stalls past the 500ms threshold. ``None`` skips the
            synthetic emit — the cascade cancel still fires.
        thread_id / session_id: Optional identifiers passed to the
            synthetic event when a session store is wired. Skipping
            them collapses the emit path.
    """

    DEFAULT_CSS = """
    ForceKillConfirm {
        border: round $warning;
        padding: 0 1;
        margin: 1 0;
        height: auto;
        background: $panel;
    }
    ForceKillConfirm > #force-kill-question {
        color: $warning;
        text-style: bold;
    }
    ForceKillConfirm > #force-kill-hint {
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("y", "confirm", "Confirm force-kill"),
        ("n", "cancel", "Cancel"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        *,
        run_id: "WorkflowRunId",
        n_related: int,
        workflows_impl: Any,
        session_store: Any = None,
        thread_id: Any = None,
        session_id: Any = None,
        id: str | None = None,  # noqa: A002 — Textual kwarg
    ) -> None:
        super().__init__(id=id)
        self._run_id = run_id
        self._n_related = n_related
        self._workflows_impl = workflows_impl
        self._session_store = session_store
        self._thread_id = thread_id
        self._session_id = session_id
        # feat-019 AC #10 fallback handle; assigned in `action_confirm`
        # so `action_cancel` can defensively clear it (not strictly
        # needed since cancel doesn't schedule it, but keeps the
        # contract symmetric).
        self._fallback_handle: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        suffix = "run" if self._n_related == 1 else "runs"
        n = self._n_related
        if n <= 0:
            question = "Cancel this workflow?"
        else:
            question = (
                f"Cancel this workflow AND {n} related {suffix}?"
            )
        yield Static(question, id="force-kill-question", markup=False)
        yield Static(
            "Press [y] to confirm, [n] or Esc to cancel.",
            id="force-kill-hint",
            markup=False,
        )

    def on_mount(self) -> None:
        """Focus the widget so the ``y`` / ``n`` bindings fire."""
        # Textual routes key events to the focused widget; make sure
        # this modal has focus on mount so the bindings work without a
        # click.
        try:
            self.focus()
        except Exception:
            # Focus can raise on some headless test paths; the widget
            # still processes `on_key` via message bubble.
            pass

    def can_focus(self) -> bool:  # type: ignore[override]
        return True

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def action_confirm(self) -> None:
        """``y`` — fire cascade cancel + schedule 500ms fallback."""
        await self._fire_cancel_and_fallback()
        await self._unmount_safely()

    async def action_cancel(self) -> None:
        """``n`` / ``escape`` — unmount without cancelling."""
        if self._fallback_handle is not None:
            with _suppress():
                self._fallback_handle.cancel()
            self._fallback_handle = None
        await self._unmount_safely()

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    async def _fire_cancel_and_fallback(self) -> None:
        """Cascade cancel + schedule the synthetic-aborted fallback.

        The cascade cancel is awaited so the ``WorkflowsImpl`` state
        transition (``cancelling`` → propagate) is committed before
        the caller's ``await`` returns. The 500ms fallback is fired
        via ``call_later`` so the widget's action returns promptly.
        """
        cancel = getattr(self._workflows_impl, "cancel", None)
        if cancel is None:
            _LOGGER.debug(
                "ForceKillConfirm: workflows_impl has no cancel(); skipping",
            )
            return
        try:
            await cancel(self._run_id, cascade=True)
        except Exception:  # noqa: BLE001 — cancel is best-effort
            _LOGGER.exception(
                "ForceKillConfirm: cascade cancel raised for %s",
                self._run_id,
            )

        # Coordinator Q4 lock: separate scheduled task for the
        # synthetic aborted fallback.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._fallback_handle = loop.call_later(
            _FALLBACK_ABORT_SECONDS,
            lambda: loop.create_task(self._emit_synthetic_aborted()),
        )

    async def _emit_synthetic_aborted(self) -> None:
        """Emit a synthetic ``workflow.run.aborted(reason="force")``.

        Only runs when the caller wired a session store — otherwise
        the emit path collapses (the cascade cancel already fired).
        Best-effort: any raise is logged and swallowed so the
        fallback can't crash the app.
        """
        if self._session_store is None:
            return
        try:
            from datetime import UTC, datetime
            from schemas.entities import Actor
            from schemas.events import Event, EventType
            from schemas.identifiers import EventId
            from uuid import uuid4

            # Only synthesise if the run hasn't already gone terminal —
            # a live executor may have landed its own terminal event
            # inside the 500ms window.
            registry = getattr(self._workflows_impl, "_workflow_registry", None)
            if registry is not None:
                record = registry.get(self._run_id)
                if record is not None and record.phase in (
                    "success", "error", "cancelled",
                ):
                    return

            payload: dict[str, Any] = {
                "run_id": str(self._run_id),
                "kind": "workflow.run.aborted",
                "reason": "force",
                "ts": datetime.now(tz=UTC).isoformat(),
            }
            event = Event(
                event_id=EventId(f"ev_{uuid4()}"),
                parent_event_id=None,
                thread_id=self._thread_id,  # type: ignore[arg-type]
                session_id=self._session_id,
                type=EventType.UI_EVENT,
                data={
                    "schema_version": 1,
                    "kind": "kolsch.workflow.progress",
                    "payload": payload,
                },
                actor=Actor(kind="system"),
                created_at=datetime.now(tz=UTC),
            )
            await self._session_store.append_event(self._thread_id, event)
        except Exception:  # noqa: BLE001 — synthetic emit best-effort
            _LOGGER.exception(
                "ForceKillConfirm: synthetic aborted emit failed for %s",
                self._run_id,
            )

    async def _unmount_safely(self) -> None:
        """Remove the widget from the DOM; safe under teardown."""
        try:
            await self.remove()
        except Exception:  # noqa: BLE001 — remove during teardown
            pass

    # ------------------------------------------------------------------
    # Key event fallback
    # ------------------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        """Fallback dispatch for headless tests without binding routing.

        Some Pilot fixtures process ``key`` messages before the app-
        level binding table matches; we short-circuit here so the ``y``
        / ``n`` semantics are honoured either way.
        """
        if event.key == "y":
            event.stop()
            await self.action_confirm()
        elif event.key in ("n", "escape"):
            event.stop()
            await self.action_cancel()


class _suppress:
    """Tiny contextlib.suppress-style local helper (avoid import churn)."""

    def __enter__(self) -> "_suppress":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return True


__all__ = ["ForceKillConfirm"]

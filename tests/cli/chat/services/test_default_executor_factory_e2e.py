"""bug-030 AC #4 — end-to-end regression for `_default_executor_factory`.

D-010 extension. bug-026 (commit `24ea95f`) shipped a fix for a
broken import inside ``WorkflowsImpl._default_executor_factory`` and
flagged the accompanying test blind spot:

    every workflow test injects a custom executor factory;
    ``_default_executor_factory`` is only reached in production.

A 4-angle diagnostic workflow (2026-07-09, session
``ses_c1b91692``) then found a SIBLING bug on line 1417:
``catalog.get(ref)`` calls a method that does not exist on
``AssetCatalog`` (only ``.lookup(ref, expected_type=...)`` is
public — see ``src/assets/catalog.py:72``). Every chat-side
``run_workflow`` invocation (generic OR virtual) raised
``AttributeError`` before ``RUN_STARTED`` — matching the observed
session shape: ``tool.result is_error=false`` at line 12,
``card.closed final_status="error"`` 9ms later at line 13, no
workflow events in between.

The three tests here exercise the REAL
``_default_executor_factory`` code path (no factory override),
against a REAL ``bootstrap_runtime`` + ``AssetCatalog`` seeded from
the starter bundle's ``kolsch/workflows/hello`` workflow:

- Test A (a): known ref → no ``AttributeError`` raises; the
  factory constructs the executor and ``RUN_STARTED`` fires.
- Test B (b): unknown ref → the factory's ``lookup`` returns
  ``None``, the run construction fails cleanly, the card closes
  with a non-null ``error_message`` containing the ref name (so
  the model can act on it).
- Test C (c): the hello workflow runs to completion —
  ``RUN_STARTED`` + ``RUN_COMPLETED`` land on the store's UI
  event log; the card closes with ``final_status="success"``.

Every test constructs ``WorkflowsImpl`` with ``executor_factory=None``
so ``_default_executor_factory`` is the production code path
exercised — this is the composition-gate discipline bug-026
flagged, extended to the factory itself.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest

from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.workflows_impl import WorkflowsImpl
from platform_runtime import bootstrap_runtime
from schemas.wire_kinds import (
    WIRE_CARD_CLOSED,
    WIRE_WORKFLOW_PROGRESS,
)
from store import SessionStore, SqliteBackend
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from schemas import Event


# ---------------------------------------------------------------------------
# Fixture — real workspace scaffolded via ``kolsch setup``.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def initialized_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize the shipped starter bundle via ``kolsch setup``.

    Mirrors bug-028's flagship fixture. The starter bundle ships
    ``kolsch/workflows/hello.yaml`` which is a two-node linear
    pipeline (``ready`` noop → ``greet`` echo) with a single optional
    input (``message``, defaulted). No authz-prompt rules apply so
    the headless run reaches ``RUN_COMPLETED``.
    """
    root = tmp_path_factory.mktemp("bug030_workspace")
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"setup failed: rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)
    return root


# ---------------------------------------------------------------------------
# Shared harness — real runtime + real CardsImpl + real WorkflowsImpl.
# ---------------------------------------------------------------------------


class _Harness:
    """Minimal service-triad harness driving the real factory."""

    def __init__(
        self,
        *,
        store: SessionStore,
        cards: CardsImpl,
        workflows: WorkflowsImpl,
        thread_id: object,
    ) -> None:
        self.store = store
        self.cards = cards
        self.workflows = workflows
        self.thread_id = thread_id

    async def read_ui_events(self) -> list[Event]:
        """Return every UI event on the thread's log in file order."""
        events: list[Event] = []
        async for ev in self.store.read_events(cast("Any", self.thread_id)):
            events.append(ev)
        return events


@pytest.fixture
async def harness(
    initialized_workspace: Path,
    tmp_path: Path,
) -> AsyncIterator[_Harness]:
    """Wire a real runtime + real CardsImpl + real WorkflowsImpl.

    The ``WorkflowsImpl`` is constructed with ``executor_factory=None``
    so ``_default_executor_factory`` is the code path exercised —
    this is the whole point of the D-010 extension.

    Uses the same workspace as ``initialized_workspace`` (bundles
    scaffolded via ``kolsch setup``) but writes its per-test session
    store into a fresh sqlite DB under ``tmp_path`` so tests don't
    stomp each other's event logs.
    """
    runtime = await bootstrap_runtime(root=initialized_workspace)

    # Per-test store + session so ``read_ui_events`` returns only
    # this test's events.
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_test", name="bug030")
    threads = await store.list_threads(session.session_id)
    thread_id = threads[0].thread_id

    cards = CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    run_store = WorkflowRunStore(tmp_path)
    workflows = WorkflowsImpl(
        runtime=runtime,
        cards=cards,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        run_store=run_store,
        # NB: executor_factory=None so `_default_executor_factory`
        # (the production path) is the code path under test. Removing
        # this argument or passing a mock would defeat the whole
        # point of this regression.
    )
    try:
        yield _Harness(
            store=store,
            cards=cards,
            workflows=workflows,
            thread_id=thread_id,
        )
    finally:
        await workflows.aclose()
        await cards.aclose()
        await backend.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_ui_event(
    harness: _Harness,
    kind: str,
    *,
    timeout_seconds: float = 8.0,
    poll_seconds: float = 0.05,
) -> dict[str, object] | None:
    """Poll the UI event log until a matching kind arrives (or timeout)."""
    max_iters = int(timeout_seconds / poll_seconds)
    for _ in range(max_iters):
        events = await harness.read_ui_events()
        for ev in events:
            data = cast("dict[str, object]", ev.data)
            if data.get("kind") == kind:
                payload = data.get("payload")
                return cast("dict[str, object]", payload)
        await asyncio.sleep(poll_seconds)
    return None


async def _wait_for_terminal_progress(
    harness: _Harness,
    *,
    terminal_kinds: tuple[str, ...] = (
        "workflow.run.completed",
        "workflow.run.failed",
        "workflow.run.timed_out",
        "workflow.run.aborted",
    ),
    timeout_seconds: float = 12.0,
    poll_seconds: float = 0.05,
) -> dict[str, object] | None:
    """Poll for a workflow-progress UI event whose executor kind is terminal."""
    max_iters = int(timeout_seconds / poll_seconds)
    for _ in range(max_iters):
        events = await harness.read_ui_events()
        for ev in events:
            data = cast("dict[str, object]", ev.data)
            if data.get("kind") != WIRE_WORKFLOW_PROGRESS:
                continue
            payload = cast("dict[str, object]", data.get("payload") or {})
            exec_kind = payload.get("kind")
            if isinstance(exec_kind, str) and exec_kind in terminal_kinds:
                return payload
        await asyncio.sleep(poll_seconds)
    return None


# ---------------------------------------------------------------------------
# Test A — AC #4 (a): known ref → no AttributeError; RUN_STARTED fires.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_factory_starts_hello_workflow(
    harness: _Harness,
) -> None:
    """`_default_executor_factory` on a known ref reaches RUN_STARTED.

    Pre-bug-030: this call raised ``AttributeError: 'AssetCatalog'
    object has no attribute 'get'`` inside the factory, the run
    failed at construction time before ``RUN_STARTED`` fired, and
    the card closed with a null error_message. The session repro
    (``ses_c1b91692``) was 9ms end-to-end.

    Post-bug-030: the factory uses ``catalog.lookup(ref,
    expected_type="workflow")``, the executor constructs cleanly,
    and the first UI event to land is
    ``workflow.progress`` with the executor's
    ``workflow.run.started`` payload.
    """
    card = await harness.cards.open(
        "Workflow · kolsch/workflows/hello",
        kind="workflow",
    )
    run_id = await harness.workflows.start(
        "kolsch/workflows/hello",
        {},
        card=card,
    )
    assert str(run_id).startswith("wrk_")

    # RUN_STARTED must land — pre-bug-030 this never happened
    # because the factory raised AttributeError first.
    started_payload = None
    for _ in range(200):  # 200 * 50ms = 10s cap
        events = await harness.read_ui_events()
        for ev in events:
            data = cast("dict[str, object]", ev.data)
            if data.get("kind") != WIRE_WORKFLOW_PROGRESS:
                continue
            payload = cast("dict[str, object]", data.get("payload") or {})
            if payload.get("kind") == "workflow.run.started":
                started_payload = payload
                break
        if started_payload is not None:
            break
        await asyncio.sleep(0.05)

    assert started_payload is not None, (
        "workflow.run.started never landed — did `_default_executor_"
        "factory` raise AttributeError before executor construction? "
        "That's the pre-bug-030 shape."
    )
    assert started_payload.get("run_id") == str(run_id)


# ---------------------------------------------------------------------------
# Test B — AC #4 (b): unknown ref → card closes with error_message set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_factory_unknown_ref_closes_card_with_message(
    harness: _Harness,
) -> None:
    """Unknown ref → card.closed carries a non-null error_message with the ref.

    Pre-bug-030: the factory called ``catalog.get(ref)`` which
    raised ``AttributeError`` (regardless of whether the ref
    existed) — every close was ``error_message=null``.

    Post-bug-030: ``catalog.lookup(...)`` returns ``None`` for
    unknown refs, the factory raises ``WrappedError`` with a
    legible message naming the ref, the construction-failure path
    at ``workflows_impl.py:819-834`` captures the exception on
    ``record.error_message``, and ``_finalize_terminal`` (site 2
    of Fix 2) pipes it into ``Card.close(error_message=...)``.
    """
    unknown_ref = "kolsch/workflows/does-not-exist-bug030"

    card = await harness.cards.open(
        f"Workflow · {unknown_ref}",
        kind="workflow",
    )
    _run_id = await harness.workflows.start(unknown_ref, {}, card=card)

    # Poll for the card.closed event.
    closed_payload = await _wait_for_ui_event(
        harness,
        WIRE_CARD_CLOSED,
        timeout_seconds=8.0,
    )
    assert closed_payload is not None, (
        f"card.closed never landed for unknown ref {unknown_ref!r}"
    )

    error_message = closed_payload.get("error_message")
    assert isinstance(error_message, str) and error_message, (
        f"card.closed carried error_message={error_message!r}; "
        f"pre-bug-030 this was always None. Fix 2 (three-site "
        f"error_message plumbing) is regressed."
    )
    assert unknown_ref in error_message, (
        f"expected {unknown_ref!r} in the error message so the "
        f"model can act on it; got: {error_message!r}"
    )
    assert closed_payload.get("final_status") == "error"


# ---------------------------------------------------------------------------
# Test C — AC #4 (c): trivial workflow runs to completion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_factory_hello_runs_to_completion(
    harness: _Harness,
) -> None:
    """`kolsch/workflows/hello` runs end-to-end through the default factory.

    The starter workflow is a two-node linear pipeline (``ready``
    noop → ``greet`` echo). No inputs are required (the ``message``
    bind uses ``| default(...)``), and no authz-prompt rules
    apply, so the run reaches ``RUN_COMPLETED``.

    Asserts:

    - ``workflow.run.started`` fires.
    - ``workflow.run.completed`` fires.
    - ``card.closed`` lands with ``final_status="success"`` and
      ``error_message=None`` (the success path leaves the
      diagnostic empty).
    """
    card = await harness.cards.open(
        "Workflow · kolsch/workflows/hello",
        kind="workflow",
    )
    run_id = await harness.workflows.start(
        "kolsch/workflows/hello",
        {},
        card=card,
    )

    terminal_payload = await _wait_for_terminal_progress(
        harness,
        timeout_seconds=15.0,
    )
    assert terminal_payload is not None, (
        "no terminal workflow.progress event fired — hello workflow "
        "never reached a terminal state"
    )
    assert terminal_payload.get("kind") == "workflow.run.completed", (
        f"expected workflow.run.completed, got {terminal_payload.get('kind')!r}"
    )
    assert terminal_payload.get("run_id") == str(run_id)

    closed_payload = await _wait_for_ui_event(
        harness,
        WIRE_CARD_CLOSED,
        timeout_seconds=8.0,
    )
    assert closed_payload is not None
    assert closed_payload.get("final_status") == "success"
    # Success path — no error message on the close event.
    assert closed_payload.get("error_message") is None

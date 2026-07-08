"""spec-017 Test 3 — `↳ from <parent-ref>` header hint (Pilot).

Three passes:

1. Mount workflow card body with `parent_ref="release-candidate"`.
   Header renders `↳ from release-candidate` on its own line above
   the progress bar.
2. Mount without a `parent_ref` → no `↳ from` line.
3. `set_parent_ref` after mount refreshes the header text
   (adapter path — the parent's `ref` may land after the
   `RUN_STARTED` event fires and the renderer already mounted).
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget

from cli.chat.widgets.card_body.workflow import (
    WorkflowProgressHeader,
    WorkflowRenderer,
)


@pytest.mark.asyncio
async def test_parent_ref_renders_arrow_line(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """`parent_ref='release-candidate'` → `↳ from release-candidate` above bar."""
    renderer = WorkflowRenderer(parent_ref="release-candidate")
    app = fake_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(renderer._header.render())
        assert "↳ from release-candidate" in text, (
            f"expected ↳ from line in header text; got {text!r}"
        )
        # Progress bar must still land on a separate line below the hint.
        assert "\n" in text, (
            "expected multi-line header when parent_ref set"
        )


@pytest.mark.asyncio
async def test_no_parent_ref_no_arrow_line(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """Default (`parent_ref=None`) → single-line progress bar only."""
    renderer = WorkflowRenderer()
    app = fake_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(renderer._header.render())
        assert "↳" not in text, (
            f"expected no ↳ line when parent_ref is None; got {text!r}"
        )
        # Single-line progress bar shape.
        assert "\n" not in text.rstrip(), (
            "expected single-line header when parent_ref is None"
        )


@pytest.mark.asyncio
async def test_set_parent_ref_after_mount_refreshes_header(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """`set_parent_ref` after mount replaces the hint text.

    Simulates the adapter path where the renderer mounts BEFORE the
    parent's record's ``ref`` is known (nested detection lands via
    the first `kolsch.workflow.progress` fan-out). `set_parent_ref`
    must idempotently update the header.
    """
    renderer = WorkflowRenderer()
    app = fake_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        pre = str(renderer._header.render())
        assert "↳" not in pre

        renderer.set_parent_ref("outer.ref")
        await pilot.pause()
        post = str(renderer._header.render())
        assert "↳ from outer.ref" in post, (
            f"expected refreshed hint; got {post!r}"
        )


def test_progress_header_parent_ref_compute_text() -> None:
    """Direct compute-text test — no Textual mount required.

    Complements the pilot-mounted assertions with a headless smoke
    on `WorkflowProgressHeader._compute_text` so a header-format
    regression surfaces without spinning up an `App.run_test`.
    """
    header = WorkflowProgressHeader(parent_ref="A.ref")
    text = header._compute_text()
    assert text.startswith("↳ from A.ref\n"), (
        f"expected hint line first; got {text!r}"
    )
    # And with no parent_ref: single-line.
    header_none = WorkflowProgressHeader()
    text_none = header_none._compute_text()
    assert "↳" not in text_none
    assert "\n" not in text_none


@pytest.mark.asyncio
async def test_nested_workflow_progress_fanout_sets_parent_ref() -> None:
    """End-to-end pass — spec-017 Test 3 pass #3.

    Simulates the adapter path: two `_WorkflowRegistryStub` records
    (a parent A + a nested child B whose ``parent_run_id`` points at
    A), a mounted `WorkflowRenderer` bound to B's card. Firing a
    `kolsch.workflow.progress` fan-out for B's run_id must set the
    renderer's `_parent_ref` to A's ref via the adapter's
    `_refresh_workflow_parent_ref` helper.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from cli.chat.event_adapter import EventAdapter

    parent_run_id = f"wrk_{uuid4()}"
    child_run_id = f"wrk_{uuid4()}"

    class _StubRecord:
        def __init__(
            self,
            *,
            ref: str,
            parent_run_id: str | None = None,
        ) -> None:
            self.ref = ref
            self.parent_run_id = parent_run_id

    class _StubWorkflowsImpl:
        def __init__(self) -> None:
            self._workflow_registry = {
                parent_run_id: _StubRecord(ref="A.ref"),
                child_run_id: _StubRecord(
                    ref="B.ref", parent_run_id=parent_run_id,
                ),
            }

    class _FakeApp:
        def query_one(self, *_a: object, **_kw: object) -> object:
            raise LookupError("no #transcript")

    renderer = WorkflowRenderer()
    # Pretend the renderer is already anchored to child's run so the
    # fan-out doesn't need to `RUN_STARTED` first.
    renderer._run_id = child_run_id

    adapter = EventAdapter(
        _FakeApp(),  # type: ignore[arg-type]
        workflows_impl=_StubWorkflowsImpl(),  # type: ignore[arg-type]
    )

    # Directly exercise the fan-out helper — a bare
    # `_handle_workflow_progress` would iterate card widgets, which
    # requires mounting a CardWidget in an App. The helper's contract
    # is the tested seam.
    adapter._refresh_workflow_parent_ref(renderer, child_run_id)

    assert renderer._parent_ref == "A.ref", (
        f"expected renderer._parent_ref='A.ref'; got "
        f"{renderer._parent_ref!r}"
    )
    # And the header's computed text carries the hint.
    header_text = renderer._header._compute_text()
    assert "↳ from A.ref" in header_text

"""Smoke tests for the three example bundle tools.

Each test boots a real ``ChatApp`` under Pilot, invokes the tool via
the ``_invoke_tool_via_pilot`` helper (which routes through the same
spec-008 finalizer the engine uses), and asserts:

- the tool returns the expected shape,
- the transcript mounts the expected ``CardWidget`` /
  ``ApprovalBlock``, and
- widget state matches the tool's terminal transition (card ends
  ``success``, progress bar hits 100%, approval receives the
  keypress).

A fourth test proves each tool raises ``ToolNotAvailable`` when the
services it declares are missing from ``ToolContext`` (headless
mode: workflow runner, one-shot CLI, or any test that constructs
``ToolContext`` without a ``ChatServiceRegistry``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from tests.bundles.kolsch.tools.conftest import (
    _headless_ctx,
    _invoke_tool_via_pilot,
)
from tools.services.exceptions import ToolNotAvailable

# --------------------------------------------------------------------
# Test 1 — hello_card
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_card_smoke(
    chat_app,
    hello_card_tool: Callable[..., Awaitable[object]],
) -> None:
    """hello_card streams three chunks + closes as success."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        result = await _invoke_tool_via_pilot(pilot, hello_card_tool, {})
        # bug-020: the runtime bus subscription in `ChatApp.on_mount`
        # forwards each `CardsImpl` publish to `EventAdapter.handle`
        # in the SAME session — no manual store drain. Yield to the
        # loop so the bus dispatch and adapter mount complete.
        for _ in range(20):
            await pilot.pause()
            if pilot.app.query("CardWidget"):
                break

        assert result == {"message": "hello card demo complete"}

        cards = pilot.app.query("CardWidget")
        assert len(cards) == 1
        widget = cards.first()
        assert widget.state == "success"
        # Streamed content lives on the widget's `.content` buffer
        # (spec-006 stub) and on the markdown body renderer's own
        # buffer (spec-007). The `.content` attribute is the stable
        # Protocol seam.
        assert "Hello!" in widget.content


# --------------------------------------------------------------------
# Test 2 — download_with_progress
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_with_progress_smoke(
    chat_app,
    download_with_progress_tool: Callable[..., Awaitable[object]],
) -> None:
    """download_with_progress advances the bar to total_kb / total_kb."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        result = await _invoke_tool_via_pilot(
            pilot,
            download_with_progress_tool,
            {"total_kb": 30, "step_kb": 10},
        )
        # bug-020: live bus dispatch; poll until the CardWidget mounts.
        for _ in range(20):
            await pilot.pause()
            if pilot.app.query("CardWidget"):
                break

        assert result == {"downloaded_kb": 30}

        cards = pilot.app.query("CardWidget")
        assert len(cards) == 1
        widget = cards.first()
        # Terminal state — the async-with auto-closed success.
        assert widget.state == "success"

        bar = widget.query_one("ProgressBar")
        # ProgressBar stores progress/total as float internally.
        assert int(bar.progress) == 30
        assert int(bar.total) == 30


# --------------------------------------------------------------------
# Test 3 — confirm_dangerous_action
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_dangerous_action_smoke(
    chat_app,
    confirm_dangerous_action_tool: Callable[..., Awaitable[object]],
) -> None:
    """confirm_dangerous_action mounts an ApprovalBlock; ``y`` resolves it."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()

        # Fire the tool in a task so the pilot loop can advance while
        # the tool body awaits the prompt future.
        task: asyncio.Task[object] = asyncio.create_task(
            _invoke_tool_via_pilot(
                pilot,
                confirm_dangerous_action_tool,
                {"action_description": "delete file"},
            ),
        )

        # bug-020: `PromptsImpl` doesn't (yet) publish to the runtime
        # bus, but `Cards.notice` / card mounts on the same bus DO.
        # For this smoke, we still poll for the ApprovalBlock — the
        # WIRE_PROMPT_REQUESTED event is appended to the store; the
        # adapter's bookmark path picks it up on the next handle
        # (no drain needed for card mounts). Simply pump the pilot.
        approval = None
        for _ in range(50):  # ~1s worst case
            await pilot.pause()
            found = pilot.app.query("ApprovalBlock")
            if len(found) == 1:
                approval = found.first()
                break
        assert approval is not None, "ApprovalBlock did not mount"

        # The InlineApprovalCallback wraps ApprovalBlock resolutions
        # into WIRE_PROMPT_ANSWERED events (spec-004). The direct
        # Prompts.choice path (which confirm_dangerous_action uses)
        # does NOT go through InlineApprovalCallback — its UI-to-impl
        # bridge is roadmap work. We resolve the pending PromptsImpl
        # future here to model what the callback wrapper does in
        # production; the smoke test's job is to prove the tool body
        # opens + awaits + resolves, not to exercise the missing UI
        # bridge.
        prompts_impl = pilot.app._service_registry.prompts
        pending_prompt_id = prompts_impl._pending_prompt_id
        assert pending_prompt_id is not None
        prompts_impl.resolve_pending(str(pending_prompt_id), "yes")

        result = await asyncio.wait_for(task, timeout=2.0)
        assert result == {"user_answered": "yes", "confirmed": True}


# --------------------------------------------------------------------
# Test 4 — headless mode: ToolNotAvailable for all three
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_not_available_headless(
    hello_card_tool: Callable[..., Awaitable[object]],
    download_with_progress_tool: Callable[..., Awaitable[object]],
    confirm_dangerous_action_tool: Callable[..., Awaitable[object]],
) -> None:
    """All three tools raise ``ToolNotAvailable`` when services are absent."""
    hello_defn = hello_card_tool._tool_definition  # type: ignore[attr-defined]
    dl_defn = download_with_progress_tool._tool_definition  # type: ignore[attr-defined]
    confirm_defn = confirm_dangerous_action_tool._tool_definition  # type: ignore[attr-defined]

    # `hello_card` requires cards; `ctx.cards is None` -> ToolNotAvailable.
    with pytest.raises(ToolNotAvailable):
        await hello_card_tool(hello_defn.args_schema(), _headless_ctx())

    # `download_with_progress` also requires cards.
    with pytest.raises(ToolNotAvailable):
        await download_with_progress_tool(
            dl_defn.args_schema(total_kb=10, step_kb=5),
            _headless_ctx(),
        )

    # `confirm_dangerous_action` requires prompts; `ctx.prompts is None`
    # -> ToolNotAvailable.
    with pytest.raises(ToolNotAvailable):
        await confirm_dangerous_action_tool(
            confirm_defn.args_schema(action_description="test"),
            _headless_ctx(),
        )

"""feat-127 Test 3 — `/new` awaits the butler turn before repointing ids.

Three slices of AC #2/#9:

1. Ordering — with an in-flight turn, `/new` fires the cancel token and
   does NOT repoint the session until the turn's unwind clears
   `active_cancel` (the `_run_exchange` finally's contract). Verified by
   recording which session id is live at the exact moment the simulated
   turn unwinds.
2. Escalation — a turn that ignores the cooperative token within the
   bound gets its `_run_exchange` worker hard-cancelled (the writer is
   genuinely quiesced, not walked away from) before the switch.
3. Backstop — with nothing to hard-cancel and a token that never clears,
   `/new` still proceeds, bounded — never hangs.
"""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.app import ChatApp
from tests.component.cli.chat.test_new_session_switch import _bootstrap_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_new_repoints_only_after_turn_unwinds(tmp_path: Path) -> None:
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_ses = app.session.session_id
        seen_at_unwind: list[Any] = []

        class _Tok:
            """Cancel token whose fire unwinds the 'turn' 150ms later.

            Mimics `_run_exchange`: the token wakes the awaiting turn,
            whose `finally` nulls `active_cancel`. The session id live
            at that instant is recorded — the repoint must not have
            happened yet.
            """

            def cancel(self) -> None:
                async def _unwind() -> None:
                    await asyncio.sleep(0.15)
                    seen_at_unwind.append(app.session.session_id)
                    app.active_cancel = None

                asyncio.get_running_loop().create_task(_unwind())

        app.active_cancel = _Tok()

        await app._open_new_session()
        await pilot.pause()

        assert app.session.session_id != old_ses
        # The turn unwound while the OLD session was still live — the
        # repoint strictly followed the turn's completion.
        assert seen_at_unwind == [old_ses]
        assert app.active_cancel is None


@pytest.mark.asyncio
async def test_new_hard_cancels_wedged_turn_worker(tmp_path: Path) -> None:
    """A turn that ignores the cooperative token is hard-cancelled.

    Cooperative cancellation is checked only at engine attempt
    boundaries, so a turn wedged in a long tool call outlives the poll
    bound. `/new` must then cancel the `_run_exchange` worker itself —
    quiescing the writer for real — instead of repointing ids while the
    worker still pumps old-turn events into the adapter.
    """
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_ses = app.session.session_id

        # Simulates `_run_exchange` wedged in a >bound tool call: the
        # cooperative token fires but is never observed; a hard cancel
        # lands at the await and the finally nulls `active_cancel` (the
        # real worker's contract).
        app.active_cancel = SimpleNamespace(cancel=lambda: None)

        async def _wedged_turn() -> None:
            try:
                await asyncio.sleep(60)
            finally:
                app.active_cancel = None

        worker = app.run_worker(_wedged_turn(), name="_run_exchange")
        await pilot.pause()
        app._retire_butler_turn = functools.partial(  # type: ignore[method-assign]
            ChatApp._retire_butler_turn, app, timeout=0.3
        )

        start = asyncio.get_running_loop().time()
        await app._open_new_session()
        await pilot.pause()
        elapsed = asyncio.get_running_loop().time() - start

        assert app.session.session_id != old_ses  # proceeded
        assert worker.is_cancelled  # the worker was actually killed…
        assert app.active_cancel is None  # …and its writer quiesced
        assert elapsed < 3.0, "escalation must stay bounded"


@pytest.mark.asyncio
async def test_new_proceeds_when_turn_wont_die(tmp_path: Path) -> None:
    """Last-resort backstop: even with nothing to hard-cancel, no hang.

    `active_cancel` is set but no `_run_exchange` worker exists (nothing
    for the escalation to kill) and the token never clears — both polls
    expire and `/new` still proceeds, bounded.
    """
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_ses = app.session.session_id

        app.active_cancel = SimpleNamespace(cancel=lambda: None)
        app._retire_butler_turn = functools.partial(  # type: ignore[method-assign]
            ChatApp._retire_butler_turn, app, timeout=0.3
        )

        start = asyncio.get_running_loop().time()
        await app._open_new_session()
        await pilot.pause()
        elapsed = asyncio.get_running_loop().time() - start

        assert app.session.session_id != old_ses  # proceeded anyway
        assert elapsed < 3.0, "force path must not hang /new"

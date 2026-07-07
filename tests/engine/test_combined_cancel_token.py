"""Tests for `CombinedCancelToken` — cancel-on-any composition.

Verifies the four semantic invariants for spec-010:

1. Firing any inner token flips the combined token to cancelled
   without disturbing the other inner tokens.
2. Firing the combined token's own local flag does NOT propagate to
   the inner tokens.
3. `wait_cancelled` resolves on the first fire across
   {local, inner_1, inner_2, ...} and does not leak ephemeral tasks.
4. Zero-args construction raises `ValueError`; a single inner arg
   instantiates and the result is a `CancelToken` subclass.
"""

from __future__ import annotations

import asyncio

import pytest

from engine.context import CancelToken, CombinedCancelToken


@pytest.mark.asyncio
async def test_fire_inner_a() -> None:
    """Firing inner `a` flips the combined token; `b` stays untouched."""
    a = CancelToken()
    b = CancelToken()
    combined = CombinedCancelToken(a, b)

    assert combined.is_cancelled() is False

    a.cancel()

    assert combined.is_cancelled() is True
    assert b.is_cancelled() is False


@pytest.mark.asyncio
async def test_fire_inner_b() -> None:
    """Firing inner `b` flips the combined token; `a` stays untouched."""
    a = CancelToken()
    b = CancelToken()
    combined = CombinedCancelToken(a, b)

    assert combined.is_cancelled() is False

    b.cancel()

    assert combined.is_cancelled() is True
    assert a.is_cancelled() is False


@pytest.mark.asyncio
async def test_fire_combined_local() -> None:
    """Firing the combined token's local flag does NOT propagate."""
    a = CancelToken()
    b = CancelToken()
    combined = CombinedCancelToken(a, b)

    assert combined.is_cancelled() is False

    combined.cancel()

    assert combined.is_cancelled() is True
    assert a.is_cancelled() is False
    assert b.is_cancelled() is False


@pytest.mark.asyncio
async def test_wait_first_fire() -> None:
    """`wait_cancelled` resolves on the first inner fire; no task leak."""
    a = CancelToken()
    b = CancelToken()
    combined = CombinedCancelToken(a, b)

    wait_task = asyncio.create_task(combined.wait_cancelled())

    # Give the wait_cancelled coroutine a chance to spawn its inner
    # waits and settle into asyncio.wait().
    await asyncio.sleep(0.05)
    assert wait_task.done() is False

    b.cancel()

    # Wait for wait_task with a 500ms timeout.
    await asyncio.wait_for(wait_task, timeout=0.5)
    assert wait_task.done() is True

    # No leaked tasks after resolution: only this test's own task should
    # remain in the loop's task set (the current running coroutine).
    current = asyncio.current_task()
    remaining = {t for t in asyncio.all_tasks() if t is not current}
    # `wait_task` has completed and been awaited via wait_for; it may
    # still appear briefly in all_tasks() as done. Filter done tasks —
    # what we care about is that no inner-wait tasks are still pending.
    pending = {t for t in remaining if not t.done()}
    assert pending == set(), f"Leaked pending tasks: {pending!r}"


@pytest.mark.asyncio
async def test_construction() -> None:
    """Zero-args raises `ValueError`; single inner instantiates cleanly."""
    with pytest.raises(ValueError, match="at least one inner"):
        CombinedCancelToken()

    a = CancelToken()
    combined = CombinedCancelToken(a)
    assert isinstance(combined, CancelToken)
    assert combined.is_cancelled() is False

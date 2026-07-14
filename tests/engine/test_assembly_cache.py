"""feat-059 — `AssemblyCache` and engine-wiring tests.

Covers the caller-side memoisation seam in isolation (Part 1) plus
the engine wiring and Fermi smoke (Part 2).

Tests below correspond to the numbered acceptance tests in
`process/feat-059-assembly-projection-cache.md`:

- Test 1 → `test_second_call_is_cache_hit`
- Test 2 → `test_appending_event_forces_miss`
- Test 3 → `test_respect_compaction_isolated_cache_slots`
- Test 4 → `test_tool_schemas_hash_stable_across_dict_order`
- Test 5 → `test_per_thread_cache_evicts_oldest_over_max`
- Test 6 → `test_version_bump_invalidates_cache`
- Test 7 → `test_engine_helper_reuses_projection_on_second_call`
- Test 8 → `test_no_direct_assemble_context_calls_in_engine`
- Test 9 → `test_two_engines_do_not_share_cache`
- Test 10 → `test_cache_warm_is_orders_of_magnitude_faster`
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engine import MinimalFramework
from engine.assembly import ASSEMBLY_VERSION, assemble_context
from engine.assembly_cache import (
    AssemblyCache,
    _Key,
    _MAX_PER_THREAD,
    hash_tool_schemas,
)
from schemas import Actor, Event, EventId, EventType, ThreadId
from tests.fixtures.engine_builder import build_engine
from tests.fixtures.mock_llm import MockLLM, TextResponse


_TID = ThreadId(f"th_{uuid4()}")
_SID = f"ses_{uuid4()}"


def _mk(type_: EventType, data: dict[str, Any]) -> Event:
    """Build a synthetic Event for cache-behaviour tests."""
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=_TID,
        session_id=_SID,
        type=type_,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _key_for(
    events: list[Event],
    *,
    tool_schemas: list[dict[str, Any]] | None = None,
    respect_compaction: bool = True,
    assembly_version: str = ASSEMBLY_VERSION,
    thread_id: ThreadId = _TID,
) -> _Key:
    """Construct a `_Key` for the given event path and options."""
    return _Key(
        thread_id=thread_id,
        leaf_event_id=events[-1].event_id,
        tool_schemas_hash=hash_tool_schemas(tool_schemas),
        respect_compaction=respect_compaction,
        assembly_version=assembly_version,
    )


# ---------------------------------------------------------------------------
# Test 1 — cache-hit path
# ---------------------------------------------------------------------------


def test_second_call_is_cache_hit() -> None:
    """Two `get`s after one `put` — the second is a hit by identity."""
    cache = AssemblyCache()
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": "hello"}}),
    ]
    ctx = assemble_context(events)
    key = _key_for(events)

    # First lookup is a miss (empty cache).
    assert cache.get(key) is None
    assert cache.misses == 1
    assert cache.hits == 0

    cache.put(key, ctx)

    # Second lookup is a hit and returns the same instance we stored.
    hit = cache.get(key)
    assert hit is ctx
    assert cache.hits == 1
    assert cache.misses == 1


# ---------------------------------------------------------------------------
# Test 2 — leaf change invalidates
# ---------------------------------------------------------------------------


def test_appending_event_forces_miss() -> None:
    """Appending an event changes the leaf → new key → miss."""
    cache = AssemblyCache()
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": "hello"}}),
    ]
    ctx = assemble_context(events)
    key = _key_for(events)
    cache.put(key, ctx)

    # Same key still hits.
    assert cache.get(key) is ctx

    # Appending a new event changes the leaf event id, so the new key
    # differs and must miss.
    events.append(_mk(EventType.USER_MESSAGE, {"content": {"text": "again"}}))
    new_key = _key_for(events)
    assert new_key != key
    assert cache.get(new_key) is None


# ---------------------------------------------------------------------------
# Test 3 — `respect_compaction` isolates entries
# ---------------------------------------------------------------------------


def test_respect_compaction_isolated_cache_slots() -> None:
    """`respect_compaction=True` and `False` occupy independent slots."""
    cache = AssemblyCache()
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": "hello"}}),
    ]
    ctx_true = assemble_context(events, respect_compaction=True)
    ctx_false = assemble_context(events, respect_compaction=False)

    key_true = _key_for(events, respect_compaction=True)
    key_false = _key_for(events, respect_compaction=False)
    assert key_true != key_false

    # Two misses on cold cache.
    assert cache.get(key_true) is None
    assert cache.get(key_false) is None
    cache.put(key_true, ctx_true)
    cache.put(key_false, ctx_false)

    # Two hits, each slot returns its own value.
    assert cache.get(key_true) is ctx_true
    assert cache.get(key_false) is ctx_false
    assert cache.hits == 2
    assert cache.misses == 2


# ---------------------------------------------------------------------------
# Test 4 — tool-schemas hash is stable across dict iteration order
# ---------------------------------------------------------------------------


def test_tool_schemas_hash_stable_across_dict_order() -> None:
    """Equivalent tool-schema dicts with different key order hash equal."""
    schemas_a = [{"a": 1, "b": 2, "nested": {"x": 10, "y": 20}}]
    schemas_b = [{"nested": {"y": 20, "x": 10}, "b": 2, "a": 1}]
    assert hash_tool_schemas(schemas_a) == hash_tool_schemas(schemas_b)

    # None and [] must produce the same hash — "no tools" is one state.
    assert hash_tool_schemas(None) == hash_tool_schemas([])

    # Different content still hashes differently.
    assert hash_tool_schemas([{"a": 1}]) != hash_tool_schemas([{"a": 2}])


# ---------------------------------------------------------------------------
# Test 5 — per-thread LRU eviction at `_MAX_PER_THREAD`
# ---------------------------------------------------------------------------


def test_per_thread_cache_evicts_oldest_over_max() -> None:
    """Inserting `_MAX_PER_THREAD + 1` keys evicts the oldest."""
    cache = AssemblyCache()
    events_base = [_mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}})]
    ctx = assemble_context(events_base)

    # Build 17 distinct keys against the same thread. Vary the leaf
    # event id so each is unique; other key fields are constant.
    keys: list[_Key] = []
    for _ in range(_MAX_PER_THREAD + 1):
        keys.append(
            _Key(
                thread_id=_TID,
                leaf_event_id=EventId(f"ev_{uuid4()}"),
                tool_schemas_hash=hash_tool_schemas(None),
                respect_compaction=True,
                assembly_version=ASSEMBLY_VERSION,
            ),
        )

    for key in keys:
        cache.put(key, ctx)

    # The oldest key (index 0) must have been evicted.
    assert cache.get(keys[0]) is None
    # All remaining keys are still present.
    for key in keys[1:]:
        assert cache.get(key) is ctx


# ---------------------------------------------------------------------------
# Test 6 — assembly-version bump invalidates
# ---------------------------------------------------------------------------


def test_version_bump_invalidates_cache() -> None:
    """A different `assembly_version` yields a different key → miss."""
    cache = AssemblyCache()
    events = [_mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}})]
    ctx = assemble_context(events)
    key_v2 = _key_for(events, assembly_version="2")
    cache.put(key_v2, ctx)
    assert cache.get(key_v2) is ctx

    key_v3 = _key_for(events, assembly_version="3")
    assert key_v2 != key_v3
    # A bumped ASSEMBLY_VERSION means the old cached entry is not
    # visible under the new key — the caller re-projects.
    assert cache.get(key_v3) is None


# ---------------------------------------------------------------------------
# Test 9 — two caches don't share state
# ---------------------------------------------------------------------------


def test_two_engines_do_not_share_cache() -> None:
    """Two `AssemblyCache` instances are isolated by construction."""
    cache_a = AssemblyCache()
    cache_b = AssemblyCache()
    events = [_mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}})]
    ctx = assemble_context(events)
    key = _key_for(events)

    cache_a.put(key, ctx)
    assert cache_a.get(key) is ctx
    # Cache B has never seen the key — miss.
    assert cache_b.get(key) is None
    assert cache_b.misses == 1
    assert cache_b.hits == 0


# ---------------------------------------------------------------------------
# Test 7 — engine helper reuses the projection on a second same-turn call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_helper_reuses_projection_on_second_call(
    tmp_path: Path,
) -> None:
    """Two calls to ``_assemble_context_cached`` on the same events hit.

    Narrower shape (documented in the Part-2 handoff): rather than
    driving ``AgentEngine.manual_compact`` end-to-end — which needs a
    live compaction provider, a ResolvedAgentConfig, and a session
    with real leaf state — build an engine via the standard test
    fixture and call the cached helper directly. Same seam, same
    guarantee: the second call must be a hit and produce byte-
    identical output.
    """
    mock = MockLLM(script=[TextResponse(text="ok")])
    engine, _store, _am = await build_engine(
        tmp_path,
        mock,
        MinimalFramework(),
    )

    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": "hello"}}),
    ]
    tool_schemas: list[dict[str, Any]] = []

    hits_before = engine._assembly_cache.hits
    misses_before = engine._assembly_cache.misses

    first = engine._assemble_context_cached(
        events,
        tool_schemas=tool_schemas,
        respect_compaction=True,
    )
    second = engine._assemble_context_cached(
        events,
        tool_schemas=tool_schemas,
        respect_compaction=True,
    )

    # Byte-identical output (frozen model — identity check is stronger
    # than equality, but the spec asks for byte-identical
    # ``model_dump`` too).
    assert first is second
    assert first.model_dump() == second.model_dump()

    # First was a miss, second was a hit.
    assert engine._assembly_cache.misses == misses_before + 1
    assert engine._assembly_cache.hits == hits_before + 1


# ---------------------------------------------------------------------------
# Test 8 — grep-based lint: no direct assemble_context calls in engine.py
# ---------------------------------------------------------------------------


def test_no_direct_assemble_context_calls_in_engine() -> None:
    """``src/engine/engine.py`` may call ``assemble_context`` at most once.

    Part 2 routes every projection call through
    ``AgentEngine._assemble_context_cached``, which is the sole
    remaining direct caller of the pure function. A regression here
    means a new call site slipped in without going through the cache
    and would silently reintroduce the O(n²) behaviour on that path.
    """
    engine_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "engine"
        / "engine.py"
    )
    source = engine_path.read_text(encoding="utf-8")
    count = source.count("assemble_context(")
    assert count == 1, (
        f"expected exactly one `assemble_context(` call in engine.py "
        f"(the helper's own delegation to the pure function); "
        f"found {count}"
    )


# ---------------------------------------------------------------------------
# Test 10 — Fermi smoke: warm cache is orders of magnitude faster
# ---------------------------------------------------------------------------


def _synthetic_path(length: int) -> list[Event]:
    """Build a synthetic ``length``-event path with a realistic mix.

    Cycles through USER_MESSAGE, AGENT_MESSAGE, TOOL_CALLED,
    TOOL_RESULT so the projection walks every branch of its type
    switch. The exact payloads are minimally viable — the goal is
    walk cost, not projection correctness.
    """
    events: list[Event] = []
    for i in range(length):
        cycle = i % 4
        if cycle == 0:
            events.append(_mk(
                EventType.USER_MESSAGE,
                {"content": {"text": f"user msg {i}"}},
            ))
        elif cycle == 1:
            events.append(_mk(
                EventType.AGENT_MESSAGE,
                {"content": {"text": f"agent msg {i}"}},
            ))
        elif cycle == 2:
            events.append(_mk(
                EventType.TOOL_CALLED,
                {
                    "tool_call_id": f"tc_{i}",
                    "tool_name": "noop",
                    "arguments": {"n": i},
                },
            ))
        else:
            events.append(_mk(
                EventType.TOOL_RESULT,
                {
                    "tool_call_id": f"tc_{i - 1}",
                    "tool_name": "noop",
                    "result": {"ok": True, "n": i},
                },
            ))
    return events


def test_cache_warm_is_orders_of_magnitude_faster() -> None:
    """Warm cache lookups are >=8x faster than a cold projection walk.

    Times one cold call to the pure ``assemble_context`` on a
    500-event path, then nine warm calls that go through
    ``AssemblyCache.get`` on the same key. The cache-warm path is a
    dict lookup + LRU refresh; the cold path is an O(n) event walk +
    Pydantic model construction. On a 500-event path the ratio should
    be at least three orders of magnitude in practice; 8x is a
    generous smoke gate that still catches "cache didn't fire" bugs.
    """
    events = _synthetic_path(500)
    cache = AssemblyCache()
    key = _Key(
        thread_id=events[-1].thread_id,
        leaf_event_id=events[-1].event_id,
        tool_schemas_hash=hash_tool_schemas(None),
        respect_compaction=True,
        assembly_version=ASSEMBLY_VERSION,
    )

    # Cold call: walk the full 500-event path once.
    cold_start = time.perf_counter()
    cold_ctx = assemble_context(events, respect_compaction=True)
    cold_elapsed = time.perf_counter() - cold_start
    cache.put(key, cold_ctx)

    # Nine warm calls — pure cache lookups.
    warm_samples: list[float] = []
    for _ in range(9):
        warm_start = time.perf_counter()
        hit = cache.get(key)
        warm_samples.append(time.perf_counter() - warm_start)
        assert hit is cold_ctx

    warm_avg = sum(warm_samples) / len(warm_samples)
    # Guard against clock granularity making warm_avg == 0 on very
    # fast systems: floor at 1 ns so the ratio is well-defined.
    warm_avg_floor = max(warm_avg, 1e-9)
    ratio = cold_elapsed / warm_avg_floor
    assert ratio >= 8.0, (
        f"expected warm avg to be >=8x faster than cold; "
        f"cold={cold_elapsed:.6f}s, warm_avg={warm_avg:.9f}s, "
        f"ratio={ratio:.2f}x"
    )

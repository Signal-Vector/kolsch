"""feat-059 Part 1 — `AssemblyCache` unit tests.

Covers the caller-side memoisation seam in isolation: the cache
class + tool-schemas hasher. Engine wiring lands in Part 2 and its
integration tests (7, 8, 10 in the spec) live alongside that work.

Tests below correspond to the numbered acceptance tests in
`process/feat-059-assembly-projection-cache.md`:

- Test 1 → `test_second_call_is_cache_hit`
- Test 2 → `test_appending_event_forces_miss`
- Test 3 → `test_respect_compaction_isolated_cache_slots`
- Test 4 → `test_tool_schemas_hash_stable_across_dict_order`
- Test 5 → `test_per_thread_cache_evicts_oldest_over_max`
- Test 6 → `test_version_bump_invalidates_cache`
- Test 9 → `test_two_engines_do_not_share_cache`
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from engine.assembly import ASSEMBLY_VERSION, assemble_context
from engine.assembly_cache import (
    AssemblyCache,
    _Key,
    _MAX_PER_THREAD,
    hash_tool_schemas,
)
from schemas import Actor, Event, EventId, EventType, ThreadId


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

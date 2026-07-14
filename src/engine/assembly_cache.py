"""feat-059 — In-process cache for `assemble_context` output.

The engine calls
[`assemble_context`][engine.assembly.assemble_context] up to three
times per exchange, and each call walks the full chronological event
path. That's O(n) per call, O(n²) across a session. This module is
the caller-side memoisation seam: a per-`AgentEngine` LRU keyed on
`(thread_id, leaf_event_id, tool_schemas_hash, respect_compaction,
assembly_version)` so a new event appended to the log naturally
invalidates the cached entry via the `leaf_event_id` change.

`AssembledContext` is `frozen=True`
([`assembly.py:47`][engine.assembly.AssembledContext]), so cached
instances are safe to hand out by reference — callers cannot mutate
them.

Threading contract: `AgentEngine` serialises every
`assemble_context` call inside `_lock_for_thread(str(thread_id))`, so
per-thread `OrderedDict` reads/writes are already serial. The
`_outer_lock` here only guards the outer `_per_thread` dict when a
new inner dict is created for a previously-unseen thread — otherwise
concurrent first-writes from two threads could race to install the
inner dict and lose state. That path is uncontended so the cost is
negligible.

This module intentionally does NOT modify
[`assemble_context`][engine.assembly.assemble_context] or wire itself
into [`AgentEngine`][engine.engine.AgentEngine] — Part 2 of feat-059
adds the engine-side helper method and replaces the direct call
sites at `engine.py:549, 1775, 2040`.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from schemas import EventId, ThreadId

if TYPE_CHECKING:
    from engine.assembly import AssembledContext


# Rationale: only distinct `(tool_schemas_hash, respect_compaction)`
# combinations share a thread's slot; 16 is well above any observed
# variant count and bounds per-thread memory to a small constant.
_MAX_PER_THREAD = 16


@dataclass(frozen=True)
class _Key:
    """Cache key for one memoised `assemble_context` call.

    Frozen so it hashes; five fields cover every input that can
    change the projection output. A new event appended to the thread
    naturally changes `leaf_event_id`, so log writes invalidate the
    cache for free.
    """

    thread_id: ThreadId
    leaf_event_id: EventId
    tool_schemas_hash: str
    respect_compaction: bool
    assembly_version: str


class AssemblyCache:
    """Per-`AgentEngine` LRU cache for `assemble_context` output.

    Backed by `dict[ThreadId, OrderedDict[_Key, AssembledContext]]`
    so eviction runs per thread (`_MAX_PER_THREAD`) and dropping a
    whole thread's slot is O(1). `hits` and `misses` are ints so
    tests can assert cache behaviour without a public metrics surface.
    """

    def __init__(self) -> None:
        self._per_thread: dict[
            ThreadId, OrderedDict[_Key, AssembledContext]
        ] = {}
        self.hits: int = 0
        self.misses: int = 0
        self._outer_lock = threading.Lock()

    def get(self, key: _Key) -> AssembledContext | None:
        """Return the cached `AssembledContext` for `key`, or `None`.

        On a hit, promotes the entry to most-recently-used via
        `OrderedDict.move_to_end` and increments `hits`. On a miss,
        increments `misses` — no state mutation.
        """
        inner = self._per_thread.get(key.thread_id)
        if inner is None:
            self.misses += 1
            return None
        value = inner.get(key)
        if value is None:
            self.misses += 1
            return None
        # LRU refresh: move the hit entry to the "newest" end so it
        # survives the next overflow eviction.
        inner.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: _Key, value: AssembledContext) -> None:
        """Insert or overwrite the cache entry for `key`.

        Evicts the oldest entry if the per-thread slot exceeds
        `_MAX_PER_THREAD`. The outer lock is held only when creating
        a new inner `OrderedDict` for a previously-unseen thread —
        inner mutations rely on the per-thread engine lock the caller
        already holds.
        """
        inner = self._per_thread.get(key.thread_id)
        if inner is None:
            with self._outer_lock:
                inner = self._per_thread.get(key.thread_id)
                if inner is None:
                    inner = OrderedDict()
                    self._per_thread[key.thread_id] = inner
        inner[key] = value
        inner.move_to_end(key)
        while len(inner) > _MAX_PER_THREAD:
            inner.popitem(last=False)

    def invalidate_thread(self, thread_id: ThreadId) -> None:
        """Drop the cache slot for `thread_id`; safe for unknown ids.

        Used by `aclose` / test hooks; the standard invalidation path
        is the `leaf_event_id` key change on any new log write.
        """
        with self._outer_lock:
            self._per_thread.pop(thread_id, None)


def hash_tool_schemas(schemas: list[dict[str, Any]] | None) -> str:
    """Stable content hash for a tool-schemas list.

    `json.dumps(sort_keys=True, separators=(",", ":"))` serialises
    equivalent dicts in different key order to the same bytes, so the
    resulting sha256 hexdigest is dict-order-independent. `None` is
    treated as the empty list — the two inputs mean "no tools" and
    must map to the same key.
    """
    payload = schemas or []
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

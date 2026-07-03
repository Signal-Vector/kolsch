"""Component tests for src/providers/memory — TC-MEM-01..09.

Spec: docs/explanation/memory-provider-contract.md +
Module API Reference §5. The Memory provider is session-scoped; v1
ships the ``recent_buffer`` variant.

Memory NEVER appends to the session log — the Agent Engine does
(MEM I1). The tests below stage memory.* events by calling
``store.append_event`` directly to simulate the engine's role; the
provider reads them back via ``store.read_events`` (Store I8 — sole
mediator of log access).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from errors import MemoryItemNotFound, MemoryWriteDisabled
from kernel import ValidationResult
from providers.memory import (
    MemoryItem,
    RecentBufferMemoryContext,
    RecentBufferMemoryProvider,
    RetrievalQuery,
    validate_recent_buffer_config,
)
from schemas import (
    Actor,
    AgentId,
    Event,
    EventId,
    EventType,
    MemoryItemId,
    SessionId,
    ThreadId,
)
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures — real SQLite store + RecentBufferMemoryProvider
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    (tmp_path / ".platform").mkdir()
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    yield SessionStore(root=tmp_path, backend=backend)
    await backend.close()


@pytest.fixture
def memory(store: SessionStore) -> RecentBufferMemoryProvider:
    return RecentBufferMemoryProvider(store=store)


async def _bind(
    memory_provider: RecentBufferMemoryProvider,
    store: SessionStore,
    *,
    write_tag: str | None = "notes/research",
    read_tags: list[str] | None = None,
    agent_id: str = "ag_test",
) -> tuple[SessionId, ThreadId, RecentBufferMemoryContext]:
    session = await store.create_session(agent_id)
    thread = (await store.list_threads(session.session_id))[0]
    config: dict[str, Any] = {}
    if write_tag is not None:
        config["write_tag"] = write_tag
    config["read_tags"] = read_tags or []
    ctx = await memory_provider.bind_session(
        session_id=session.session_id,
        agent_id=AgentId(agent_id),
        config=config,
    )
    return session.session_id, thread.thread_id, ctx


def _now() -> datetime:
    return datetime.now(UTC)


async def _emit_memory_written(
    store: SessionStore,
    *,
    session_id: SessionId,
    thread_id: ThreadId,
    item: MemoryItem,
    scope: str = "session",
    variant: str = "recent_buffer",
) -> None:
    """Simulate the Agent Engine emitting a memory.written event."""
    await store.append_event(
        thread_id,
        Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=thread_id,
            session_id=session_id,
            type=EventType.MEMORY_WRITTEN,
            data={
                "item_id": item.item_id,
                "agent_id": item.agent_id,
                "scope": scope,
                "content": item.content,
                "tags": list(item.tags),
                "metadata": dict(item.metadata),
                "variant": variant,
            },
            actor=Actor(kind="system"),
            created_at=item.created_at,
        ),
    )


async def _emit_memory_deleted(
    store: SessionStore,
    *,
    session_id: SessionId,
    thread_id: ThreadId,
    item_id: str,
) -> None:
    await store.append_event(
        thread_id,
        Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=thread_id,
            session_id=session_id,
            type=EventType.MEMORY_DELETED,
            data={"item_id": item_id},
            actor=Actor(kind="system"),
            created_at=_now(),
        ),
    )


def _make_item(
    *,
    agent_id: str = "ag_test",
    tag: str = "notes/research",
    text: str = "hello",
    created_at: datetime | None = None,
) -> MemoryItem:
    return MemoryItem(
        item_id=MemoryItemId(f"mem_{uuid4()}"),
        agent_id=AgentId(agent_id),
        tags=[tag],
        metadata={},
        content={"text": text},
        created_at=created_at or _now(),
    )


# ===========================================================================
# TC-MEM-01 — cache rebuild equals pre-restart state
# ===========================================================================


async def test_cache_rebuild_equals_pre_restart_state(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    """Catalog literal form: snapshot retrieval before restart, rebind
    a fresh context (simulating restart), snapshot again, assert the
    two snapshots are identical — including order. Catches
    nondeterministic-ordering bugs the "expected-set" form misses."""
    session_id, thread_id, _ctx = await _bind(
        memory,
        store,
        read_tags=["notes/*"],
    )

    item_a = _make_item(text="alpha", tag="notes/a")
    item_b = _make_item(text="beta", tag="notes/b")
    item_c = _make_item(text="gamma", tag="notes/c")

    await _emit_memory_written(
        store, session_id=session_id, thread_id=thread_id, item=item_a
    )
    await _emit_memory_written(
        store, session_id=session_id, thread_id=thread_id, item=item_b
    )
    await _emit_memory_written(
        store, session_id=session_id, thread_id=thread_id, item=item_c
    )
    # Delete item_b. After replay the cache should hold {a, c}.
    await _emit_memory_deleted(
        store,
        session_id=session_id,
        thread_id=thread_id,
        item_id=item_b.item_id,
    )

    # Snapshot 1 — bind a context, list items, capture ordered ids.
    first_ctx = await memory.bind_session(
        session_id=session_id,
        agent_id=AgentId("ag_test"),
        config={"write_tag": "notes/research", "read_tags": ["notes/*"]},
    )
    snapshot_first = [i.item_id for i in await first_ctx.list_items()]

    # Snapshot 2 — fully discard the previous context (simulating a
    # restart), bind a new one, list items again.
    del first_ctx
    second_ctx = await memory.bind_session(
        session_id=session_id,
        agent_id=AgentId("ag_test"),
        config={"write_tag": "notes/research", "read_tags": ["notes/*"]},
    )
    snapshot_second = [i.item_id for i in await second_ctx.list_items()]

    # Ordered equality — MEM I9 (deterministic replay).
    assert snapshot_first == snapshot_second
    assert set(snapshot_first) == {item_a.item_id, item_c.item_id}


# ===========================================================================
# TC-MEM-02 — write applies the configured tag
# ===========================================================================


async def test_write_applies_configured_tag(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    _, _, ctx = await _bind(memory, store, write_tag="notes/research")
    item = await ctx.write(content={"text": "fact"})
    assert item.tags == ["notes/research"]


# ===========================================================================
# TC-MEM-03 — user cannot override write tag
# ===========================================================================


async def test_user_cannot_override_write_tag(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    _, _, ctx = await _bind(memory, store, write_tag="notes/research")
    item = await ctx.write(
        content={"text": "fact"},
        # Attempted override via metadata. The remaining "ok" key
        # exists so we can confirm legitimate metadata survives
        # while the smuggled `tags` key is stripped — pin both
        # halves of the policy.
        metadata={"tags": ["smuggled-tag"], "ok": "kept"},
    )
    # The configured write_tag wins regardless of what the caller put
    # in metadata.
    assert item.tags == ["notes/research"]
    assert "smuggled-tag" not in item.tags
    # The smuggled tag is also stripped from metadata — if it were
    # left there, a future regression that "fixes item.tags but
    # leaves metadata.tags" would slip through.
    assert "tags" not in item.metadata
    # Legitimate metadata keys are preserved.
    assert item.metadata == {"ok": "kept"}


# ===========================================================================
# TC-MEM-04 — write without write_tag is disabled
# ===========================================================================


async def test_write_without_write_tag_raises_disabled(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    _, _, ctx = await _bind(
        memory,
        store,
        write_tag=None,
        read_tags=["notes/*"],
    )
    with pytest.raises(MemoryWriteDisabled):
        await ctx.write(content={"text": "x"})


# ===========================================================================
# TC-MEM-05 — wildcard write_tag rejected at Layer 3 validation
# ===========================================================================


def test_layer3_rejects_wildcard_in_write_tag() -> None:
    result = validate_recent_buffer_config(
        {"write_tag": "research/*", "read_tags": ["notes/*"]}
    )
    assert result.ok is False
    codes = [e.code for e in result.errors]
    assert "wildcard_in_write_tag" in codes


def test_layer3_accepts_minimal_valid_config() -> None:
    result = validate_recent_buffer_config({"write_tag": "notes/research"})
    assert result.ok is True, result.errors


def test_layer3_rejects_subsystem_with_neither_write_nor_read() -> None:
    """Spec §5.1 — at least one of write_tag/read_tags required;
    otherwise the subsystem should be omitted entirely."""
    result = validate_recent_buffer_config({})
    assert result.ok is False
    codes = [e.code for e in result.errors]
    assert "memory_disabled_but_present" in codes


def test_layer3_rejects_negative_recent_turns_and_max_tokens() -> None:
    """The validator docstring promises "non-negative ints if
    supplied"; pin the rule so a future regression doesn't drift."""
    result = validate_recent_buffer_config(
        {"write_tag": "x", "recent_turns": -5, "max_tokens": -1000}
    )
    assert result.ok is False
    codes = [e.code for e in result.errors]
    paths = [e.path for e in result.errors]
    assert codes.count("negative_value") == 2
    assert "/recent_turns" in paths
    assert "/max_tokens" in paths


def test_layer3_rejects_non_int_recent_turns() -> None:
    result = validate_recent_buffer_config({"write_tag": "x", "recent_turns": "ten"})
    assert result.ok is False
    assert any(e.code == "not_an_int" for e in result.errors)


def test_layer3_satisfies_kernel_config_validator_signature() -> None:
    """The validator is what the kernel calls during Layer 3 dispatch
    (spec §5.1). It MUST be a Callable[[dict], ValidationResult]."""
    out = validate_recent_buffer_config({"write_tag": "x"})
    assert isinstance(out, ValidationResult)


# ===========================================================================
# TC-MEM-06 — read_tags wildcard matches hierarchically
# ===========================================================================


async def test_read_tags_wildcard_matches_hierarchical_children(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    session_id, thread_id, _ = await _bind(
        memory,
        store,
        write_tag="notes/research",
        read_tags=["research/*"],
    )

    # Spec §5.1 — `research/*` MUST match BOTH `research/a` AND
    # `research/a/b` (deeply nested). The shorthand "one level" is a
    # common misreading; pin the deeper-depth case explicitly.
    a = _make_item(tag="research/a", text="paper-a")
    b = _make_item(tag="research/b", text="paper-b")
    deep = _make_item(tag="research/a/sub/deep", text="paper-deep")
    other = _make_item(tag="other/c", text="paper-c")
    for item in (a, b, deep, other):
        await _emit_memory_written(
            store, session_id=session_id, thread_id=thread_id, item=item
        )

    rebuilt = await memory.bind_session(
        session_id=session_id,
        agent_id=AgentId("ag_test"),
        config={"write_tag": "notes/research", "read_tags": ["research/*"]},
    )
    result = await rebuilt.retrieve(RetrievalQuery())
    returned_text = {
        item.content["text"] for item in result.items if item.source == "memory_item"
    }
    assert returned_text == {"paper-a", "paper-b", "paper-deep"}


# ===========================================================================
# TC-MEM-07 — session isolation
# ===========================================================================


async def test_session_isolation(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    session_a_id, thread_a_id, _ = await _bind(memory, store)
    session_b_id, thread_b_id, _ = await _bind(memory, store)

    a_item = _make_item(text="A-only", tag="notes/research")
    b_item = _make_item(text="B-only", tag="notes/research")
    await _emit_memory_written(
        store, session_id=session_a_id, thread_id=thread_a_id, item=a_item
    )
    await _emit_memory_written(
        store, session_id=session_b_id, thread_id=thread_b_id, item=b_item
    )

    ctx_a = await memory.bind_session(
        session_id=session_a_id,
        agent_id=AgentId("ag_test"),
        config={"write_tag": "notes/research", "read_tags": ["notes/*"]},
    )
    # Isolation MUST hold across BOTH read paths — `list_items`
    # (introspection) and `retrieve` (context assembly). MEM I7.
    listed_a = await ctx_a.list_items()
    ids_listed = {item.item_id for item in listed_a}
    assert a_item.item_id in ids_listed
    assert b_item.item_id not in ids_listed

    retrieved_a = await ctx_a.retrieve(RetrievalQuery())
    ids_retrieved = {
        item.source_id for item in retrieved_a.items if item.source == "memory_item"
    }
    assert a_item.item_id in ids_retrieved
    assert b_item.item_id not in ids_retrieved


# ===========================================================================
# TC-MEM-08 — delete unknown item raises
# ===========================================================================


async def test_delete_unknown_item_raises(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    _, _, ctx = await _bind(memory, store)
    with pytest.raises(MemoryItemNotFound):
        await ctx.delete(f"mem_{uuid4()}")


# ===========================================================================
# TC-MEM-09 — Memory does NOT emit events (code inspection)
# ===========================================================================


def test_memory_provider_does_not_call_store_append_event() -> None:
    """MEM I1 — the Memory provider MUST NOT append to the session
    log directly. Verified by source inspection: no reference to
    `append_event` appears anywhere in the provider's source.
    """
    from providers import memory as memory_module

    source = inspect.getsource(memory_module)
    assert "append_event" not in source, (
        "Memory provider MUST NOT call store.append_event (MEM I1). "
        "Engine emits memory.* events; provider returns results only."
    )


# ===========================================================================
# Extra spec invariants
# ===========================================================================


async def test_write_returns_item_without_emitting_event(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    """Spec §4.2 — context.write returns the MemoryItem to the caller;
    the engine (NOT the provider) emits the memory.written event.
    Verified by reading back the log: only the synthetic
    session.opened is present after a provider-side write."""
    _session_id, thread_id, ctx = await _bind(memory, store)
    await ctx.write(content={"text": "fact"})

    events = [e async for e in store.read_events(thread_id)]
    types = [e.type for e in events]
    assert EventType.MEMORY_WRITTEN not in types, (
        "MEM I1: provider must NOT append memory.written itself"
    )


async def test_retrieve_returns_actual_content(
    store: SessionStore,
    memory: RecentBufferMemoryProvider,
) -> None:
    """MEM I2 — retrieve returns actual content for the
    Materialization Rule; no deferred references."""
    session_id, thread_id, _ = await _bind(memory, store, read_tags=["notes/*"])
    item = _make_item(text="the answer is 42")
    await _emit_memory_written(
        store, session_id=session_id, thread_id=thread_id, item=item
    )
    rebuilt = await memory.bind_session(
        session_id=session_id,
        agent_id=AgentId("ag_test"),
        config={"write_tag": "notes/research", "read_tags": ["notes/*"]},
    )
    result = await rebuilt.retrieve(RetrievalQuery())
    items = [i for i in result.items if i.source == "memory_item"]
    assert len(items) == 1
    assert items[0].content == {"text": "the answer is 42"}

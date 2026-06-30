"""Memory provider — session-scoped recall of tagged items plus recent events.

The v1 `recent_buffer` variant is a session-scoped, in-process cache
fed by replaying `memory.written` / `memory.deleted` events from the
session log; retrieval combines recent conversational events with the
explicitly-written tagged items. For the full contract — write-tag
binding, deterministic replay, session isolation — see the
[memory provider contract](../../docs/explanation/memory-provider-contract.md).

The provider never writes to the session log itself: the engine emits
`memory.written` / `memory.deleted` / `memory.retrieved` events, and
this provider just returns results. Log access is mediated entirely
by [`SessionStore.read_events`][store.SessionStore.read_events]; the
provider never opens JSONL files directly.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from errors import (
    ConfigInvalid,
    MemoryItemNotFound,
    MemorySessionUnbound,
    MemoryWriteDisabled,
)
from kernel import ValidationError, ValidationResult
from schemas import AgentId, EventType, MemoryItemId, SessionId

if TYPE_CHECKING:
    from collections.abc import Callable

    from store import SessionStore

_log = logging.getLogger("providers.memory")


# ===========================================================================
# Public data types
# ===========================================================================


class MemoryItem(BaseModel):
    """One explicitly-written memory record.

    `content` is opaque to the provider — typed as a dict for Pydantic
    ergonomics; the agent owning the write knows the shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: MemoryItemId
    agent_id: AgentId
    tags: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    content: dict[str, Any]
    created_at: datetime


class RetrievalQuery(BaseModel):
    """Per-call retrieval parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tokens: int | None = None
    tags_any: list[str] | None = None
    tags_all: list[str] | None = None
    metadata: dict[str, Any] | None = None
    semantic_query: str | None = None
    time_range: tuple[datetime, datetime] | None = None


class RetrievedItem(BaseModel):
    """One item in a [`RetrievalResult`][providers.memory.RetrievalResult]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: dict[str, Any]
    source: Literal["log_event", "memory_item"]
    source_id: str
    relevance: float | None = None
    tags: list[str] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    """The bundle returned to the engine for context assembly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[RetrievedItem]
    total_tokens: int
    source_summary: dict[str, Any] = Field(default_factory=dict)


class FilterExpression(BaseModel):
    """Query shape consumed by
    [`MemoryContext.list_items`][providers.memory.MemoryContext]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tags_any: list[str] | None = None
    tags_all: list[str] | None = None
    metadata: dict[str, Any] | None = None
    agent_id: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


class CompactionResult(BaseModel):
    """Output of a memory-compaction pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str
    original_token_count: int
    summary_token_count: int
    cost: float | None = None


# ===========================================================================
# Layer 3 validator (kernel ConfigValidator signature)
# ===========================================================================


def validate_recent_buffer_config(
    body: dict[str, Any],
) -> ValidationResult:
    """Validate a `recent_buffer` config body at Layer 3.

    Three rules:

    - `write_tag` (if present) must not contain `*`.
    - At least one of `write_tag` or `read_tags` must be present;
      otherwise the subsystem should be omitted entirely.
    - `recent_turns` / `max_tokens` must be non-negative ints when
      supplied.
    """
    errors: list[ValidationError] = []
    write_tag = body.get("write_tag")
    read_tags = body.get("read_tags", []) or []

    if write_tag is not None and "*" in str(write_tag):
        errors.append(
            ValidationError(
                layer=3,
                source="memory.recent_buffer",
                path="/write_tag",
                code="wildcard_in_write_tag",
                message=(
                    f"write_tag {write_tag!r} contains '*'; "
                    "wildcards are not permitted on write_tag"
                ),
            )
        )

    if write_tag is None and not read_tags:
        errors.append(
            ValidationError(
                layer=3,
                source="memory.recent_buffer",
                path="/",
                code="memory_disabled_but_present",
                message=(
                    "recent_buffer requires at least one of write_tag "
                    "or read_tags; otherwise omit the subsystem entirely"
                ),
            )
        )

    for field in ("recent_turns", "max_tokens"):
        if field not in body:
            continue
        value = body[field]
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(
                ValidationError(
                    layer=3,
                    source="memory.recent_buffer",
                    path=f"/{field}",
                    code="not_an_int",
                    message=(f"{field} must be an integer; got {type(value).__name__}"),
                )
            )
            continue
        if value < 0:
            errors.append(
                ValidationError(
                    layer=3,
                    source="memory.recent_buffer",
                    path=f"/{field}",
                    code="negative_value",
                    message=(f"{field} must be non-negative; got {value}"),
                )
            )

    if not isinstance(read_tags, list):
        errors.append(
            ValidationError(
                layer=3,
                source="memory.recent_buffer",
                path="/read_tags",
                code="not_a_list",
                message=(f"read_tags must be a list; got {type(read_tags).__name__}"),
            )
        )

    return ValidationResult(ok=not errors, errors=errors)


# ===========================================================================
# Tag matching — exact + hierarchical prefix wildcard
# ===========================================================================


def _tag_matches_pattern(tag: str, pattern: str) -> bool:
    """Return whether `tag` matches `pattern` under the recent-buffer rules.

    A prefix wildcard `"research/*"` matches children
    (`research/a`, `research/a/b`) but not `research` exactly.
    Patterns without `*` are matched exactly.
    """
    if pattern.endswith("/*"):
        prefix = pattern[:-1]  # keep the trailing slash
        return tag.startswith(prefix)
    return tag == pattern


def _item_matches_any_tag(item_tags: list[str], patterns: list[str]) -> bool:
    if not patterns:
        return False
    return any(_tag_matches_pattern(t, p) for t in item_tags for p in patterns)


# ===========================================================================
# MemoryContext + RecentBufferMemoryProvider
# ===========================================================================


def _item_from_event(event_data: dict[str, Any], created_at: datetime) -> MemoryItem:
    """Reconstruct a [`MemoryItem`][providers.memory.MemoryItem] from a
    `memory.written` event.

    The envelope's `created_at` is authoritative — the engine stamps
    it when emitting, and we trust it for cache ordering. The data
    payload carries `item_id` / `agent_id` / `tags` / etc.
    """
    return MemoryItem(
        item_id=MemoryItemId(event_data["item_id"]),
        agent_id=AgentId(event_data["agent_id"]),
        tags=list(event_data.get("tags", [])),
        metadata=dict(event_data.get("metadata", {})),
        content=dict(event_data.get("content", {})),
        created_at=created_at,
    )


class MemoryContext(Protocol):
    """The session-bound handle the engine receives at bind time."""

    session_id: SessionId
    agent_id: AgentId
    write_tag: str | None
    read_tags: list[str]

    async def write(
        self,
        content: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> MemoryItem: ...

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult: ...

    async def list_items(
        self,
        filters: FilterExpression | None = None,
    ) -> list[MemoryItem]: ...

    async def delete(self, item_id: str) -> None: ...


class RecentBufferMemoryContext:
    """The v1 `recent_buffer` `MemoryContext`.

    Holds the per-session cache. Writes update the in-memory cache and
    return the constructed item to the caller (the Agent Engine) — the
    engine emits the `memory.written` event so the provider stays out
    of the log. The same flow applies to deletes.
    """

    def __init__(  # noqa: PLR0913 — bound config is intentionally explicit
        self,
        *,
        session_id: SessionId,
        agent_id: AgentId,
        write_tag: str | None,
        read_tags: list[str],
        recent_turns: int,
        max_tokens: int,
        cache: dict[MemoryItemId, MemoryItem],
    ) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.write_tag = write_tag
        self.read_tags = list(read_tags)
        self._recent_turns = recent_turns
        self._max_tokens = max_tokens
        self._cache: dict[MemoryItemId, MemoryItem] = dict(cache)

    async def write(
        self,
        content: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> MemoryItem:
        """Construct and cache a new memory item under the configured tag.

        Raises:
            MemoryWriteDisabled: No `write_tag` was configured at
                bind time.
        """
        if self.write_tag is None:
            raise MemoryWriteDisabled(
                agent_id=str(self.agent_id),
                reason="no write_tag configured",
            )
        # Strip any caller-supplied tag from metadata; the configured
        # write_tag wins. We never accept a runtime override.
        clean_metadata = dict(metadata or {})
        clean_metadata.pop("tags", None)
        from uuid import uuid4  # noqa: PLC0415 — minor IO

        item = MemoryItem(
            item_id=MemoryItemId(f"mem_{uuid4()}"),
            agent_id=self.agent_id,
            tags=[self.write_tag],
            metadata=clean_metadata,
            content=dict(content),
            created_at=datetime.now(tz=_utc()),
        )
        self._cache[item.item_id] = item
        return item

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Return tagged memory items matching `query`, newest first."""
        patterns = query.tags_any if query.tags_any is not None else self.read_tags
        # Collect memory items matching the tag filter, newest first.
        matching = [
            item
            for item in self._cache.values()
            if _item_matches_any_tag(item.tags, patterns)
        ]
        # Deterministic ordering: created_at desc, item_id tie-break so
        # identical timestamps don't reorder across runs.
        matching.sort(
            key=lambda item: (item.created_at, item.item_id),
            reverse=True,
        )

        # The engine assembles recent conversational events itself;
        # this provider just returns the memory items. Blending events
        # with items lands when the engine wires this through the
        # Assembly Procedure.
        items = [
            RetrievedItem(
                content=item.content,
                source="memory_item",
                source_id=item.item_id,
                relevance=None,
                tags=list(item.tags),
            )
            for item in matching
        ]
        # Token accounting is a placeholder until the engine ships its
        # real tokenizer; treat each content as one token for
        # diagnostics-only purposes.
        return RetrievalResult(
            items=items,
            total_tokens=len(items),
            source_summary={
                "memory_item_count": len(items),
                "log_event_count": 0,
            },
        )

    async def list_items(
        self,
        filters: FilterExpression | None = None,
    ) -> list[MemoryItem]:
        """Return cached items filtered by `filters`, newest first.

        Named `list_items` rather than `list` to avoid shadowing the
        builtin; functionally the contract's `list` method.
        """
        result = list(self._cache.values())
        if filters is None:
            return self._sort(result)
        if filters.tags_any:
            result = [
                item
                for item in result
                if _item_matches_any_tag(item.tags, filters.tags_any)
            ]
        if filters.tags_all:
            patterns = filters.tags_all
            result = [
                item
                for item in result
                if all(
                    any(_tag_matches_pattern(t, p) for t in item.tags) for p in patterns
                )
            ]
        if filters.metadata:
            for key, value in filters.metadata.items():
                result = [item for item in result if item.metadata.get(key) == value]
        if filters.agent_id:
            result = [item for item in result if item.agent_id == filters.agent_id]
        if filters.created_after:
            result = [
                item for item in result if item.created_at > filters.created_after
            ]
        if filters.created_before:
            result = [
                item for item in result if item.created_at < filters.created_before
            ]
        return self._sort(result)

    async def delete(self, item_id: str) -> None:
        """Drop `item_id` from the cache.

        Raises:
            MemoryItemNotFound: No item with that id is cached.
        """
        cast_id = MemoryItemId(item_id)
        if cast_id not in self._cache:
            raise MemoryItemNotFound(
                item_id=item_id,
                session_id=str(self.session_id),
            )
        del self._cache[cast_id]

    @staticmethod
    def _sort(items: list[MemoryItem]) -> list[MemoryItem]:
        return sorted(
            items,
            key=lambda i: (i.created_at, i.item_id),
            reverse=True,
        )


def _utc() -> Any:  # noqa: ANN401 — tiny helper indirection for tests
    from datetime import UTC  # noqa: PLC0415

    return UTC


class RecentBufferMemoryProvider:
    """The v1 Memory provider.

    Constructed with a [`SessionStore`][store.SessionStore] so it can
    read events back for cache rebuild — the store is the sole
    mediator of log access; the provider never opens JSONL files
    itself.
    """

    def __init__(self, *, store: SessionStore) -> None:
        self._store = store

    async def bind_session(
        self,
        *,
        session_id: SessionId,
        agent_id: AgentId,
        config: dict[str, Any],
    ) -> RecentBufferMemoryContext:
        """Build the session-scoped `MemoryContext` by replaying the log.

        Walks the thread's `memory.written` / `memory.deleted` events
        in order so the rebuilt cache matches a live cache exactly.

        Raises:
            ConfigInvalid: `config` fails the recent-buffer validator.
            MemorySessionUnbound: The session has no threads on disk.
        """
        # Safety re-check; main validation ran at Layer 3.
        revalidation = validate_recent_buffer_config(config)
        if not revalidation.ok:
            payload = [e.model_dump(mode="json") for e in revalidation.errors]
            raise ConfigInvalid(
                validation_errors=payload,
                where="providers.memory.bind_session",
            )

        threads = await self._store.list_threads(session_id)
        if not threads:
            raise MemorySessionUnbound(session_id=str(session_id))
        thread = threads[0]

        cache: dict[MemoryItemId, MemoryItem] = {}
        async for event in self._store.read_events(thread.thread_id):
            if event.type == EventType.MEMORY_WRITTEN:
                try:
                    item = _item_from_event(event.data, event.created_at)
                except (KeyError, ValueError) as exc:
                    _log.warning(
                        "skipping malformed memory.written event %s: %s",
                        event.event_id,
                        exc,
                    )
                    continue
                cache[item.item_id] = item
            elif event.type == EventType.MEMORY_DELETED:
                deleted_id = event.data.get("item_id")
                if deleted_id is not None:
                    cache.pop(MemoryItemId(str(deleted_id)), None)

        return RecentBufferMemoryContext(
            session_id=session_id,
            agent_id=agent_id,
            write_tag=config.get("write_tag"),
            read_tags=list(config.get("read_tags", []) or []),
            recent_turns=int(config.get("recent_turns", 10)),
            max_tokens=int(config.get("max_tokens", 4000)),
            cache=cache,
        )


# ===========================================================================
# Registry seam
# ===========================================================================


_REGISTRY: dict[str, Callable[..., RecentBufferMemoryProvider]] = {}


def register_memory_variant(
    name: str,
    factory: Callable[..., RecentBufferMemoryProvider],
) -> None:
    """Register a variant factory under `name`."""
    _REGISTRY[name] = factory


def make_memory_provider(
    *,
    store: SessionStore,
    config: dict[str, Any],
) -> RecentBufferMemoryProvider:
    """Dispatch by `config['variant']` — symmetric with the sibling factories.

    Raises:
        ConfigInvalid: `variant` is unset or names no registered factory.
    """
    variant = config.get("variant")
    factory = _REGISTRY.get(variant or "")
    if factory is None:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 2,
                    "source": "providers.memory",
                    "path": "/memory/variant",
                    "code": "unknown_variant",
                    "message": (
                        f"unknown memory variant {variant!r}; "
                        f"registered: {sorted(_REGISTRY)}"
                    ),
                }
            ],
            where="providers.memory.make_memory_provider",
        )
    return factory(store=store)


register_memory_variant("recent_buffer", RecentBufferMemoryProvider)

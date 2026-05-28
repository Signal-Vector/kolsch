"""@tool decorator + canonical types — Module API Reference §9 + spec §2.

A ``@tool``-decorated function carries a :class:`ToolDefinition`
attached to its ``_tool_definition`` attribute. The Tools subsystem
discovers tools by reading that attribute (Phase 1 simplification of
spec §5's full discovery — declared-from-file loading is its own
follow-up).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from schemas import AgentId, SessionId, ThreadId

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Spec §2.2 — name must match this pattern.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolContext(BaseModel):
    """The runtime context passed to every tool function — §9.2.

    Five fields, all read-only. Tools cannot write the event log,
    inspect the principal, or perform authz checks (all done by the
    dispatch flow before the tool runs).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: AgentId
    session_id: SessionId
    thread_id: ThreadId
    principal: str
    secrets: dict[str, str]


class ToolResult(BaseModel):
    """The return shape of :meth:`ToolsSubsystem.invoke_tool` — §9.2.

    ``result`` is whatever the tool returned, captured verbatim
    (T-I5). ``metadata`` carries optional latency/cost diagnostics
    the dispatch flow attaches.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    result: Any
    metadata: dict[str, Any] = {}


class ToolInfo(BaseModel):
    """Metadata-only view of a registered tool — §9.2.

    Returned by :meth:`ToolsSubsystem.list_registered_tools` so
    callers can introspect without holding function references.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    args_schema: dict[str, Any]


@dataclass
class ToolDefinition:
    """Everything the dispatch needs to know about a tool — spec §2.2."""

    name: str
    description: str
    args_schema: type[BaseModel]
    func: Callable[..., Awaitable[Any]]
    secrets_needed: list[str] = field(default_factory=list)
    action: str | None = None
    timeout_seconds: float | None = None

    @property
    def action_name(self) -> str:
        """The action string used for authz checks. Defaults to
        ``tool.<name>`` per spec T-I1; overridable via the decorator
        for the rare case of a tool wanting a different namespace."""
        return self.action or f"tool.{self.name}"

    def to_info(self) -> ToolInfo:
        return ToolInfo(
            name=self.name,
            description=self.description,
            args_schema=self.args_schema.model_json_schema(),
        )


def tool(
    *,
    name: str,
    description: str,
    args_schema: type[BaseModel],
    secrets_needed: list[str] | None = None,
    action: str | None = None,
    timeout_seconds: float | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """The ``@tool`` decorator — spec §2.

    Attaches a :class:`ToolDefinition` to the function's
    ``_tool_definition`` attribute. The function is returned
    unchanged so the underlying coroutine remains directly callable.

    Phase 1 simplifications:
    - ``args_schema`` is REQUIRED. Spec §2.2 calls it optional; the
      Pydantic-validate-first dispatch step would have to skip itself
      otherwise. Tools that take no args declare an empty ``BaseModel``.
    - ``returns_schema`` from spec §2.2 is omitted in v1 (no
      Pydantic-validate-return step in dispatch).
    """
    if not _NAME_PATTERN.fullmatch(name):
        msg = f"tool name {name!r} must match ^[a-z][a-z0-9_]*$ per spec §2.2"
        raise ValueError(msg)

    def decorate(
        fn: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        defn = ToolDefinition(
            name=name,
            description=description,
            args_schema=args_schema,
            func=fn,
            secrets_needed=list(secrets_needed or []),
            action=action,
            timeout_seconds=timeout_seconds,
        )
        fn._tool_definition = defn  # type: ignore[attr-defined]
        return fn

    return decorate


def get_tool_definition(
    fn: Callable[..., Awaitable[Any]],
) -> ToolDefinition:
    """Return the :class:`ToolDefinition` attached to a decorated function.

    Raises :class:`AttributeError` if ``fn`` is not ``@tool``-decorated;
    callers typically filter by ``hasattr(fn, "_tool_definition")``
    during discovery.
    """
    defn = fn._tool_definition  # type: ignore[attr-defined]
    if not isinstance(defn, ToolDefinition):
        msg = f"{fn!r} is not a @tool-decorated function"
        raise TypeError(msg)
    return defn


def register_tool(
    fn: Callable[..., Awaitable[Any]],
    *,
    secrets_needed: list[str] | None = None,
) -> None:
    """Update an existing ``@tool`` function's metadata in place.

    Used by tests to add ``secrets_needed`` AFTER decoration when the
    test wants to keep the original ``@tool(...)`` call concise.
    Mutates the attached :class:`ToolDefinition` directly.
    """
    defn = get_tool_definition(fn)
    if secrets_needed is not None:
        defn.secrets_needed = list(secrets_needed)

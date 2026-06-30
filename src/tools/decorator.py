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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from files import PathPolicy
from schemas import AgentId, SessionId, ThreadId

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Spec §2.2 — name must match this pattern.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolContext(BaseModel):
    """The runtime context passed to every tool function — §9.2.

    Eight fields, all read-only. Tools cannot write the event log,
    inspect the principal, or perform authz checks (all done by the
    dispatch flow before the tool runs).

    The five Phase 1 fields (``agent_id``, ``session_id``,
    ``thread_id``, ``principal``, ``secrets``) are required for every
    tool invocation. The Phase 2.0 additions (``workspace_root``,
    ``path_policy``) are optional and populated by the
    :class:`ToolsSubsystem` from its constructor args when present —
    they let user-authored bundle tools at
    ``<workspace>/bundles/kolsch/tools/`` reach the platform's path
    policy and workspace root without holding a closure over them
    (the way the Phase 1 in-engine factories do). When the subsystem
    was constructed without a ``path_policy`` / ``root`` (e.g., tests
    that don't exercise file tools), the fields stay ``None`` —
    bundle tools that need them should raise a clear error rather
    than silently misbehave.

    The QoL addition (``tool_call_id``) carries the engine's per-call
    id so tools that need to persist artifacts (e.g., bash overflow
    writing the full stream to a workspace tmp path) can name them
    by call rather than colliding across concurrent invocations.
    Populated by ``_ObservedToolHandle.__call__`` and the
    force-invoke read path; ``None`` when a tool runs outside the
    engine (tests, REPL probes).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    agent_id: AgentId
    session_id: SessionId
    thread_id: ThreadId
    principal: str
    secrets: dict[str, str]
    # Phase 2.0 (TOOL-R7) — populated by the dispatcher; None elsewhere.
    workspace_root: Path | None = None
    path_policy: PathPolicy | None = None
    # QoL — tool output truncation + persistence — populated by the
    # observer / engine force-invoke path; None when a tool runs
    # outside the engine (tests, programmatic dispatch).
    tool_call_id: str | None = None


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
    """Everything the dispatch needs to know about a tool — spec §2.2.

    Phase 2.2 (ENV-R5 / Step 11) — gains an optional ``summary``
    field used by :class:`templating.vars.tools.ToolItem` for the
    prompt's compact tool listing. Derived from the first
    sentence of ``description`` when omitted from
    :func:`tool` (the substring through the first period, trimmed).

    qol-action-namespace-bundle-scoped.md Step 2 — gains
    ``home_bundle``, stamped at registration time by
    :meth:`ToolsSubsystem.register_for_agent` (Option B from the
    spec writeup §2). The ``@tool`` decorator stays bundle-unaware;
    the field is None at decoration time and populated when the
    engine binds the tool to an agent's session.
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    func: Callable[..., Awaitable[Any]]
    secrets_needed: list[str] = field(default_factory=list)
    action: str | None = None
    timeout_seconds: float | None = None
    summary: str = ""
    home_bundle: str | None = None

    @property
    def action_name(self) -> str:
        """The action string used for authz checks.

        qol-action-namespace-bundle-scoped.md Step 2 — when
        ``home_bundle`` is set (always true for engine-driven
        registration), returns the 3-seg catalog ref
        ``<home_bundle>/tools/<name>``. When unset (Phase 1
        programmatic test registrations that construct
        ToolDefinition directly without going through the engine),
        falls back to the legacy ``tool.<name>`` form so existing
        test paths stay byte-identical. ``action`` (set via the
        decorator's ``action=`` kwarg) still overrides both.
        """
        if self.action is not None:
            return self.action
        if self.home_bundle is None:
            return f"tool.{self.name}"
        return f"{self.home_bundle}/tools/{self.name}"

    def to_info(self) -> ToolInfo:
        return ToolInfo(
            name=self.name,
            description=self.description,
            args_schema=self.args_schema.model_json_schema(),
        )


def _derive_summary(description: str) -> str:
    """Spec §5.1 fallback: the substring of ``description`` from
    start through the first period, trimmed. When the description
    has no period, the full description is the summary (still
    trimmed)."""
    period_index = description.find(".")
    if period_index < 0:
        return description.strip()
    return description[: period_index + 1].strip()


def tool(
    *,
    name: str,
    description: str,
    args_schema: type[BaseModel],
    secrets_needed: list[str] | None = None,
    action: str | None = None,
    timeout_seconds: float | None = None,
    summary: str | None = None,
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

    Phase 2.2 (ENV-R5 / Step 11):
    - ``summary`` — optional one-line summary surfaced via
      :class:`templating.vars.tools.ToolItem`'s ``.summary`` for the
      prompt's compact tool listing. When omitted, derived from the
      first sentence of ``description`` (the substring through the
      first period, trimmed). The model still receives the full
      ``description`` via the upstream LLM API's ``tools=`` parameter
      regardless of ``summary``.
    """
    if not _NAME_PATTERN.fullmatch(name):
        msg = f"tool name {name!r} must match ^[a-z][a-z0-9_]*$ per spec §2.2"
        raise ValueError(msg)

    resolved_summary = summary if summary is not None else _derive_summary(description)

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
            summary=resolved_summary,
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

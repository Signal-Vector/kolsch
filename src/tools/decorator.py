"""The `@tool` decorator plus the canonical types that ride alongside it.

A `@tool`-decorated function carries a
[`ToolDefinition`][tools.decorator.ToolDefinition] attached to its
`_tool_definition` attribute. The Tools subsystem discovers tools by
reading that attribute. The other types in this module —
[`ToolContext`][tools.decorator.ToolContext],
[`ToolResult`][tools.decorator.ToolResult],
[`ToolInfo`][tools.decorator.ToolInfo] — make up the public shape the
dispatch flow exposes to tool authors and consumers.

For the full Tools subsystem contract (discovery, registration,
dispatch), see the
[tools subsystem](../explanation/tools-subsystem.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, SkipValidation

from files import PathPolicy
from schemas import AgentId, SessionId, ThreadId
from tools.services import (
    Cards,
    Prompts,
    ServiceName,
    SessionView,
    Workflows,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from engine.context import CancelToken  # noqa: F401 — referenced by docstring

# Tool name pattern.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolContext(BaseModel):
    """The runtime context passed to every tool function.

    Eight read-only fields, populated by the dispatch flow before the
    tool body runs. Tools cannot mutate the event log or override
    authz decisions — those are handled upstream of the tool body.

    The five core fields (`agent_id`, `session_id`, `thread_id`,
    `principal`, `secrets`) are required for every tool invocation.
    `workspace_root` and `path_policy` are optional, populated by the
    [`ToolsSubsystem`][tools.subsystem.ToolsSubsystem] from its
    constructor args when set, so user-authored bundle tools at
    `<workspace>/bundles/kolsch/tools/` can reach the platform's path
    policy and workspace root without holding a closure over them.
    When the subsystem was constructed without a `path_policy` /
    `root` (e.g. tests that don't exercise file tools), the fields
    stay `None` — bundle tools that need them should raise a clear
    error rather than silently misbehave.

    `tool_call_id` carries the engine's per-call id so tools that
    need to persist artifacts (e.g. bash overflow writing the full
    stream to a workspace tmp path) can name them by call rather than
    colliding across concurrent invocations. `None` when a tool runs
    outside the engine (tests, REPL probes).

    `cancellation` carries the exchange's
    [`CancelToken`][engine.context.CancelToken] so a tool body can
    race long-running work (subprocess waits, network calls) against
    `ctx.cancellation.wait_cancelled()`. Tools that don't poll it
    work unchanged; `None` outside the engine path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    agent_id: AgentId
    session_id: SessionId
    thread_id: ThreadId
    principal: str
    secrets: dict[str, str]
    workspace_root: Path | None = None
    path_policy: PathPolicy | None = None
    tool_call_id: str | None = None
    # Typed as `Any` rather than `CancelToken | None` to avoid a
    # tools ↔ engine import cycle at runtime. Tool authors should
    # annotate access sites as `CancelToken | None` and import lazily.
    cancellation: Any = None

    # spec-001 additions: four capability-scoped service handles the
    # dispatcher populates per-tool-call from the `ChatServiceRegistry`
    # when the tool declares them via `@tool(requires=[...])`. All
    # four default to `None`; in headless mode (no registry passed to
    # `dispatch_tool`) they stay `None`. Author narrowing goes through
    # `tools.services.require_services()`, never `isinstance`.
    #
    # Wrapped in `SkipValidation[...]` because the service Protocols
    # carry `@runtime_checkable=False` (deliberate — see
    # `tools/services/cards.py`) and Pydantic v2 would otherwise
    # build an `isinstance`-based validator that rejects the Protocol
    # class outright. `SkipValidation` preserves the author-facing
    # type shape (mypy sees `Cards | None`) while telling Pydantic to
    # accept the value as-is at construction time.
    cards: SkipValidation[Cards | None] = None
    prompts: SkipValidation[Prompts | None] = None
    workflows: SkipValidation[Workflows | None] = None
    session: SkipValidation[SessionView | None] = None


class ToolResult(BaseModel):
    """The return shape of `ToolsSubsystem.invoke_tool`.

    Returned by
    [`ToolsSubsystem.invoke_tool`][tools.subsystem.ToolsSubsystem.invoke_tool].
    `result` is whatever the tool returned, captured verbatim.
    `metadata` carries optional latency / cost diagnostics the
    dispatch flow attaches.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    result: Any
    metadata: dict[str, Any] = {}


class ToolInfo(BaseModel):
    """Metadata-only view of a registered tool.

    Returned by
    [`ToolsSubsystem.list_registered_tools`][tools.subsystem.ToolsSubsystem.list_registered_tools]
    so callers can introspect without holding function references.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    args_schema: dict[str, Any]


@dataclass
class ToolDefinition:
    """Everything the dispatch needs to know about a tool.

    The `@tool` decorator stamps one of these on the decorated
    function's `_tool_definition` attribute. The Tools subsystem
    discovers tools by reading that attribute.

    `summary` is used for the prompt's compact tool listing; it falls
    back to the first sentence of `description` when omitted from the
    decorator.

    `home_bundle` is stamped at registration time by the engine when
    binding a tool to an agent's session. The `@tool` decorator stays
    bundle-unaware — the field is `None` at decoration time and
    populated when the engine binds the tool. `action_name` uses
    `home_bundle` to derive the authz action string.
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
    requires: list[ServiceName] = field(default_factory=list)
    """spec-001: names of tool services the dispatcher must inject on
    `ToolContext` before this tool runs. Empty by default; the
    dispatcher only populates fields listed here."""

    target_shape: Literal["auto", "json"] = "auto"
    """bug-029: hint for `_extract_target_from_args` on how to
    render the approval widget's `Args` line for this tool.

    - ``"auto"`` (default) — bug-025's priority walk:
      first stringy attr in
      :data:`tools.dispatch._TARGET_ARG_PRIORITY`
      (``path`` / ``command`` / ``url`` / ``ref`` / ``run_id``)
      wins, with a truncated ``model_dump_json`` fallback when
      none matches. Correct for tools whose args are conventionally
      one load-bearing field (``bash``, ``read_file``,
      ``fetch_website``).
    - ``"json"`` — skip the priority walk; always return the
      full ``model_dump_json(exclude_defaults=True)`` truncated to
      200 chars with a ``…`` suffix if longer. Used by the
      per-workflow virtual tools built in
      :func:`workflows.tool_schema.build_virtual_tool_definitions`
      whose args model is arbitrary and multi-field — showing
      just the ``url`` (or first priority attr) misleads the user
      about what the tool is actually being called with.
    """

    @property
    def action_name(self) -> str:
        """The authz action string used to check this tool's permission.

        When `home_bundle` is set (the engine-driven registration
        path), returns the catalog ref `<home_bundle>/tools/<name>`.
        When unset (programmatic test registrations that construct
        `ToolDefinition` directly without going through the engine),
        falls back to the legacy `tool.<name>` form so existing test
        paths stay byte-identical. The `action` constructor arg
        overrides both.
        """
        if self.action is not None:
            return self.action
        if self.home_bundle is None:
            return f"tool.{self.name}"
        return f"{self.home_bundle}/tools/{self.name}"

    def to_info(self) -> ToolInfo:
        """Return a metadata-only [`ToolInfo`][tools.decorator.ToolInfo] view."""
        return ToolInfo(
            name=self.name,
            description=self.description,
            args_schema=self.args_schema.model_json_schema(),
        )


def _derive_summary(description: str) -> str:
    """Derive a one-line summary from `description`.

    Returns the substring from the start through the first period,
    trimmed. When `description` contains no period, the full string
    is the summary (still trimmed). Used as the fallback when the
    `@tool` decorator's `summary=` kwarg is omitted.
    """
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
    requires: list[ServiceName] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """The `@tool` decorator.

    Attaches a [`ToolDefinition`][tools.decorator.ToolDefinition] to
    the function's `_tool_definition` attribute. The function is
    returned unchanged so the underlying coroutine remains directly
    callable.

    Args:
        name: The tool's wire name. Must match `^[a-z][a-z0-9_]*$`.
        description: Long-form description; surfaced to the model
            verbatim via the upstream LLM API's `tools=` parameter.
        args_schema: A Pydantic model describing the tool's inputs.
            Required — the dispatch pipeline validates arguments
            against it before invoking the function. Tools that take
            no arguments declare an empty `BaseModel`.
        secrets_needed: Names of secrets the dispatch should resolve
            into `ToolContext.secrets` before the tool runs.
        action: Override the authz action string. When omitted,
            `ToolDefinition.action_name` derives one from the tool's
            `home_bundle` and `name`.
        timeout_seconds: Optional per-call timeout enforced by the
            dispatch.
        summary: Optional one-line summary for the prompt's compact
            tool listing. When omitted, derived from the first
            sentence of `description`. The model still receives the
            full `description` via the LLM API regardless.
        requires: Names of tool services (`"cards"`, `"prompts"`,
            `"workflows"`, `"session"`) the dispatcher must inject on
            `ToolContext` before this tool runs. Members must be in
            `ServiceName`; unknown
            names raise `ValueError` at decoration time. Default
            `None` — tools that don't opt in see all four service
            fields as `None`.

    Returns:
        The decorator that stamps a `ToolDefinition` and returns the
        function unchanged.

    Raises:
        ValueError: `name` doesn't match the required pattern, or
            `requires` contains an entry not in `ServiceName`.
    """
    if not _NAME_PATTERN.fullmatch(name):
        msg = f"tool name {name!r} must match ^[a-z][a-z0-9_]*$"
        raise ValueError(msg)

    if requires is not None:
        valid_services = set(get_args(ServiceName))
        unknown = [s for s in requires if s not in valid_services]
        if unknown:
            msg = (
                f"tool {name!r} declared unknown requires={unknown}; "
                f"valid: {sorted(valid_services)}"
            )
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
            requires=list(requires or []),
        )
        fn._tool_definition = defn  # type: ignore[attr-defined]
        return fn

    return decorate


def get_tool_definition(
    fn: Callable[..., Awaitable[Any]],
) -> ToolDefinition:
    """Return the `ToolDefinition` attached to a decorated function.

    Args:
        fn: A function decorated with `@tool`.

    Returns:
        The `ToolDefinition` stamped by the decorator.

    Raises:
        AttributeError: `fn` has no `_tool_definition` attribute.
            Callers typically filter by
            `hasattr(fn, "_tool_definition")` during discovery to
            avoid the raise.
        TypeError: `fn._tool_definition` exists but isn't a
            `ToolDefinition` (e.g. shadowed by an unrelated
            attribute).
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
    """Update an existing `@tool` function's metadata in place.

    Used by tests to add `secrets_needed` AFTER decoration when the
    test wants to keep the original `@tool(...)` call concise.
    Mutates the attached `ToolDefinition` directly.

    Args:
        fn: A function decorated with `@tool`.
        secrets_needed: New list of secret names. `None` leaves the
            existing list unchanged.
    """
    defn = get_tool_definition(fn)
    if secrets_needed is not None:
        defn.secrets_needed = list(secrets_needed)

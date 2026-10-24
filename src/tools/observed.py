"""Observed tool-invocation core — the caller-agnostic emit seam.

[spec-119] Every observed tool call in kolsch flows through one small
core: [`invoke_observed`][tools.observed.invoke_observed] emits
`tool.called` before dispatching through
`ToolsSubsystem.invoke_tool`, emits `tool.result` / `tool.failed`
after, and returns a typed [`ToolOutcome`][tools.observed.ToolOutcome]
— **without deciding any caller's return shape**. The engine's
`_ObservedToolHandle` (`src/engine/observers.py`) is the first
caller: it maps the outcome into the framework's return dict and
keeps the exchange-coupled machinery (span registry, cancel
terminal). A future MCP tool bridge (T2) is the second: same events,
its own sanitized mapping (which drops `recovery_hint`).

Context (`agent_id` / `session_id` / `thread_id`) and the `emit`
sink are **plain per-call parameters**, not constructor state, so a
bridge can rebind a fresh context and its own sink per exchange.
The core never registers spans, never emits `session.interrupted`,
and lets `asyncio.CancelledError` propagate to the caller.

The recovery-hint derivers (`_derive_authz_recovery_hint` /
`_suggest_glob_pattern`) live here — moved from `engine.observers`
— because the core builds the `tool.failed` event data and a
`tools/`-side module cannot import them from `engine/` without an
import cycle (`engine` imports `tools`, never the reverse).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from errors import PlatformError, ToolUnauthorized
from schemas import (
    Actor,
    Error,
    EventType,
    ToolCalledData,
    ToolFailedData,
    ToolResultData,
)
from tools.decorator import ToolContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from schemas import CallerChain


# ---------------------------------------------------------------------------
# Typed outcome — Ok | Denied | Failed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ok:
    """Successful invocation. `result` is the tool's **raw** return.

    The emitted `tool.result` event carries the *normalized* payload
    (non-dict results wrapped as `{"value": ...}`); the outcome
    carries the raw value so callers forward exactly what the tool
    returned. The emit-normalized / return-raw split is intentional.
    """

    result: Any


@dataclass(frozen=True, slots=True)
class Denied:
    """Authorization denied the call (`ToolUnauthorized`).

    `recovery_hint` is the derived `/perm allow <ref> <pattern>`
    chat command (or `None` when nothing actionable can be
    suggested). Callers decide whether to surface it — the engine
    forwards it to the model; an external bridge must drop it.
    """

    error: Error
    recovery_hint: str | None


@dataclass(frozen=True, slots=True)
class Failed:
    """The invocation failed with any other `PlatformError`.

    `recovery_hint` carries the exception's opt-in `.recovery_hint`
    attribute (qol-090 — e.g. `ToolExecutionFailed`), or `None`.
    """

    error: Error
    recovery_hint: str | None


ToolOutcome = Ok | Denied | Failed
"""The tagged union `invoke_observed` returns — no caller return
shape (no `{"error": ...}` dict, no MCP content blocks) baked in."""


# ---------------------------------------------------------------------------
# The invocation core
# ---------------------------------------------------------------------------


# PLR0913 — the core deliberately takes context as plain per-call
# parameters (no fixed-context constructor) so every caller can
# rebind per exchange; the parameter list IS the seam contract.
async def invoke_observed(
    *,
    emit: Callable[..., Awaitable[None]],
    agent_id: str,
    session_id: str,
    thread_id: str,
    tools_subsystem: Any,  # noqa: ANN401 — duck-typed `invoke_tool` facade; stubs in tests
    tool_name: str,
    args: dict[str, Any],
    caller_chain: CallerChain,
    tool_call_id: str | None = None,
    cancel: Any = None,  # noqa: ANN401 — CancelToken; typed loosely to avoid a tools → engine import
) -> ToolOutcome:
    """Run one observed tool invocation: emit `tool.*`, return the outcome.

    Emits `tool.called` BEFORE dispatching, then exactly one of
    `tool.result` (success) / `tool.failed` (denied or errored)
    after, all with `actor=Actor(kind="agent", id=agent_id)` and a
    **copied** `arguments` dict. Event emission is the ground-truth
    contract; the typed outcome is deliberately caller-agnostic.

    Cancellation: `asyncio.CancelledError` from `invoke_tool`
    propagates unchanged — the core never converts it to `Failed`
    and never emits a terminal for it (the engine wrapper owns the
    `session.interrupted` cancel terminal). `ExchangeCancelled` is a
    `PlatformError` subclass and lands in the `Failed` arm — the
    same arm ordering the pre-split `_ObservedToolHandle` had, kept
    byte-identical for behaviour preservation.

    Args:
        emit: Async event sink with the engine-emit keyword shape
            `(type_=EventType, data=dict, actor=Actor)`. The engine
            passes its serialized `_emit_void`; other callers bring
            their own sink (envelope stamping — `trace_id`, ids —
            is the sink's business, not the core's).
        agent_id: The invoking agent; stamped as the event actor and
            threaded into the `ToolContext` (`principal` becomes
            `agent.<agent_id>`).
        session_id: Session the call belongs to (context field).
        thread_id: Thread the call belongs to (context field).
        tools_subsystem: Object exposing
            `ToolsSubsystem.invoke_tool(tool_name=, args=, context=)`.
            Duck-typed so tests and programmatic callers can stub it.
        tool_name: The registered tool name to dispatch.
        args: The model-emitted arguments. Copied before both the
            event emit and the dispatch — the caller's dict is never
            mutated or shared.
        caller_chain: The proximate-first frames the call's
            authorization is judged against, set on the dispatched
            `ToolContext`. Required: the engine passes its exchange
            chain, the MCP bridge passes the chain it was constructed
            with (never one read from the wire).
        tool_call_id: The framework's call id; when `None` the core
            mints the identical legacy fallback `tc_<uuid4>`.
        cancel: Optional `CancelToken` forwarded as
            `ToolContext.cancellation` (typed `Any` to keep this
            module engine-import-free).

    Returns:
        `Ok(result)` with the raw tool result, `Denied(error,
        recovery_hint)` on `ToolUnauthorized`, or `Failed(error,
        recovery_hint)` on any other `PlatformError`.
    """
    call_id = tool_call_id or f"tc_{uuid4()}"

    # BEFORE — tool.called.
    await emit(
        type_=EventType.TOOL_CALLED,
        data=ToolCalledData(
            tool_name=tool_name,
            tool_call_id=call_id,
            arguments=dict(args),
        ).model_dump(mode="json"),
        actor=Actor(kind="agent", id=agent_id),
    )

    try:
        result = await tools_subsystem.invoke_tool(
            tool_name=tool_name,
            args=dict(args),
            context=ToolContext(
                agent_id=agent_id,  # type: ignore[arg-type]
                session_id=session_id,  # type: ignore[arg-type]
                thread_id=thread_id,  # type: ignore[arg-type]
                principal=f"agent.{agent_id}",
                secrets={},
                caller_chain=caller_chain,
                tool_call_id=call_id,
                cancellation=cancel,
            ),
        )
    except ToolUnauthorized as exc:
        err = Error.from_platform_error(exc)
        # Derive the exact `/perm allow` chat command from the
        # matched rule's `what` + the in-flight args. Carried as
        # structured data on both the event and the outcome; the
        # CALLER decides whether the invoker gets to see it.
        recovery_hint = _derive_authz_recovery_hint(exc, arguments=args)
        await emit(
            type_=EventType.TOOL_FAILED,
            data=ToolFailedData(
                tool_call_id=call_id,
                tool_name=tool_name,
                error=err,
                recovery_hint=recovery_hint,
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=agent_id),
        )
        return Denied(error=err, recovery_hint=recovery_hint)
    except PlatformError as exc:
        err = Error.from_platform_error(exc)
        # qol-090: forward the recovery_hint carried on the
        # exception (currently ToolExecutionFailed sets it via
        # errors.recovery.recovery_hint) into the event payload and
        # the outcome. Hint is None for any exception class that
        # doesn't opt in.
        recovery_hint = getattr(exc, "recovery_hint", None)
        await emit(
            type_=EventType.TOOL_FAILED,
            data=ToolFailedData(
                tool_call_id=call_id,
                tool_name=tool_name,
                error=err,
                recovery_hint=recovery_hint,
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=agent_id),
        )
        return Failed(error=err, recovery_hint=recovery_hint)

    # AFTER — tool.result. The EVENT carries the normalized payload;
    # the OUTCOME carries the raw result (do not normalize it).
    await emit(
        type_=EventType.TOOL_RESULT,
        data=ToolResultData(
            tool_call_id=call_id,
            tool_name=tool_name,
            result=_normalize_result(result.result),
        ).model_dump(mode="json"),
        actor=Actor(kind="agent", id=agent_id),
    )
    return Ok(result=result.result)


def _normalize_result(value: object) -> dict[str, Any]:
    """Wrap a tool result for the event-data dict shape.

    The event payload `tool.result.data.result` is a dict. Dict
    results pass through; everything else is wrapped under a `value`
    key so the verbatim content remains capturable.
    """
    if isinstance(value, dict):
        return value
    return {"value": value}


# ---------------------------------------------------------------------------
# Denied-recovery hint — derives the `/perm allow` slash command
# ---------------------------------------------------------------------------


_TARGET_ARG_PRIORITY = ("path", "command", "url")
"""Same order as `tools.dispatch._TARGET_ARG_PRIORITY`. Duplicated
here (rather than imported) because the invocation core can't take a
dependency on the dispatcher's internal helper — both modules read
the convention from the same contract."""


def _derive_authz_recovery_hint(
    exc: ToolUnauthorized,
    *,
    arguments: dict[str, Any] | None,
) -> str | None:
    """Build the `/perm allow <tool> <pattern>` hint string.

    Reads `ToolUnauthorized.detail`'s `matched_rule` to find the
    qualified tool ref (or falls back to the legacy action namespace
    `tool.<name>` when the matched rule's `what` is in old shape).
    The suggested pattern is derived from the in-flight arguments
    via the same convention the modal uses (first whitespace-bounded
    token for bash; parent dir for file tools; `*` otherwise).
    Returns `None` if there's nothing actionable to surface (e.g.
    no rule matched — pure default deny — in which case the user
    can use `/perm allow <ref> *`).

    The pattern is intentionally **broad** by default (`*`); the
    user can edit before submitting. The point of the hint is to
    save them from looking up the qualified tool ref and the
    slash-command grammar — not to guess their intent precisely.
    """
    detail = exc.detail or {}
    tool_name = detail.get("tool_name") or ""
    matched_rule = detail.get("matched_rule") or {}
    if not isinstance(tool_name, str) or not tool_name:
        return None

    # Prefer the matched rule's `what` when it's the new
    # qualified-ref shape (contains `/`); fall back to a legacy
    # `tool.<name>` action namespace and ultimately to the bare
    # tool name. Bare-name fallback isn't a valid /perm tool arg
    # — we suppress the hint so we never tell the user to type
    # `/perm allow bash` (which would fail validation).
    what = matched_rule.get("what") if isinstance(matched_rule, dict) else None
    tool_ref = what if isinstance(what, str) and "/" in what else None

    if tool_ref is None:
        return None

    pattern = _suggest_glob_pattern(tool_name=tool_name, arguments=arguments)
    # Quote when the pattern contains whitespace so the slash
    # parser splits the verb / ref / pattern correctly.
    quoted = f'"{pattern}"' if " " in pattern else pattern
    return f"/perm allow {tool_ref} {quoted}"


def _suggest_glob_pattern(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> str:
    """Mirror the chat modal's `suggest_pattern` helper.

    Same three heuristics in priority order:

    1. bash with a command target: first whitespace-bounded token
       → `"<token> *"`.
    2. File tools with a path target: parent-dir glob
       `"<parent>/*"`.
    3. Fallback: `"*"`.

    Kept independent of the modal so the platform can run without
    importing chat code (`tools/` doesn't depend on `cli/`).
    """
    if not arguments:
        return "*"
    target: str | None = None
    for attr in _TARGET_ARG_PRIORITY:
        value = arguments.get(attr)
        if isinstance(value, str):
            target = value
            break
    if target is None:
        return "*"
    if tool_name == "bash":
        first_token = target.strip().split(maxsplit=1)[0] if target.strip() else ""
        if first_token:
            return f"{first_token} *"
    if tool_name in {"read_file", "write_file", "list_files"}:
        parent, sep, _name = target.rpartition("/")
        if sep:
            return f"{parent}/*"
    return "*"

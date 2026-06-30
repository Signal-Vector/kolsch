"""Event renderer for `kolsch chat`.

The chat is a **pure consumer** of
[`AgentEngine.execute_stream`][engine.AgentEngine.execute_stream].
Every visible block in the REPL corresponds to one event emitted by
the engine; the renderer adds no semantic layer of its own. This is
the load-bearing architectural property enforced by AGENTS.md's
chat-renderer hard rule.

The event-type → render-action mapping:

- `thread.exchange_started` — silent boundary (no UI).
- `user.message` — silent. prompt_toolkit's terminal echo of the
  submitted prompt line is the canonical scrollback "You:
  <content>"; the event confirms it for the JSONL only.
- `model.request` — print the "<AgentName> is thinking..."
  indicator.
- `model.response` (non-streamed) — render the final content
  (markdown).
- `tool.called` — silent; capture args for the result capsule.
- `tool.result` — render the tool capsule; truncate >20 lines.
- `tool.failed` — render the panel in dim cyan, or the
  permission capsule for authz denies.
- `agent.message` — if text differs from what `model.response`
  rendered, re-print; otherwise no-op.
- `thread.exchange_completed` — silent (input prompt unlocks via
  the loop).
- `thread.exchange_failed` — print a system notice; loop
  continues.
- `model.fallback` — italic notice "Falling back to <model>".
- `config.set` / `memory.*` — silent (diagnostic-only in the
  JSONL).

**Live region scope.** A Rich `Live` region is permitted only
around the in-progress agent block during streaming. The
FakeFramework + EchoLLM emit `model.response` as a single
non-streamed event; there is no token-by-token streaming to wrap
today. Future streaming connectors will add a `Live` ctx around the
`model.response` token-append loop — the exit-before-next-static-
block rule is what keeps native scrolling intact.

**Replay-safety.** This module is a pure async consumer of an event
iterator. It does not depend on wall-clock or fresh runtime state.
The replay path feeds it events read from the JSONL; the output
for a replayed session is identical to what the original chat
rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TextIO

from datetime import datetime

from rich.console import Console
from rich.text import Text

from schemas import EventType


# ---------------------------------------------------------------------------
# Capsule primitive
# ---------------------------------------------------------------------------
#
# A capsule renders a chat block as a sidebar:
#
#     │ tool · HH:MM:SS                                   [cyan3]
#     │ bash(command='ls -la')
#     │   running
#
# Each line is prefixed with the bar character (`│` U+2502) in the source's
# color. The header carries `<source> · HH:MM:SS` (source label in source
# color; timestamp in grey50). No closing border; blank line between
# capsules.

_BAR = "│"
_CODE_BAR = "┃"

_CAPSULE_PALETTE: dict[str, str] = {
    "user": "bright_blue",
    "agent": "grey70",
    "system": "grey42",
    "permission": "dark_orange3",
    "tool": "cyan3",
    "tool-dim": "cyan3 dim",
    "code": "cyan",
    # Workflow-event capsules. The renderer in
    # `cli._workflow_capsules` looks up these keys when emitting
    # capsules from a workflow's JSONL tail. `workflow_muted` is
    # used for branch-not-taken / pruned events; `workflow_paused`
    # for the inline `user_input` prompt; `workflow_failed` for
    # the `caller_message` failure surface.
    "workflow": "cyan",
    "workflow_muted": "bright_black",
    "workflow_paused": "yellow",
    "workflow_failed": "red",
}


def _now_hhmmss() -> str:
    return datetime.now().strftime("%H:%M:%S")  # noqa: DTZ005 — local time is right for UX


def _render_agent_capsule(console: Console, agent_name: str, text: str) -> None:
    """Render an agent message as a sidebar capsule with code-block styling.

    Splits the text on triple-backtick boundaries. Prose lines get
    the outer agent bar `│`; code-block lines get the outer bar
    plus the inner `┃` in cyan. The language label appears on its
    own `┃ <lang>` line before the code body.
    """
    style = _CAPSULE_PALETTE["agent"]
    ts = _now_hhmmss()

    # Header.
    header = Text()
    header.append(f"{_BAR} ", style=style)
    header.append(agent_name, style=style)
    header.append(f" · {ts}", style="grey50")
    console.print(header)

    in_code = False
    lang = ""
    for raw_line in text.split("\n"):
        if raw_line.startswith("```"):
            if not in_code:
                lang = raw_line[3:].strip() or "text"
                # Emit the `┃ <lang>` header inside the agent capsule.
                line = Text()
                line.append(f"{_BAR} ", style=style)
                line.append(f"{_CODE_BAR} ", style=_CAPSULE_PALETTE["code"])
                line.append(lang, style=_CAPSULE_PALETTE["code"])
                console.print(line)
                in_code = True
            else:
                in_code = False
                lang = ""
            continue
        line = Text()
        line.append(f"{_BAR} ", style=style)
        if in_code:
            line.append(f"{_CODE_BAR} ", style=_CAPSULE_PALETTE["code"])
            line.append(raw_line)
        else:
            line.append(raw_line)
        console.print(line)

    console.print()


def _capsule(
    console: Console,
    *,
    source: str,
    header_extra: str = "",
    body_lines: list[str],
    timestamp: str | None = None,
    color: str | None = None,
) -> None:
    """Print one capsule.

    `source` is the label (`"tool"` / `"permission"` / etc.);
    `color` overrides the palette lookup when set (e.g. the dim
    failed-tool variant). `header_extra` lets callers append
    duration info (`→ HH:MM:SS (1.8s)`). `body_lines` are the
    content lines; each gets the bar prefix.
    """
    style = color if color is not None else _CAPSULE_PALETTE.get(source, "white")
    ts = timestamp if timestamp is not None else _now_hhmmss()

    # Header: bar + label + · + timestamp.
    header = Text()
    header.append(f"{_BAR} ", style=style)
    header.append(source, style=style)
    header.append(f" · {ts}", style="grey50")
    if header_extra:
        header.append(f" {header_extra}", style=style)
    console.print(header)

    # Body: each line gets the bar prefix.
    for line in body_lines:
        body = Text()
        body.append(f"{_BAR} ", style=style)
        body.append(line)
        console.print(body)

    # Blank line separator between capsules.
    console.print()


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from schemas import Event


# ---------------------------------------------------------------------------
# Rendering knobs
# ---------------------------------------------------------------------------


_TOOL_RESULT_MAX_LINES = 20
"""Tool-result panel truncation threshold.

Long results (>20 lines) collapse to the first 20 lines plus a
`[N more lines, /show <id> for full]` suffix. The full content
stays in the JSONL — the truncation is display-only."""


_THINKING_INDICATOR = "  …"
"""Subtle "is thinking" indicator printed once per turn on the
first `model.request`.

A one-line text indicator (not a spinning animation) — the
FakeFramework + EchoLLM resolve instantly so an animation would
only ever flash. When real streaming connectors land the indicator
becomes the spinner inside a Rich `Live` ctx.

**Per-turn gating.** In multi-iteration tool-call turns the
framework loops `model.request → tool.called → tool.result →
model.request` until a text response. Without gating the indicator
fired on every `model.request`, producing N ellipses between every
tool-call panel. The renderer now tracks the current turn's id (set
on `thread.exchange_started`) and suppresses subsequent indicator
fires within the same turn; the next `thread.exchange_started`
(or `thread.exchange_completed` / `thread.exchange_failed`) resets
the gate."""


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class Renderer:
    """Pure consumer of an engine event stream.

    Construct one per turn (`thread.exchange_started` →
    `thread.exchange_completed` / `thread.exchange_failed`); nothing
    is meant to leak across turns — the renderer's only state is
    the current turn's block list. The runtime can re-instantiate
    freely; the constructor is cheap.

    Args:
        stdout: Where rendered text writes. The chat input loop
            passes the chat's stdout (which is `sys.stdout` in
            production and an `io.StringIO` in tests). Plain
            `write`-able file object; no terminal probing — the
            Rich `Console` below adapts.
        agent_name: The agent's display name. Used as the prefix
            for the agent block ("Helper: ..."). Derived upstream
            from `ResolvedAgentConfig.name`.
        console: Optional Rich `Console` injected by tests so they
            can capture output without poking at terminal
            autodetection. Production code uses the default
            (`Console(file=stdout)`).
    """

    def __init__(
        self,
        *,
        stdout: TextIO,
        agent_name: str,
        console: Console | None = None,
    ) -> None:
        self._stdout = stdout
        self._agent_name = agent_name
        # Tests inject a Console to control width / disable colours.
        # Production uses `force_terminal=False` so the Console doesn't
        # try to probe — the architectural commitment is to stay out
        # of terminal-capability negotiation.
        self._console = console or Console(
            file=stdout,
            force_terminal=False,
            highlight=False,
        )
        # Set by `model.response` so `agent.message` can decide
        # whether to re-render — only when text differs from the
        # streamed-final tokens.
        self._last_agent_text: str | None = None
        # Per-turn thinking-indicator gate. The current turn id is
        # set on `thread.exchange_started`; the first
        # `model.request` of a turn emits the indicator and records
        # the id; subsequent `model.request`s (tool-loop
        # iterations) see the recorded id and skip the print.
        # `thread.exchange_completed` / `thread.exchange_failed`
        # reset both fields so the next turn's first
        # `model.request` fires again.
        self._current_exchange_id: str | None = None
        self._last_indicated_exchange_id: str | None = None
        # `tool.called` is silent; `tool.result` / `tool.failed`
        # render the completed capsule. Carry the call's arguments
        # from `tool.called` to the matching result/failed event via
        # this map so the rendered signature shows args (the result
        # event itself doesn't carry them).
        self._tool_call_args: dict[str, dict[str, object]] = {}

    # -----------------------------------------------------------------------
    # The single public method
    # -----------------------------------------------------------------------

    async def render(self, event_stream: AsyncIterator[Event]) -> None:
        """Consume `event_stream` until it ends; render each event.

        Per-event dispatch is a table over the (small) event
        vocabulary. Each branch does exactly one rendering action.
        Unknown event types are silently ignored — this is
        intentional so a future engine event that lands before its
        renderer support does not crash the chat. The
        chat-renderer hard rule prevents the inverse — the renderer
        can't invent a UI element for an event the engine never
        emits.
        """
        async for event in event_stream:
            await self._dispatch(event)

    # -----------------------------------------------------------------------
    # Per-event branches — one method per event type
    # -----------------------------------------------------------------------

    async def _dispatch(self, event: Event) -> None:
        # Dispatch table — one entry per renderable event. Silent
        # event types (thread.exchange_started, user.message,
        # thread.exchange_completed, config.set, memory.*) are not
        # listed; absent entries are treated as silent. Keeping the
        # table here (rather than a long if/elif ladder) keeps the
        # per-event return count under ruff's PLR0911 threshold and
        # makes mapping changes a one-line edit.
        handler = _DISPATCH.get(event.type)
        if handler is None:
            return
        handler(self, event)

    def _track_exchange_started(self, event: Event) -> None:
        """Record the new turn's id so the per-turn indicator gate can fire once.

        Silent on the wire — `thread.exchange_started` stays
        UI-less; this handler only updates state.
        """
        self._current_exchange_id = str(event.event_id)
        self._last_indicated_exchange_id = None

    def _render_model_request(self, event: Event) -> None:
        """Emit `_THINKING_INDICATOR` once per turn, not once per `model.request`.

        In multi-iteration tool-call turns the framework loops
        `model.request → tool.called → tool.result → model.request`
        until a text response; printing the indicator on every
        `model.request` produced N indicators between tool panels.
        The current turn id is set on `thread.exchange_started`;
        emit on the first `model.request` where the current id
        differs from the last-indicated id, then record the current
        id so subsequent fires within the same turn skip the print.
        """
        _ = event  # turn id flows through self, not the event
        if self._current_exchange_id is None:
            # Defensive — event stream started mid-turn (no
            # `thread.exchange_started` seen). Render once so the
            # indicator isn't silently lost; cap further fires until
            # a real `thread.exchange_started` resets state.
            self._console.print(Text(_THINKING_INDICATOR, style="dim italic"))
            self._last_indicated_exchange_id = "<orphan>"
            return
        if self._current_exchange_id == self._last_indicated_exchange_id:
            return  # already indicated for this turn — suppress
        self._console.print(Text(_THINKING_INDICATOR, style="dim italic"))
        self._last_indicated_exchange_id = self._current_exchange_id

    def _reset_indicator_gate(self, event: Event) -> None:
        """Clear the per-turn indicator gate on `thread.exchange_completed`.

        The next turn's first `model.request` then fires the
        indicator again. `thread.exchange_failed` shares this reset
        logic but is wired separately (its handler also renders a
        failure notice); see `_render_exchange_failed`.
        """
        _ = event
        self._current_exchange_id = None
        self._last_indicated_exchange_id = None

    def _render_model_response(self, event: Event) -> None:
        content = event.data.get("content") or {}
        text = self._extract_text(content)
        if not text:
            return
        _render_agent_capsule(self._console, self._agent_name, text)
        self._last_agent_text = text

    def _render_agent_message(self, event: Event) -> None:
        content = event.data.get("content") or {}
        text = self._extract_text(content)
        if not text:
            return
        if text == self._last_agent_text:
            return  # non-divergent, no re-render
        # Diverges from what model.response rendered — re-print.
        _render_agent_capsule(self._console, self._agent_name, text)
        self._last_agent_text = text

    def _render_tool_called(self, event: Event) -> None:
        """`tool.called` is silent — one-capsule-per-tool-call discipline.

        Captures the call's arguments by `tool_call_id` so the
        matching `tool.result` / `tool.failed` capsule can render
        the full `<tool>(<args>)` signature line — result events
        themselves don't carry the arguments.

        Rich `Live`-based in-place evolution is deferred;
        static-capsule-only today.
        """
        tool_call_id = str(event.data.get("tool_call_id", ""))
        if tool_call_id:
            self._tool_call_args[tool_call_id] = event.data.get("arguments") or {}

    def _render_tool_result(self, event: Event) -> None:
        """Tool-result capsule — the completed form per spec."""
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        tool_call_id = str(event.data.get("tool_call_id", ""))
        args = self._tool_call_args.pop(tool_call_id, {})
        signature = f"{tool_name}({_format_args(args)})"
        result = event.data.get("result") or {}
        body, truncated_lines = _truncate_tool_body(
            _format_tool_result(result),
            max_lines=_TOOL_RESULT_MAX_LINES,
        )
        body_lines: list[str] = [signature]
        for line in body.split("\n"):
            body_lines.append(f"  {line}")
        if truncated_lines > 0:
            tool_call_id = str(event.data.get("tool_call_id", "?"))
            body_lines.append(
                f"  [{truncated_lines} more lines, /show {tool_call_id} for full]",
            )
        _capsule(
            self._console,
            source="tool",
            body_lines=body_lines,
        )

    def _render_tool_failed(self, event: Event) -> None:
        """Tool-failed capsule — dim cyan variant.

        The header gains a `failed` suffix; the whole bar dims so
        the user reads "transactional record, this didn't succeed."

        Authz denials get a distinct capsule (orange `permission`
        source) showing the matched rule and the exact `/perm`
        command to undo. Discriminator: the error code is
        `tool.unauthorized` AND the event carries a non-empty
        `recovery_hint`. Generic tool failures (unrelated to authz)
        still use the dim cyan `tool` capsule.
        """
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        tool_call_id = str(event.data.get("tool_call_id", ""))
        args = self._tool_call_args.pop(tool_call_id, {})
        signature = f"{tool_name}({_format_args(args)})"
        error = event.data.get("error") or {}
        code = _error_code(error)
        message = str(error.get("message", ""))
        recovery_hint = event.data.get("recovery_hint")

        if code == "tool.unauthorized" and isinstance(recovery_hint, str):
            self._render_denied_capsule(
                signature=signature,
                error=error,
                recovery_hint=recovery_hint,
            )
            return

        _capsule(
            self._console,
            source="tool",
            color=_CAPSULE_PALETTE["tool-dim"],
            header_extra="failed",
            body_lines=[signature, f"  {code}: {message}"],
        )

    def _render_denied_capsule(
        self,
        *,
        signature: str,
        error: dict[str, Any],
        recovery_hint: str,
    ) -> None:
        """Render the authz-denied capsule.

        Visual contract — distinct from the generic tool-failure
        capsule because the user must act:

        ```text
        │ permission · HH:MM:SS                          [dark_orange3]
        │ denied: <tool_ref>(<arg-summary>)
        │   matched rule:  what=<w>  pattern=<p>  effect=deny
        │
        │ to allow:  /perm allow <ref> <pattern>
        ```

        The matched rule's `what` / `pattern` are pulled from the
        wrapped `ToolUnauthorized` detail. When a rule carries the
        legacy regex shape (`who` present), the legacy `what` regex
        is surfaced instead — the user can still convert via
        `/perm` once they understand which rule fired.
        """
        detail = error.get("detail") or {}
        matched_rule = detail.get("matched_rule") or {}
        # Build the matched-rule line — both schema shapes render
        # cleanly into the capsule.
        rule_line = _format_matched_rule(matched_rule)
        body_lines = [f"denied: {signature}"]
        if rule_line:
            body_lines.append(f"  matched rule:  {rule_line}")
        body_lines.append("")
        body_lines.append(f"to allow:  {recovery_hint}")
        _capsule(
            self._console,
            source="permission",
            body_lines=body_lines,
        )

    def _render_exchange_failed(self, event: Event) -> None:
        """Print a notice and unlock the input loop.

        A failed turn in the middle of a chat does not end the
        chat; the user sees a notice and can keep going (with
        `/model` or by re-prompting).

        A Ctrl-C cancellation specifically renders the in-progress
        agent block in muted styling with a `[cancelled]` suffix
        (not the generic yellow exchange-failed line) so the user
        sees their partial reply, not a generic "exchange failed"
        string. The discriminator is the error's code:
        `engine.cancelled` triggers the muted path; anything else
        stays on the generic notice.
        """
        error = event.data.get("error") or {}
        code = _error_code(error)
        message = str(error.get("message", ""))
        # Failed turn ends the per-turn indicator gate too; the
        # next user message starts a fresh turn.
        self._current_exchange_id = None
        self._last_indicated_exchange_id = None
        if _is_cancellation_code(code):
            self._render_cancelled(message)
            return
        self._console.print(
            Text(
                f"[exchange failed — {code}: {message}]",
                style="yellow",
            ),
        )

    def _render_cancelled(self, message: str) -> None:
        """Render a Ctrl-C suffix on the partial agent block.

        `_last_agent_text` is set by the most recent
        `model.response` render; if the engine cancelled before any
        tokens landed there's no partial block — print the muted
        "[cancelled]" sentinel so the user sees something explicit.
        """
        partial = self._last_agent_text or ""
        if partial:
            suffix = Text(" [cancelled]", style="dim italic")
            self._console.print(suffix)
        else:
            self._console.print(
                Text(
                    f"[cancelled — {message}]" if message else "[cancelled]",
                    style="dim italic",
                ),
            )

    def _render_model_fallback(self, event: Event) -> None:
        """Render the model-fallback italic notice.

        The `model.fallback` event shape is
        `{from: {provider, model}, to: {provider, model}, reason, ...}`.
        """
        to = event.data.get("to") or {}
        reason = str(event.data.get("reason", "transient model failure"))
        to_label = (
            f"{to.get('provider', '?')}/{to.get('model', '?')}"
            if isinstance(to, dict)
            else str(to)
        )
        self._console.print(
            Text(
                f"falling back to {to_label} because {reason}",
                style="italic dim",
            ),
        )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _extract_text(content: dict[str, object]) -> str:
        """Pull the display string out of a `content` dict.

        Both `agent.message` and `model.response` carry their
        rendered text under `content.text` per the schemas
        (`AgentMessageData`, `ModelResponseData`). Multi-modal
        payloads grow this surface later; the renderer defensively
        stringifies an unexpected non-string `text` value.
        """
        text = content.get("text")
        if isinstance(text, str):
            return text
        if text is None:
            return ""
        return str(text)


# ---------------------------------------------------------------------------
# Module-private formatters
# ---------------------------------------------------------------------------


def _format_matched_rule(matched_rule: dict[str, object]) -> str:
    """Format the matched-rule line for the denied capsule.

    Both schema shapes render cleanly:

    - **New shape** (`who is None`): `what=<w>  pattern=<p>
      effect=<e>`.
    - **Legacy shape** (`who` present): `what=<w>  effect=<e>`.
      The legacy rule's `what` is a regex that matched either
      action or target; surface it verbatim so the user can
      recognise which rule fired before deciding whether to
      migrate it.

    Returns `""` when the matched-rule dict is empty (default
    deny — no rule matched, just the catch-all).
    """
    if not matched_rule:
        return ""
    what = matched_rule.get("what")
    effect = matched_rule.get("effect")
    pattern = matched_rule.get("pattern")
    who = matched_rule.get("who")
    if who is None and isinstance(pattern, str):
        return f"what={what!r}  pattern={pattern!r}  effect={effect!r}"
    return f"what={what!r}  effect={effect!r}"


def _error_code(error: dict[str, object]) -> str:
    """Pull the canonical error code from an Error envelope.

    The schema's canonical key is `type`; tests that build crafted
    events sometimes use `code` (mirroring the PlatformError
    catalog's user-facing field name) — handle both so the renderer
    is honest about whatever the producer emitted.
    """
    for key in ("type", "code"):
        value = error.get(key)
        if isinstance(value, str) and value:
            return value
    return "<unknown>"


_CANCELLATION_CODES: tuple[str, ...] = (
    "engine.cancelled",
    "exchange.cancelled",
)
"""Error codes that map to the muted-suffix render path.

The engine uses `exchange.cancelled` for `CancelToken`-triggered
teardown; the chat spec uses the prose `engine.cancelled`. Both
surface the same UX in the renderer."""


def _is_cancellation_code(code: str) -> bool:
    return code in _CANCELLATION_CODES or "cancel" in code.lower()


_PANEL_HEADER_ARG_MAX_LEN = 60
"""Truncation width for argument values in the tool-call panel
header so it stays one line. The full arguments live in the JSONL
under `tool.called.data.arguments`."""


def _format_args(args: dict[str, object]) -> str:
    """Render a tool's arguments as `key=val, key=val` for the panel header.

    Strings get repr'd so the user sees the quotes (helps
    distinguish an empty string from a missing argument); other
    types `str()` through. Long values truncate to
    `_PANEL_HEADER_ARG_MAX_LEN` chars with `...` so the panel
    header stays one line.
    """
    if not args:
        return ""
    parts = []
    for key, value in args.items():
        rendered = repr(value) if isinstance(value, str) else str(value)
        if len(rendered) > _PANEL_HEADER_ARG_MAX_LEN:
            rendered = rendered[: _PANEL_HEADER_ARG_MAX_LEN - 3] + "..."
        parts.append(f"{key}={rendered}")
    return ", ".join(parts)


def _format_tool_result(result: dict[str, object]) -> str:
    """Render a `tool.result.data.result` payload as displayable text.

    Most builtins (`read_file`, `bash`) return their primary
    output under a recognisable key (`content`, `stdout`). When
    neither is present, the payload stringifies — the user sees a
    JSON-ish dump rather than a cryptic blank.
    """
    for key in ("content", "stdout", "text", "value"):
        if key in result:
            value = result[key]
            return value if isinstance(value, str) else str(value)
    # Fall back to a compact dict repr so the panel still shows
    # something — full payload is in the JSONL.
    import json  # noqa: PLC0415

    try:
        return json.dumps(result, sort_keys=True, indent=2, default=str)
    except (TypeError, ValueError):
        return str(result)


def _truncate_tool_body(body: str, *, max_lines: int) -> tuple[str, int]:
    """Truncate a tool result to `max_lines`; return `(text, extras)`.

    `extras` is the count of lines that were dropped; the caller
    uses it to render the `[N more lines, ...]` suffix.
    """
    lines = body.splitlines()
    if len(lines) <= max_lines:
        return body, 0
    truncated = "\n".join(lines[:max_lines])
    return truncated, len(lines) - max_lines


_DISPATCH: dict[EventType, Callable[[Renderer, Event], None]] = {
    # Silent on the wire, but updates per-turn indicator state.
    EventType.THREAD_EXCHANGE_STARTED: lambda r, e: r._track_exchange_started(e),
    EventType.MODEL_REQUEST: lambda r, e: r._render_model_request(e),
    EventType.MODEL_RESPONSE: lambda r, e: r._render_model_response(e),
    EventType.TOOL_CALLED: lambda r, e: r._render_tool_called(e),
    EventType.TOOL_RESULT: lambda r, e: r._render_tool_result(e),
    EventType.TOOL_FAILED: lambda r, e: r._render_tool_failed(e),
    EventType.AGENT_MESSAGE: lambda r, e: r._render_agent_message(e),
    # Silent on the wire; resets the per-turn indicator gate.
    EventType.THREAD_EXCHANGE_COMPLETED: lambda r, e: r._reset_indicator_gate(e),
    EventType.THREAD_EXCHANGE_FAILED: lambda r, e: r._render_exchange_failed(e),
    EventType.MODEL_FALLBACK: lambda r, e: r._render_model_fallback(e),
}
"""Event-type → render-method binding.

Silent rendering: `thread.exchange_started` and
`thread.exchange_completed` are listed because the per-turn
thinking-indicator gate needs their event ids to bound the gate;
their handlers update state but emit no UI. Other silent events
(`user.message`, `config.set`, `memory.*`, etc.) are not listed;
`Renderer._dispatch` treats absent entries as no-op. Adding a new
branch means adding one line here."""


__all__ = ["Renderer"]

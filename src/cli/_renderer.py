"""Event renderer for ``kolsch chat`` — Phase 2.1 Step 24c.

The chat is a **pure consumer** of
:meth:`engine.AgentEngine.execute_stream`. Every visible block in
the REPL corresponds to one event emitted by the engine; the
renderer adds no semantic layer of its own. This is the load-bearing
architectural property enforced by AGENTS.md's CHAT-R9 hard rule.

The mapping from event type to render action comes verbatim from
phase2.1-chat-specs.md Part 3:

================================  =============================================
Event type                        Action
================================  =============================================
``thread.exchange_started``       silent boundary (no UI)
``user.message``                  silent — prompt_toolkit's terminal echo of
                                  the submitted prompt line is the canonical
                                  scrollback "You: <content>"; the event
                                  confirms it for the JSONL only.
``model.request``                 print "<AgentName> is thinking..." indicator
``model.response`` (non-stream)   render the final content (markdown)
``tool.called``                   print tool-call panel header "▶ running"
``tool.result``                   fill in the panel; truncate >20 lines
``tool.failed``                   render the panel in red
``agent.message``                 if text differs from what model.response
                                  rendered, re-print; otherwise no-op
``thread.exchange_completed``     silent (input prompt unlocks via the loop)
``thread.exchange_failed``        print a system notice; loop continues
``model.fallback``                italic notice "Falling back to <model>"
``config.set`` / ``memory.*``     silent (diagnostic-only in the JSONL)
================================  =============================================

Live region scope (patch §2.3, §2.4): a Rich ``Live`` region is
permitted ONLY around the in-progress agent block during streaming.
The FakeFramework + EchoLLM that ship with Phase 1 emit
``model.response`` as a single non-streamed event; for 24c there is
no token-by-token streaming to wrap. Future steps that wire a
streaming connector will add a ``Live`` ctx around the
``model.response`` token-append loop — the exit-before-next-static-
block rule (§2.3) is what keeps native scrolling intact (§2.4).

**Replay-safety.** This module is a pure async consumer of an event
iterator. It does not depend on wall-clock or fresh runtime state.
The Step 24e replay path feeds it events read from the JSONL; the
output for a replayed session is identical to what the original
chat rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TextIO

from datetime import datetime

from rich.console import Console
from rich.text import Text

from schemas import EventType


# ---------------------------------------------------------------------------
# Capsule primitive — qol-chat-capsule-layout.md
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
}


def _now_hhmmss() -> str:
    return datetime.now().strftime("%H:%M:%S")  # noqa: DTZ005 — local time is right for UX


def _render_agent_capsule(console: Console, agent_name: str, text: str) -> None:
    """Render an agent message as a sidebar capsule with `┃ `
    code-block styling for ``` fenced sections per
    qol-chat-capsule-layout.md §Code block.

    Splits the text on triple-backtick boundaries. Prose lines get
    the outer agent bar `│`; code-block lines get the outer bar
    PLUS the inner `┃` in cyan. The language label appears on its
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
    """Print one capsule per qol-chat-capsule-layout.md spec.

    ``source`` is the label (``"tool"`` / ``"permission"`` / etc.);
    ``color`` overrides the palette lookup when set (e.g. the dim
    failed-tool variant).
    ``header_extra`` lets callers append duration info (`→ HH:MM:SS (1.8s)`).
    ``body_lines`` are the content lines; each gets the bar prefix.
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
# Rendering knobs (patch §2.2 / §2.3)
# ---------------------------------------------------------------------------


_TOOL_RESULT_MAX_LINES = 20
"""Tool-result panel truncation threshold per patch §2.3.

Long results (>20 lines) collapse to the first 20 lines plus a
``[N more lines, /show <id> for full]`` suffix. The full content
stays in the JSONL — the truncation is display-only."""


_THINKING_INDICATOR = "  …"
"""Subtle "is thinking" indicator printed once PER EXCHANGE on the
first ``model.request``.

Per patch §2.2: "Spinner + 'Thinking...' in the agent block's
spot." 24c uses a one-line text indicator (not a spinning
animation) — the FakeFramework + EchoLLM resolve instantly so an
animation would only ever flash. When real streaming connectors
land in a later phase the indicator becomes the spinner inside a
Rich ``Live`` ctx.

bug-crewai-stdout-leak-investigation Finding F5 / Step G2: in
multi-turn tool-call exchanges the framework loops
``model.request → tool.called → tool.result → model.request``
until a text response. Pre-fix the indicator fired on every
``model.request``, producing N ellipses between every tool-call
panel — visually noisy and easily misread as a CrewAI leak. The
renderer now tracks the current exchange's id (set on
``thread.exchange_started``) and suppresses subsequent indicator
fires within the same exchange; the next ``thread.exchange_started``
(or ``thread.exchange_completed`` / ``thread.exchange_failed``)
resets the gate. Per E-BUG-CREWAI-006 D10."""


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class Renderer:
    """Pure consumer of an engine event stream.

    Construct one per exchange (``thread.exchange_started`` →
    ``thread.exchange_completed`` / ``thread.exchange_failed``);
    nothing is meant to leak across exchanges (patch §2.3 + §3
    "the renderer's only state is the current exchange's block
    list"). The runtime can re-instantiate freely; the constructor
    is cheap.

    :param stdout: where rendered text writes. The chat input
        loop passes the chat's stdout (which is `sys.stdout` in
        production and an ``io.StringIO`` in tests). Plain
        ``write``-able file object; no terminal probing — the
        Rich ``Console`` below adapts.
    :param agent_name: the agent's display name. Used as the
        prefix for the agent block ("Helper: ..."). Derived
        upstream from ``ResolvedAgentConfig.name``.
    :param console: optional Rich ``Console`` injected by tests
        so they can capture output without poking at terminal
        autodetection. Production code uses the default
        (``Console(file=stdout)``).
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
        # try to probe — the §2.4 architectural commitment is that we
        # stay out of terminal-capability negotiation.
        self._console = console or Console(
            file=stdout,
            force_terminal=False,
            highlight=False,
        )
        # Set by ``model.response`` so ``agent.message`` can decide
        # whether to re-render (patch §3 "if differs from streamed
        # tokens, update final").
        self._last_agent_text: str | None = None
        # G2 — per-exchange thinking-indicator gate. The current
        # exchange id is set on ``thread.exchange_started``; the
        # first ``model.request`` of an exchange emits the indicator
        # and records the exchange id; subsequent ``model.request``s
        # (tool-loop iterations) see the recorded id and skip the
        # print. ``thread.exchange_completed`` /
        # ``thread.exchange_failed`` reset both fields so the next
        # exchange's first ``model.request`` fires again.
        self._current_exchange_id: str | None = None
        self._last_indicated_exchange_id: str | None = None
        # qol-chat-capsule-layout.md §Tool capsule — tool.called is
        # silent; tool.result / tool.failed render the completed
        # capsule. We carry the call's arguments from tool.called to
        # the matching tool.result/tool.failed via this map so the
        # rendered signature shows args (the result event itself
        # doesn't carry them).
        self._tool_call_args: dict[str, dict[str, object]] = {}

    # -----------------------------------------------------------------------
    # The single public method
    # -----------------------------------------------------------------------

    async def render(self, event_stream: AsyncIterator[Event]) -> None:
        """Consume ``event_stream`` until it ends; render each event.

        Patch §3 + Part 10 item 1: per-event dispatch is a single
        ``if/elif`` ladder over the (small) event vocabulary. Each
        branch does exactly one rendering action. Unknown event
        types are silently ignored — this is intentional so a
        future engine event that lands before its renderer support
        does NOT crash the chat. (The CHAT-R9 hard rule prevents
        the inverse — the renderer can't invent a UI element for
        an event the engine never emits.)
        """
        async for event in event_stream:
            await self._dispatch(event)

    # -----------------------------------------------------------------------
    # Per-event branches — one method per event type per patch Part 3
    # -----------------------------------------------------------------------

    async def _dispatch(self, event: Event) -> None:
        # Dispatch table — one entry per renderable event. Silent
        # event types (thread.exchange_started, user.message,
        # thread.exchange_completed, config.set, memory.*) are not
        # listed; the loop below treats absent entries as silent.
        # Keeping the table here (rather than a long if/elif ladder)
        # keeps the per-event return count under ruff's PLR0911
        # threshold and makes the patch §3 mapping a one-line edit.
        handler = _DISPATCH.get(event.type)
        if handler is None:
            return
        handler(self, event)

    def _track_exchange_started(self, event: Event) -> None:
        """G2 — record the new exchange's id so the per-exchange
        thinking-indicator gate (in :meth:`_render_model_request`)
        can fire exactly once per exchange.

        Silent on the wire — patch §3 row 1 keeps
        ``thread.exchange_started`` UI-less; this handler only
        updates state."""
        self._current_exchange_id = str(event.event_id)
        self._last_indicated_exchange_id = None

    def _render_model_request(self, event: Event) -> None:
        """G2 — emit :data:`_THINKING_INDICATOR` ONCE per exchange,
        not once per ``model.request``. In multi-turn tool-call
        exchanges the framework loops
        ``model.request → tool.called → tool.result → model.request``
        until a text response; printing the indicator on every
        ``model.request`` produced N indicators between tool panels
        (the Finding F5 / E-BUG-CREWAI-005 symptom). The current
        exchange id is set on ``thread.exchange_started``; we emit
        on the first ``model.request`` where the current id differs
        from the last-indicated id, then record the current id so
        subsequent fires within the same exchange skip the print."""
        _ = event  # exchange-id flows through self, not the event
        if self._current_exchange_id is None:
            # Defensive — event stream started mid-exchange (no
            # ``thread.exchange_started`` seen). Render once so the
            # indicator isn't silently lost; cap further fires until
            # a real ``thread.exchange_started`` resets state.
            self._console.print(Text(_THINKING_INDICATOR, style="dim italic"))
            self._last_indicated_exchange_id = "<orphan>"
            return
        if self._current_exchange_id == self._last_indicated_exchange_id:
            return  # already indicated for this exchange — suppress
        self._console.print(Text(_THINKING_INDICATOR, style="dim italic"))
        self._last_indicated_exchange_id = self._current_exchange_id

    def _reset_indicator_gate(self, event: Event) -> None:
        """G2 — clear the per-exchange indicator gate on
        ``thread.exchange_completed`` so the NEXT exchange's first
        ``model.request`` fires the indicator again.

        ``thread.exchange_failed`` shares this reset logic but is
        wired separately (its handler also renders a failure
        notice); see :meth:`_render_exchange_failed`."""
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
            return  # patch §3 row 9 — non-divergent, no re-render
        # Diverges from what model.response rendered — re-print.
        _render_agent_capsule(self._console, self._agent_name, text)
        self._last_agent_text = text

    def _render_tool_called(self, event: Event) -> None:
        """``tool.called`` is silent per qol-chat-capsule-layout.md
        §Tool capsule (one-capsule-per-tool-call discipline).

        Captures the call's arguments by tool_call_id so the
        matching ``tool.result`` / ``tool.failed`` capsule can
        render the full ``<tool>(<args>)`` signature line —
        result events themselves don't carry the arguments.

        Rich `Live`-based in-place evolution is deferred (the
        QoL's escalation path #1 — static-capsule-only).
        """
        tool_call_id = str(event.data.get("tool_call_id", ""))
        if tool_call_id:
            self._tool_call_args[tool_call_id] = (
                event.data.get("arguments") or {}
            )

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
        """Tool-failed capsule — dim cyan variant per spec.

        The header gains a `failed` suffix; the whole bar dims so the
        user reads "transactional record, this didn't succeed."

        qol-permission-system-human-readable.md Step 3 — authz
        denials get a DISTINCT capsule (orange ``permission``
        source) showing the matched rule and the exact ``/perm``
        command to undo. Discriminator: the error code is
        ``tool.unauthorized`` AND the event carries a non-empty
        ``recovery_hint``. Generic tool failures (unrelated to
        authz) still use the dim cyan ``tool`` capsule.
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
        """Render the authz-denied capsule per
        qol-permission-system-human-readable.md Step 3 + the layout
        spec at qol-chat-capsule-layout.md §Permission capsule.

        Visual contract — distinct from the generic tool-failure
        capsule because the user must act:

            │ permission · HH:MM:SS                          [dark_orange3]
            │ denied: <tool_ref>(<arg-summary>)
            │   matched rule:  what=<w>  pattern=<p>  effect=deny
            │
            │ to allow:  /perm allow <ref> <pattern>

        The matched rule's ``what`` / ``pattern`` are pulled from
        the wrapped ``ToolUnauthorized`` detail. When a rule
        carries the legacy regex shape (``who`` present), we
        surface the legacy ``what`` regex instead — the user can
        still convert via ``/perm`` once they understand which
        rule fired.
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

        Per patch §3 + §6.3: an exchange-failed event in the middle
        of a chat does NOT end the chat; the user sees a notice
        and can keep going (with ``/model`` or by re-prompting).

        Patch §6.1: a Ctrl-C cancellation specifically renders the
        in-progress agent block in muted styling with a
        ``[cancelled]`` suffix (NOT the generic yellow exchange-
        failed line) so the user sees their partial reply, not a
        generic "exchange failed" string. The discriminator is
        the error's code: ``engine.cancelled`` triggers the muted
        path; anything else stays on the generic notice.
        """
        error = event.data.get("error") or {}
        code = _error_code(error)
        message = str(error.get("message", ""))
        # G2 — failed exchange ends the per-exchange indicator gate
        # too; the next user message starts a fresh exchange.
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
        """Patch §6.1 — Ctrl-C suffix on the partial agent block.

        ``_last_agent_text`` is set by the most recent
        ``model.response`` render; if the engine cancelled
        before any tokens landed, there's no partial block —
        we just print the muted "[cancelled]" sentinel so the
        user sees something explicit.
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
        """Italic notice per patch §3 row 12 / §6.3 row 1.

        Cross-references Phase 1.3's ``model.fallback`` event
        shape: ``{from: {provider, model}, to: {provider, model},
        reason, ...}``.
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
        """Pull the display string out of a ``content`` dict.

        Both ``agent.message`` and ``model.response`` carry their
        rendered text under ``content.text`` per the Phase 1
        schemas (``AgentMessageData``, ``ModelResponseData``).
        Multi-modal payloads grow this surface in a later phase;
        the renderer defensively stringifies an unexpected
        non-string ``text`` value.
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

    qol-permission-system-human-readable.md Step 3 — both schema
    shapes render cleanly:

    - **New shape** (``who is None``): ``what=<w>  pattern=<p>
      effect=<e>``.
    - **Legacy shape** (``who`` present): ``what=<w>
      effect=<e>``. The legacy rule's ``what`` is a regex that
      matched either action or target; surface it verbatim so
      the user can recognise which rule fired before deciding
      whether to migrate it.

    Returns ``""`` when the matched-rule dict is empty (default
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

    The schema's canonical key is ``type`` per
    ``schemas.error.Error.type``; tests that build crafted
    events sometimes use ``code`` (mirroring the PlatformError
    catalog's user-facing field name) — handle both so the
    renderer is honest about whatever the producer emitted.
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
"""Error codes that map to the patch §6.1 muted-suffix render path.

Phase 1's engine uses ``exchange.cancelled`` for ``CancelToken``-
triggered teardown; the chat spec uses the prose ``engine.cancelled``.
Both surface the same UX in the renderer."""


def _is_cancellation_code(code: str) -> bool:
    return code in _CANCELLATION_CODES or "cancel" in code.lower()


_PANEL_HEADER_ARG_MAX_LEN = 60
"""Truncation width for argument values in the tool-call panel
header so it stays one line. The full arguments live in the JSONL
under ``tool.called.data.arguments``."""


def _format_args(args: dict[str, object]) -> str:
    """Render a tool's arguments as ``key=val, key=val`` for the panel header.

    Strings get repr'd so the user sees the quotes (helps
    distinguish an empty string from a missing argument); other
    types str() through. Long values truncate to
    :data:`_PANEL_HEADER_ARG_MAX_LEN` chars with ``...`` so the
    panel header stays one line.
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
    """Render a ``tool.result.data.result`` payload as displayable text.

    Most builtins (``read_file``, ``bash``) return their primary
    output under a recognisable key (``content``, ``stdout``).
    When neither is present, the payload stringifies — the user
    sees a JSON-ish dump rather than a cryptic blank.
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
    """Truncate a tool result to ``max_lines``; return ``(text, extras)``.

    ``extras`` is the count of lines that were dropped; the caller
    uses it to render the ``[N more lines, ...]`` suffix per patch
    §2.3.
    """
    lines = body.splitlines()
    if len(lines) <= max_lines:
        return body, 0
    truncated = "\n".join(lines[:max_lines])
    return truncated, len(lines) - max_lines


_DISPATCH: dict[EventType, Callable[[Renderer, Event], None]] = {
    # G2 — silent on the wire, but updates per-exchange indicator state.
    EventType.THREAD_EXCHANGE_STARTED: lambda r, e: r._track_exchange_started(e),
    EventType.MODEL_REQUEST: lambda r, e: r._render_model_request(e),
    EventType.MODEL_RESPONSE: lambda r, e: r._render_model_response(e),
    EventType.TOOL_CALLED: lambda r, e: r._render_tool_called(e),
    EventType.TOOL_RESULT: lambda r, e: r._render_tool_result(e),
    EventType.TOOL_FAILED: lambda r, e: r._render_tool_failed(e),
    EventType.AGENT_MESSAGE: lambda r, e: r._render_agent_message(e),
    # G2 — silent on the wire; resets the per-exchange indicator gate.
    EventType.THREAD_EXCHANGE_COMPLETED: lambda r, e: r._reset_indicator_gate(e),
    EventType.THREAD_EXCHANGE_FAILED: lambda r, e: r._render_exchange_failed(e),
    EventType.MODEL_FALLBACK: lambda r, e: r._render_model_fallback(e),
}
"""Event-type → render-method binding. Patch §3 mapping.

Silent rendering: ``thread.exchange_started`` and
``thread.exchange_completed`` are listed because G2's per-exchange
thinking-indicator gate needs their event ids to bound the gate;
their handlers update state but emit no UI. Other silent events
(``user.message``, ``config.set``, ``memory.*``, etc.) are not
listed; :meth:`Renderer._dispatch` treats absent entries as
no-op. Adding a new branch means adding ONE line here."""


__all__ = ["Renderer"]

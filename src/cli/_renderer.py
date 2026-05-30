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
``user.message``                  echo "You: <content>" (the chat already
                                  printed the user's input; the event confirms
                                  it — the renderer does NOT re-print to avoid
                                  duplicating)
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

from typing import TYPE_CHECKING, TextIO

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from schemas import EventType

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
"""Subtle "is thinking" indicator printed once on ``model.request``.

Per patch §2.2: "Spinner + 'Thinking...' in the agent block's
spot." 24c uses a one-line text indicator (not a spinning
animation) — the FakeFramework + EchoLLM resolve instantly so an
animation would only ever flash. When real streaming connectors
land in a later phase the indicator becomes the spinner inside a
Rich ``Live`` ctx."""


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

    def _render_model_request(self) -> None:
        self._console.print(
            Text(_THINKING_INDICATOR, style="dim italic"),
        )

    def _render_model_response(self, event: Event) -> None:
        content = event.data.get("content") or {}
        text = self._extract_text(content)
        if not text:
            return
        self._console.print(Text(f"{self._agent_name}: ", style="bold"), end="")
        self._console.print(Markdown(text))
        self._console.print()  # trailing blank for readability
        self._last_agent_text = text

    def _render_agent_message(self, event: Event) -> None:
        content = event.data.get("content") or {}
        text = self._extract_text(content)
        if not text:
            return
        if text == self._last_agent_text:
            return  # patch §3 row 9 — non-divergent, no re-render
        # Diverges from what model.response rendered — re-print.
        self._console.print(Text(f"{self._agent_name}: ", style="bold"), end="")
        self._console.print(Markdown(text))
        self._console.print()
        self._last_agent_text = text

    def _render_tool_called(self, event: Event) -> None:
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        args = event.data.get("arguments") or {}
        header = f"▶ {tool_name}({_format_args(args)})"
        self._console.print(
            Panel(
                Text("running...", style="dim"),
                title=header,
                title_align="left",
                border_style="cyan",
                padding=(0, 1),
            ),
        )

    def _render_tool_result(self, event: Event) -> None:
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        result = event.data.get("result") or {}
        body, truncated_lines = _truncate_tool_body(
            _format_tool_result(result),
            max_lines=_TOOL_RESULT_MAX_LINES,
        )
        suffix = ""
        if truncated_lines > 0:
            tool_call_id = str(event.data.get("tool_call_id", "?"))
            suffix = f"\n[{truncated_lines} more lines, /show {tool_call_id} for full]"
        self._console.print(
            Panel(
                Text(body + suffix),
                title=f"▼ {tool_name}",
                title_align="left",
                border_style="green",
                padding=(0, 1),
            ),
        )

    def _render_tool_failed(self, event: Event) -> None:
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        error = event.data.get("error") or {}
        code = str(error.get("code", "<unknown>"))
        message = str(error.get("message", ""))
        self._console.print(
            Panel(
                Text(f"{code}: {message}", style="red"),
                title=f"✗ {tool_name}",
                title_align="left",
                border_style="red",
                padding=(0, 1),
            ),
        )

    def _render_exchange_failed(self, event: Event) -> None:
        """Print a one-line system notice and unlock the input loop.

        Per patch §3 + §6.3: an exchange-failed event in the middle
        of a chat does NOT end the chat; the user sees a notice and
        can keep going (with ``/model`` or by re-prompting).
        """
        error = event.data.get("error") or {}
        code = str(error.get("code", "<unknown>"))
        message = str(error.get("message", ""))
        self._console.print(
            Text(
                f"[exchange failed — {code}: {message}]",
                style="yellow",
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
    EventType.MODEL_REQUEST: lambda r, _e: r._render_model_request(),
    EventType.MODEL_RESPONSE: lambda r, e: r._render_model_response(e),
    EventType.TOOL_CALLED: lambda r, e: r._render_tool_called(e),
    EventType.TOOL_RESULT: lambda r, e: r._render_tool_result(e),
    EventType.TOOL_FAILED: lambda r, e: r._render_tool_failed(e),
    EventType.AGENT_MESSAGE: lambda r, e: r._render_agent_message(e),
    EventType.THREAD_EXCHANGE_FAILED: lambda r, e: r._render_exchange_failed(e),
    EventType.MODEL_FALLBACK: lambda r, e: r._render_model_fallback(e),
}
"""Event-type → render-method binding. Patch §3 mapping.

Silent events (``thread.exchange_started``, ``user.message``,
``thread.exchange_completed``, ``config.set``, ``memory.*``,
``model.fallback`` once Phase 1.3 settles, etc.) are not in this
table; :meth:`Renderer._dispatch` treats absent entries as
no-op. Adding a new branch means adding ONE line here."""


__all__ = ["Renderer"]

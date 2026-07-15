"""`AssistantProse` widget — the assistant's streaming markdown response.

Design (see `process/spec-textual-chat-ui.md` → Technical Context →
widget subsection for `AssistantProse`):

- Subclass Textual's `Markdown` widget.
- No border. Foreground colour `$assistant-fg` via `DEFAULT_CSS`.
- Public API:
    * `async with widget.stream() as stream: ...` — context manager
      yielding a `MarkdownStream` for token-by-token append during a
      streaming model response. `Markdown.get_stream` returns a
      `MarkdownStream` started; on exit we call `await stream.stop()`.
    * `async def append(text: str)` — one-shot append. Delegates to
      the underlying `Markdown.append`. Non-streaming providers use
      this on `MODEL_RESPONSE`.
- `NO_COLOR` support: fenced code blocks render as plain dim
  monospace. `MarkdownFence` normally runs Pygments via
  `textual.widgets._markdown.highlight`; we suppress it by
  substituting a `_PlainFence` subclass in the `BLOCKS` mapping when
  `os.environ["NO_COLOR"]` is set. `_PlainFence.highlight` returns a
  `Content` with no spans — the raw code text, no colour.

This widget is framework-aligned (rule 1), self-contained (rule 2),
and delegates streaming + rendering to Textual (rule 3). No custom
markdown parser, no custom stream loop, no per-character styling.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

from textual import events
from textual.content import Content
from textual.widgets import Markdown, Static
from textual.widgets._markdown import MarkdownFence, MarkdownStream

_ASSISTANT_FALLBACK_NAME = "Assistant"
"""Generic identity used when the caller doesn't supply an agent name.

kolsch ships with pluggable agents; hard-coding ``Claude`` would leak
one deployment's naming into every render path. ``Assistant`` is the
generic that the spec (qol-081) locks in.
"""

_LOGGER = logging.getLogger(__name__)


def _wrap_json_if_detected(text: str) -> str:
    """Wrap ``text`` in ```json fences when it parses as JSON.

    Textual's ``Markdown`` widget reflows paragraphs — single newlines
    become spaces. A raw JSON array or object with embedded newlines
    flattens into a single-line paragraph, hiding structure. Wrapping
    in ``\\`\\`\\`json`` fences renders it as a code block, honouring
    newlines + indentation.

    Detection is strict — full-string parse via ``json.loads``. Mixed
    prose + JSON is not handled here; if the model wraps output in
    fences itself or emits prose alongside JSON, this helper returns
    the text unchanged.

    Applies to non-streaming mounts only. Streaming tokens arrive
    piecewise and can't be pre-parsed; a JSON response received via
    the streaming path continues to render mashed.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return text
    try:
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        pretty = stripped
    return f"```json\n{pretty}\n```"


ANCHOR_PATTERN = re.compile(
    r"\[([^\]]+)\]\(kolsch-anchor://workflow_card/([^)]+)\)"
)
"""feat-018 anchor markup — ``[<label>](kolsch-anchor://workflow_card/<run_id>)``.

Coordinator-locked convention: the assistant embeds the anchor as
markdown-URL-syntax; :class:`AssistantProse` swaps the match for a
focusable :class:`WorkflowAnchorButton` widget inline with the prose
flow. Enter or click on the button routes to
``EventAdapter._scroll_to_workflow_card`` via
``ChatApp.action_scroll_to_workflow_card``.
"""


class WorkflowAnchorButton(Static):
    """Focusable inline reference to a workflow card.

    Rendered by :class:`AssistantProse` when the assistant's markdown
    contains ``[<label>](kolsch-anchor://workflow_card/<run_id>)``
    markup. The button is a :class:`Static` (widget-simple, no
    layout box), ``can_focus=True`` so Tab reaches it, and both
    ``on_click`` and the Enter binding trigger the app-level
    ``action_scroll_to_workflow_card`` action that delegates to
    the ``EventAdapter``.
    """

    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;

    WorkflowAnchorButton {
        color: $tool-fg;
        text-style: bold;
        height: 1;
        width: auto;
        background: transparent;
    }
    WorkflowAnchorButton:focus {
        text-style: bold reverse;
    }
    """

    BINDINGS = [
        ("enter", "activate", "Scroll to workflow card"),
    ]

    can_focus = True

    def __init__(self, *, label: str, run_id: str) -> None:
        super().__init__(f"[▸ {label}]")
        self._run_id = run_id
        self._label = label

    @property
    def run_id(self) -> str:
        """The workflow ``run_id`` this anchor points at."""
        return self._run_id

    def on_click(self, event: events.Click) -> None:  # pragma: no cover - via Pilot
        _ = event
        self._activate()

    def action_activate(self) -> None:
        """Enter binding — scroll transcript to the workflow card."""
        self._activate()

    def _activate(self) -> None:
        app = self.app
        action = getattr(app, "action_scroll_to_workflow_card", None)
        if action is None:
            _LOGGER.warning(
                "assistant_prose: app has no action_scroll_to_workflow_card; "
                "anchor click for run_id=%r ignored",
                self._run_id,
            )
            return
        action(self._run_id)


class _PlainFence(MarkdownFence):
    """Fenced-code block that suppresses Pygments under `NO_COLOR`.

    `Markdown.BLOCKS` maps `"fence"` / `"code_block"` to `MarkdownFence`,
    which calls the module-level `highlight()` (Pygments-driven) inside
    its `highlight` classmethod. We swap the mapping to this subclass
    under `NO_COLOR`; `highlight` here returns a plain `Content` with
    no style spans, so the fenced block renders as monospaced text
    with the widget's ambient `text-style: dim` — no invented colour.
    """

    @classmethod
    def highlight(cls, code: str, language: str) -> Content:
        # No spans → no colour. Normalise line endings the way the
        # parent does (see `_markdown.highlight`) so line wrapping /
        # width calculations match the coloured variant.
        return Content("\n".join(code.splitlines()))


class AssistantProse(Markdown):
    """Assistant response widget — streaming markdown, rounded frame.

    Reactive attributes: none of our own. Reactive state is fully
    owned by the underlying `Markdown` widget's internal document.

    Auto-wraps raw JSON in ```json fences (non-streaming path only)
    so agent responses that return structured output don't collapse
    into a single-line paragraph under Markdown reflow. See
    :func:`_wrap_json_if_detected`.

    Identity header (qol-081): when the caller passes a ``timestamp``,
    the widget renders ``<agent-name> · HH:MM`` in the top rail so the
    assistant carries the same identity chrome as
    :class:`~cli.chat.widgets.user_chatbox.UserChatbox`. The agent name
    is bold in ``$assistant-fg``; the time is dim in ``$hint``. Callers
    that don't pass a timestamp (nested workflow-row prose, ad-hoc
    literal markdown) keep the bare rail.
    """

    def __init__(
        self,
        markdown: str = "",
        *,
        agent_name: str | None = None,
        timestamp: datetime | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(_wrap_json_if_detected(markdown), **kwargs)  # type: ignore[arg-type]
        if timestamp is not None:
            self.border_title = self._build_identity_header(agent_name, timestamp)

    @staticmethod
    def _build_identity_header(
        agent_name: str | None, timestamp: datetime
    ) -> str:
        """Format the ``<agent> · HH:MM`` border title.

        Returns a plain string; per-segment styling isn't possible via
        ``border_title`` because Rich's style tokens don't resolve
        Textual theme variables (``$assistant-fg``, ``$hint``). The
        title picks up uniform ``border-title-color`` / ``-style`` from
        this widget's ``DEFAULT_CSS`` (bold in ``$assistant-fg``) —
        matching :class:`UserChatbox`'s uniform-title pattern. ``HH:MM``
        is 24-hour, no seconds.
        """
        name = agent_name or _ASSISTANT_FALLBACK_NAME
        return f"{name} · {timestamp:%H:%M}"

    # Fallback variable defaults so the widget mounts under Pilot
    # without theme.tcss loaded (per the Design Principles' modularity
    # rule: a widget must render standalone). When theme.tcss /
    # mono.tcss ARE loaded by `ChatApp`, their values take precedence
    # because they come later in the cascade.
    DEFAULT_CSS = """
    $assistant-fg: #b0c0d5;

    /* Round border in `$assistant-fg` mirrors the `UserChatbox`
     * green box — same silhouette so the transcript reads as a
     * conversation of stacked turn boxes. Padding sits inside the
     * border, giving the markdown a one-column gutter from the
     * frame. `margin: 0 0 1 0` gives a one-row breather between
     * turns. */
    AssistantProse {
        border: round $assistant-fg;
        border-title-color: $assistant-fg;
        border-title-style: bold;
        padding: 0 1;
        margin: 0 0 1 0;
        color: $assistant-fg;
        background: transparent;
        height: auto;
    }
    """

    def get_block_class(self, block_name: str) -> type:
        """Return the widget class that renders a given markdown block.

        Under `NO_COLOR`, substitute `_PlainFence` for the Pygments-
        highlighted `MarkdownFence` so fenced code blocks render as
        plain monospace with no invented colour. Evaluated per call
        so tests can toggle `NO_COLOR` between runs (Pilot tests are
        function-scoped and may monkeypatch the env).
        """
        if block_name in ("fence", "code_block") and os.environ.get(
            "NO_COLOR"
        ) is not None:
            return _PlainFence
        return Markdown.BLOCKS[block_name]

    @asynccontextmanager
    async def stream(self) -> AsyncIterator[MarkdownStream]:
        """Yield a `MarkdownStream` for token-by-token appends.

        Usage in the event adapter:

            async with prose.stream() as stream:
                async for delta in deltas:
                    await stream.write(delta.text_delta)

        The stream is started by `Markdown.get_stream` and stopped on
        context exit (or on exception). Textual's `MarkdownStream`
        handles the debouncing / batching so that N deltas per second
        collapse to a manageable number of widget refreshes.
        """
        stream = Markdown.get_stream(self)
        try:
            yield stream
        finally:
            await stream.stop()

    async def append(self, text: str) -> None:  # type: ignore[override]
        """Append `text` to the underlying markdown document.

        Thin async wrapper over `Markdown.append` — the base class
        method returns an `AwaitComplete` that we await here so
        callers get a plain coroutine. Non-streaming providers use
        this on `MODEL_RESPONSE`; streaming providers should use
        `stream()` instead so writes are debounced.

        feat-018: when `text` contains
        ``[<label>](kolsch-anchor://workflow_card/<run_id>)`` markup,
        the anchor spans are split out and mounted as
        :class:`WorkflowAnchorButton` widgets inline with the prose
        blocks. The surrounding text is appended to the markdown
        stream as normal.

        Auto-wraps raw JSON in ```json fences before the anchor scan
        so structured agent responses render as code blocks. Anchor
        markdown is a URL-syntax link, incompatible with a top-level
        JSON parse — the JSON detection returns unchanged for anchor-
        carrying prose.
        """
        text = _wrap_json_if_detected(text)
        if ANCHOR_PATTERN.search(text) is None:
            await Markdown.append(self, text)
            return
        # Split text into (prose, anchor?, prose, anchor?, ...) chunks.
        pieces = self._split_anchors(text)
        for kind, prose_text, label, run_id in pieces:
            if kind == "prose":
                if prose_text:
                    await Markdown.append(self, prose_text)
                continue
            # kind == "anchor"
            button = WorkflowAnchorButton(label=label, run_id=run_id)
            try:
                await self.mount(button)
            except Exception:  # noqa: BLE001 — mount failure logged
                _LOGGER.exception(
                    "assistant_prose: WorkflowAnchorButton mount "
                    "failed for run_id=%r", run_id,
                )

    @staticmethod
    def _split_anchors(text: str) -> list[tuple[str, str, str, str]]:
        """Split ``text`` around anchor matches into ordered chunks.

        Returns a list of ``(kind, prose_text, anchor_label,
        anchor_run_id)`` tuples where exactly one of ``prose_text`` or
        the anchor pair is meaningful per row:

        - ``kind="prose"`` — ``prose_text`` is the segment; label/run_id
          are empty strings.
        - ``kind="anchor"`` — ``label`` and ``run_id`` are the
          anchor's payload; ``prose_text`` is the empty string.

        A flat 4-tuple keeps the return type mypy-friendly without
        needing a `Union[str, tuple[str, str]]` per element.
        """
        pieces: list[tuple[str, str, str, str]] = []
        cursor = 0
        for match in ANCHOR_PATTERN.finditer(text):
            start, end = match.span()
            if start > cursor:
                pieces.append(("prose", text[cursor:start], "", ""))
            pieces.append(("anchor", "", match.group(1), match.group(2)))
            cursor = end
        if cursor < len(text):
            pieces.append(("prose", text[cursor:], "", ""))
        return pieces

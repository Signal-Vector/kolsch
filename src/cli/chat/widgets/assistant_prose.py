"""`AssistantProse` widget — the assistant's streaming markdown response.

Post spec-078, this widget is a :class:`Widget` container that
composes the shared :class:`~cli.chat.widgets._gutter_block.GutterBlock`
primitive (rail = ``$assistant-fg``, no leading glyph). The Markdown
document that actually renders the response lives in a private
:class:`_ProseMarkdown` child (a :class:`~textual.widgets.Markdown`
subclass) mounted inside the GutterBlock's body slot. Public API —
:meth:`append`, :meth:`stream`, :attr:`source`, the ``agent_name`` /
``timestamp`` constructor kwargs — is preserved by delegating to the
child Markdown; no ``src/`` caller needed edits.

Identity header (qol-081): when the caller passes a ``timestamp``,
the widget renders ``<agent-name> · HH:MM`` as an in-body header
Static above the markdown (bold in ``$assistant-fg``). Pre-migration
this was Textual's ``border_title`` on the round frame; post-migration
the frame is gone, so the header sits inside the GutterBlock's body
slot and picks up the rail's color. Callers that don't pass a
timestamp keep the bare rail — matches the pre-migration behavior.

NO_COLOR support: fenced code blocks render as plain dim monospace
via the :class:`_PlainFence` substitute — the private Markdown
subclass overrides :meth:`~textual.widgets.Markdown.get_block_class`
so the swap happens whenever ``NO_COLOR`` is set in the environment.

feat-018 anchor markup — ``[<label>](kolsch-anchor://workflow_card/<run_id>)``
— continues to route through :meth:`append` and mount focusable
:class:`WorkflowAnchorButton` widgets inline with the prose.

This widget is framework-aligned (rule 1), self-contained (rule 2),
and delegates streaming + rendering to Textual (rule 3). No custom
markdown parser, no custom stream loop, no per-character styling.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from textual.containers import Vertical
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Markdown, Static
from textual.widgets._markdown import MarkdownFence, MarkdownStream

from cli.chat.widgets._gutter_block import GutterBlock

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult

_ASSISTANT_FALLBACK_NAME = "Assistant"
"""Generic identity used when the caller doesn't supply an agent name.

kolsch ships with pluggable agents; hard-coding ``Claude`` would leak
one deployment's naming into every render path. ``Assistant`` is the
generic that the spec (qol-081) locks in.
"""

_LOGGER = logging.getLogger(__name__)


def _wrap_json_if_detected(text: str) -> str:
    r"""Wrap ``text`` in ```json fences when it parses as JSON.

    Textual's ``Markdown`` widget reflows paragraphs — single newlines
    become spaces. A raw JSON array or object with embedded newlines
    flattens into a single-line paragraph, hiding structure. Wrapping
    in ``\`\`\`json`` fences renders it as a code block, honouring
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


ANCHOR_PATTERN = re.compile(r"\[([^\]]+)\]\(kolsch-anchor://workflow_card/([^)]+)\)")
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

    BINDINGS: ClassVar = [
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
        """Textual click handler — same effect as pressing Enter."""
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
    """Fenced-code block that suppresses Pygments under ``NO_COLOR``.

    :attr:`Markdown.BLOCKS` maps ``"fence"`` / ``"code_block"`` to
    :class:`MarkdownFence`, which calls the module-level ``highlight``
    (Pygments-driven) inside its ``highlight`` classmethod. The
    private :class:`_ProseMarkdown` swaps that mapping to this
    subclass whenever ``NO_COLOR`` is set; :meth:`highlight` here
    returns a plain :class:`Content` with no style spans, so the
    fenced block renders as monospaced text with the widget's ambient
    ``text-style: dim`` — no invented colour.
    """

    @classmethod
    def highlight(cls, code: str, language: str) -> Content:
        _ = language
        return Content("\n".join(code.splitlines()))


class _ProseMarkdown(Markdown):
    """Private Markdown child mounted inside :class:`AssistantProse`.

    Reason for the subclass: the spec-078 wrapper composes
    ``AssistantProse`` as a :class:`Widget` container that HOSTS a
    Markdown, rather than subclassing Markdown itself. The Markdown
    still needs to swap in :class:`_PlainFence` under ``NO_COLOR``,
    which requires overriding
    :meth:`~textual.widgets.Markdown.get_block_class`. So the
    NO_COLOR-aware fence swap moves onto this private subclass, and
    :class:`AssistantProse` composes an instance of it.

    Evaluation happens per call so tests can toggle ``NO_COLOR``
    between runs (Pilot tests are function-scoped and may
    monkeypatch the env).
    """

    DEFAULT_CSS = """
    $assistant-fg: #b0c0d5;

    _ProseMarkdown {
        background: transparent;
        color: $assistant-fg;
        height: auto;
        padding: 0;
        margin: 0;
    }
    """

    def get_block_class(self, block_name: str) -> type:
        """Return the widget class that renders a given markdown block."""
        if (
            block_name in ("fence", "code_block")
            and os.environ.get("NO_COLOR") is not None
        ):
            return _PlainFence
        return Markdown.BLOCKS[block_name]


class AssistantProse(Widget):
    """Assistant response widget — GutterBlock rail + streaming markdown body.

    Auto-wraps raw JSON in ```json fences (non-streaming path only)
    so agent responses that return structured output don't collapse
    into a single-line paragraph under Markdown reflow. See
    :func:`_wrap_json_if_detected`.

    Identity header (qol-081): when the caller passes a ``timestamp``,
    the widget renders ``<agent-name> · HH:MM`` as an in-body header
    Static above the markdown (bold in ``$assistant-fg``). Callers
    that don't pass a timestamp keep the bare rail.
    """

    # Fallback vars so the widget mounts under Pilot without theme.tcss.
    DEFAULT_CSS = """
    $assistant-fg: #b0c0d5;

    AssistantProse {
        height: auto;
        width: 100%;
    }
    AssistantProse .ap-header {
        color: $assistant-fg;
        text-style: bold;
        height: 1;
        width: 100%;
    }
    """

    def __init__(
        self,
        markdown: str = "",
        *,
        agent_name: str | None = None,
        timestamp: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._initial_markdown = _wrap_json_if_detected(markdown)
        self._agent_name = agent_name
        self._timestamp = timestamp
        self._markdown_kwargs = kwargs
        # Set in compose(); read via `.source` / streaming / append.
        self._markdown_widget: _ProseMarkdown | None = None

    @property
    def agent_name(self) -> str | None:
        """The agent-name kwarg supplied at construction (may be ``None``)."""
        return self._agent_name

    @property
    def timestamp(self) -> datetime | None:
        """The timestamp kwarg supplied at construction (may be ``None``)."""
        return self._timestamp

    @property
    def source(self) -> str:
        """The Markdown document's current source text.

        Before mount, returns the constructor's initial (possibly
        JSON-wrapped) text; after mount, delegates to the child
        Markdown's own :attr:`~textual.widgets.Markdown.source`
        which reflects every :meth:`append` since.
        """
        if self._markdown_widget is None:
            return self._initial_markdown
        return self._markdown_widget.source

    def compose(self) -> ComposeResult:
        """Compose the identity header (optional) + Markdown body inside GutterBlock."""
        md = _ProseMarkdown(self._initial_markdown, **self._markdown_kwargs)
        self._markdown_widget = md
        children: list[Widget] = []
        if self._timestamp is not None:
            header_text = self._build_identity_header(
                self._agent_name,
                self._timestamp,
            )
            children.append(Static(header_text, classes="ap-header", markup=False))
        children.append(md)
        yield GutterBlock(
            rail_color="$assistant-fg",
            body=Vertical(*children),
        )

    @staticmethod
    def _build_identity_header(agent_name: str | None, timestamp: datetime) -> str:
        """Format the ``<agent> · HH:MM`` identity header.

        Same shape the pre-migration widget used for its border_title
        — ``HH:MM`` (24-hour, no seconds). The header renders inside
        the body slot now instead of on a border; the string format
        is preserved so downstream consumers that keyed off the label
        text keep working.
        """
        name = agent_name or _ASSISTANT_FALLBACK_NAME
        return f"{name} · {timestamp:%H:%M}"

    @asynccontextmanager
    async def stream(self) -> AsyncIterator[MarkdownStream]:
        """Yield a :class:`MarkdownStream` for token-by-token appends.

        Usage in the event adapter:

            async with prose.stream() as stream:
                async for delta in deltas:
                    await stream.write(delta.text_delta)

        The stream is started by :meth:`Markdown.get_stream` on the
        child Markdown widget and stopped on context exit (or on
        exception). Textual's :class:`MarkdownStream` handles the
        debouncing / batching so that N deltas per second collapse to
        a manageable number of widget refreshes.

        The widget must be mounted before streaming — if
        ``_markdown_widget`` is still ``None`` (compose hasn't run),
        this raises ``RuntimeError`` to surface the mistake loudly.
        """
        if self._markdown_widget is None:
            msg = "AssistantProse.stream() called before mount"
            raise RuntimeError(msg)
        stream = Markdown.get_stream(self._markdown_widget)
        try:
            yield stream
        finally:
            await stream.stop()

    async def append(self, text: str) -> None:
        """Append ``text`` to the underlying markdown document.

        Auto-wraps raw JSON in ```json fences before the anchor scan
        so structured agent responses render as code blocks. Anchor
        markdown is a URL-syntax link, incompatible with a top-level
        JSON parse — the JSON detection returns unchanged for anchor-
        carrying prose.

        feat-018: when ``text`` contains
        ``[<label>](kolsch-anchor://workflow_card/<run_id>)`` markup,
        the anchor spans are split out and mounted as
        :class:`WorkflowAnchorButton` widgets inline with the prose
        blocks. The surrounding text is appended to the markdown
        stream as normal.

        The widget must be mounted (compose completed) before this is
        called; the streaming/append paths only run inside the app's
        event loop after mount.
        """
        if self._markdown_widget is None:
            msg = "AssistantProse.append() called before mount"
            raise RuntimeError(msg)
        text = _wrap_json_if_detected(text)
        if ANCHOR_PATTERN.search(text) is None:
            await self._markdown_widget.append(text)
            return
        pieces = self._split_anchors(text)
        for kind, prose_text, label, run_id in pieces:
            if kind == "prose":
                if prose_text:
                    await self._markdown_widget.append(prose_text)
                continue
            button = WorkflowAnchorButton(label=label, run_id=run_id)
            try:
                await self._markdown_widget.mount(button)
            except Exception:
                _LOGGER.exception(
                    "assistant_prose: WorkflowAnchorButton mount failed for run_id=%r",
                    run_id,
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
        needing a ``Union[str, tuple[str, str]]`` per element.
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

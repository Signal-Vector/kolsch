"""Workflow-event capsule renderer.

The chat side of `/workflow` and `/fork` consumes a workflow run's
event stream and renders one capsule per node-level event. This
module holds the renderer; the slash commands wire it to the
running executor.

The renderer is structured so the chat surface and tests share one
code path:

- Tests construct the renderer, hand it a list of pre-built event
  dicts, capture the rendered text, and assert on shape.
- Production constructs the renderer, hands it the JSONL tail
  iterator, awaits the executor task in parallel, and lets the
  capsules stream to the user's terminal.

The sole label source is
[`display_label`][workflows.labels.display_label]. No inline
fallback. The renderer never reaches into `description:` or
`@node(label=...)` directly — every header passes through one call
to `display_label(node_def, type_label)`.

`workflow.node.started` emits a single dim `▸ <label>…` line — not
a capsule, no border bar, no timestamp, no event_id subscript. The
matching `workflow.node.completed` / `.failed` / `.pruned` capsule
lands beneath it.

`workflow.node.failed` capsules render the failing error class's
`caller_message` ClassVar, not `str(exc)`. The engineer-targeted
single-line form stays on the CLI / JSONL surfaces.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from rich.text import Text

from cli._rich_capsule import _BAR, _CAPSULE_PALETTE, _capsule
from workflows.errors import WorkflowError
from workflows.labels import display_label

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from rich.console import Console

    from workflows.ids import WorkflowRunId
    from workflows.parser import Workflow
    from workflows.run_store import WorkflowRunStore


__all__ = ["WorkflowCapsuleRenderer"]


_RUNNING_GLYPH = "▸"  # ▸
_ELLIPSIS = "…"  # …


_EVENT_ID_SHORT_LEN = 8
"""Width of the event_id subscript shown below each capsule's header.

Operators copy the id for `/fork`. The renderer displays the short
form verbatim; `/fork` accepts the short form via prefix-matching."""


_TerminalKindResolver = "Callable[[Any], str | None]"


class WorkflowCapsuleRenderer:
    """Consumes a workflow run's event stream; emits one capsule per event.

    Capsules render at `workflow.node.completed` (success),
    `workflow.node.pruned` (muted), `workflow.node.failed`
    (`caller_message`); `workflow.node.started` emits the running
    indicator (one dim line, not a capsule). All other event kinds
    are silent today.

    The `workflow` parameter is the parsed workflow definition; the
    renderer uses `workflow.nodes[node_name]` to fetch the
    per-instance `NodeDef` (carrying any `description:`) the label
    helper needs. When `workflow` is `None` (tests that don't
    construct a full workflow), the renderer synthesizes a minimal
    stub from the event's `node_name` so `display_label` still has
    a callable surface — the cleaned-name fallback runs.

    The `type_label_for` callable resolves a node_def to its
    type-level label (`LABEL = "..."` on built-ins,
    `@node(label=...)` on authored Python).

    The `indent_depth` parameter supports sub-workflow nesting: a
    child renderer with depth N indents every line by `"  " * N` so
    child capsules visually nest under the parent's `workflow:`
    node capsule.
    """

    def __init__(
        self,
        *,
        console: Console,
        workflow: Workflow | None = None,
        type_label_for: Callable[[Any], str | None] | None = None,
        indent_depth: int = 0,
        store: WorkflowRunStore | None = None,
        child_workflow_loader: (Callable[[str], Workflow | None] | None) = None,
    ) -> None:
        self._console = console
        self._workflow = workflow
        self._type_label_for = type_label_for or (lambda _node_def: None)
        self._indent_depth = indent_depth
        self._indent = "  " * indent_depth
        # Store + loader are needed for the async tail entry point
        # (production path). Tests can pass events directly via
        # `render_events` without setting them.
        self._store = store
        self._child_workflow_loader = child_workflow_loader
        # Pending child-workflow renderer tasks. When a
        # ``workflow.node.called_workflow`` event lands we spawn a
        # child renderer (one indent deeper) tailing the child run's
        # JSONL; we keep the task here so the parent can await it
        # before its own tail terminates.
        self._child_tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    # Entry points.
    # ------------------------------------------------------------------

    def render_events(self, events: Iterable[dict[str, Any]]) -> None:
        """Walk an iterable of event dicts; emit one render per event.

        Used by unit tests (pre-built event lists) and by the async
        tail loop (one event at a time). The `render_run` async
        path is built on top of this same dispatch.
        """
        for event in events:
            self._dispatch(event)

    async def render_run(
        self,
        run_id: WorkflowRunId,
        *,
        done_event: asyncio.Event | None = None,
        poll_seconds: float = 0.05,
    ) -> None:
        """Tail the run's JSONL on the event loop; emit one render per event.

        Awaits child sub-workflow renderers; returns when a terminal
        `workflow.run.*` event lands OR `done_event` is set.

        The `done_event` parameter is the WAITING_FOR_INPUT escape
        hatch. A paused run never emits a terminal `workflow.run.*`
        event (it ends mid-stream, awaiting resume), so a naive
        tail would block forever. The slash handler sets
        `done_event` after the executor coroutine returns; the loop
        body does one final drain after seeing it set, then exits
        cleanly. Synchronous file appends in the executor's
        `emit_event` mean all events are visible on disk by the
        time the executor task completes.

        Sub-workflow nesting: when a `workflow.node.called_workflow`
        event lands, the renderer spawns a child
        `WorkflowCapsuleRenderer` at `indent_depth+1` tailing the
        child run's JSONL. The parent awaits the child task in its
        own `render_run` so child capsules complete before the
        parent's tail returns.
        """
        if self._store is None:
            msg = (
                "render_run requires a WorkflowRunStore; pass store=... at construction"
            )
            raise ValueError(msg)
        import json  # noqa: PLC0415

        from workflows.run_store import _terminal_event_kinds  # noqa: PLC0415

        terminal_kinds = _terminal_event_kinds()
        run_dir = self._store.directory(run_id)
        offset = 0
        try:
            while True:
                hit_terminal = False
                if run_dir.events_path.exists():
                    # Note: we use ``readline()`` in a while loop rather
                    # than ``for line in fh:`` because Python disables
                    # ``fh.tell()`` once the file is in iterator mode
                    # (``OSError: telling position disabled by next()
                    # call``). The polled-resume pattern requires
                    # ``tell()`` to capture the next-read offset.
                    with run_dir.events_path.open("r", encoding="utf-8") as fh:
                        fh.seek(offset)
                        while True:
                            line = fh.readline()
                            if not line:
                                break
                            stripped = line.strip()
                            if not stripped:
                                continue
                            event = json.loads(stripped)
                            await self._adispatch(event)
                            if event.get("kind") in terminal_kinds:
                                hit_terminal = True
                                break
                        offset = fh.tell()
                if hit_terminal:
                    break
                # Check the done signal AFTER the drain so the final
                # batch of events emitted just before the executor
                # returned is captured. With ``done_event`` set the
                # next iteration's file scan is the final one.
                if done_event is not None and done_event.is_set():
                    break
                await asyncio.sleep(poll_seconds)
        finally:
            for child_task in self._child_tasks:
                await child_task

    # ------------------------------------------------------------------
    # Dispatch.
    # ------------------------------------------------------------------

    def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        if kind == "workflow.node.started":
            self._render_running_indicator(event)
        elif kind == "workflow.node.completed":
            self._render_completed_capsule(event)
        elif kind == "workflow.node.pruned":
            self._render_pruned_capsule(event)
        elif kind == "workflow.node.failed":
            self._render_failed_capsule(event)
        # All other kinds are silent today (no run-level framing,
        # no tool.* capsules, no state.mutated render).
        # `workflow.node.called_workflow` (sub-workflow spawn) is
        # handled only in the async path `_adispatch` because
        # spawning a child renderer needs the event loop.

    async def _adispatch(self, event: dict[str, Any]) -> None:
        """Async-side dispatch — handles the same events plus the spawn case.

        `workflow.node.called_workflow` spawns a child renderer
        (one indent deeper) tailing the named child run.
        """
        kind = event.get("kind", "")
        if kind == "workflow.node.called_workflow":
            self._spawn_child_renderer(event)
            return
        self._dispatch(event)

    def _spawn_child_renderer(self, event: dict[str, Any]) -> None:
        """Spawn a nested renderer for a sub-workflow run.

        `workflow.node.called_workflow` carries the
        `child_workflow_run_id` of the sub-workflow's run. Tail the
        child JSONL in a fresh `WorkflowCapsuleRenderer` whose
        `indent_depth` is one deeper so child capsules nest
        visually under the parent's `workflow:` node.
        """
        if self._store is None:
            return
        payload = event.get("payload") or {}
        child_run_id_str = payload.get("child_workflow_run_id")
        if not isinstance(child_run_id_str, str) or not child_run_id_str:
            return
        from workflows.ids import WorkflowRunId  # noqa: PLC0415

        child_run_id = WorkflowRunId(child_run_id_str)
        child_workflow: Workflow | None = None
        if self._child_workflow_loader is not None:
            try:
                child_workflow = self._child_workflow_loader(child_run_id_str)
            except Exception:  # noqa: BLE001 — best-effort lookup; renderer falls back to the stub label path on any error
                child_workflow = None
        child_renderer = WorkflowCapsuleRenderer(
            console=self._console,
            workflow=child_workflow,
            type_label_for=self._type_label_for,
            indent_depth=self._indent_depth + 1,
            store=self._store,
            child_workflow_loader=self._child_workflow_loader,
        )
        self._child_tasks.append(
            asyncio.create_task(child_renderer.render_run(child_run_id))
        )

    # ------------------------------------------------------------------
    # Per-kind handlers.
    # ------------------------------------------------------------------

    def _render_running_indicator(self, event: dict[str, Any]) -> None:
        """Print one dim `▸ <label>…` line per started node.

        Not a capsule: no border bar, no timestamp, no event_id
        subscript. The line persists in scrollback unchanged; the
        matching completion / failure / pruned-target capsule lands
        beneath it.
        """
        label = self._label_for(event)
        line = f"{self._indent}{_RUNNING_GLYPH} {label}{_ELLIPSIS}"
        self._console.print(Text(line, style="dim"))

    def _render_completed_capsule(self, event: dict[str, Any]) -> None:
        """Emit the capsule at node completion.

        Header = `display_label(node_def, type_label)`. Body = the
        captured output, formatted as a single-line preview.
        Event_id subscript appears in dim text below the header.
        """
        label = self._label_for(event)
        body_lines = self._format_output_body(event)
        body_lines.extend(self._event_id_subscript(event))
        self._capsule_with_indent(
            source="workflow",
            label=label,
            body_lines=body_lines,
        )

    def _render_pruned_capsule(self, event: dict[str, Any]) -> None:
        """Render branch-not-taken edges muted with body `(not taken)`.

        Pruned nodes never emit `workflow.node.started` (pruning
        happens at edge-evaluation time, before any runner fires),
        so there is no preceding running indicator — the muted
        capsule lands standalone.
        """
        label = self._label_for(event)
        body_lines = ["(not taken)"]
        body_lines.extend(self._event_id_subscript(event))
        self._capsule_with_indent(
            source="workflow_muted",
            label=label,
            body_lines=body_lines,
        )

    def render_pause_capsule(self, node_name: str) -> None:
        """Render the inline `user_input` pause capsule.

        The chat handler calls this AFTER the executor returns
        `WorkflowRunStatus.WAITING_FOR_INPUT`. The capsule is
        rendered synchronously (the executor task has already
        returned — there's nothing to await). Shape:

        - Header = `display_label(node_def, type_label)`.
        - Body line 1 = `node_def.prompt` from the parsed workflow.
          For prompts that contain Jinja markers (`{{ ... }}`) the
          operator sees the unrendered template; the rendered form
          on `WorkflowPausedForInput.prompt` is dropped by the
          executor catch and is not on disk today.
        - Body line 2 = dim `→ <node_name>` for forensics +
          sequential-pause disambiguation. Always rendered.
        - No `(awaiting your response)` placeholder — the chat's
          `:> ` input prompt is the cue.
        """
        node_def = self._lookup_node_def(node_name)
        label = (
            display_label(node_def, self._type_label_for(node_def))
            if node_def is not None
            else display_label(_NodeNameStub(node_name))
        )
        prompt_text = (
            str(getattr(node_def, "prompt", "")) if node_def is not None else ""
        )
        body_lines: list[str] = []
        if prompt_text:
            body_lines.append(prompt_text)
        body_lines.append(f"→ {node_name}")
        # Use the workflow_paused palette entry; the dim style on the
        # node_name line is applied via Rich Text in the indented path
        # below (the non-indent path delegates to ``_capsule`` which
        # doesn't support per-line styling — accept that for the unin-
        # dented surface; the dim arrow line still renders legibly).
        self._capsule_with_indent(
            source="workflow_paused",
            label=label,
            body_lines=body_lines,
        )

    def _render_failed_capsule(self, event: dict[str, Any]) -> None:
        """Render the failing error class's `caller_message` ClassVar.

        Looks up the class named in the event payload's
        `error_class` field on `workflows.errors`; falls back to
        `WorkflowError.caller_message` (the generic blurb) when
        the class is missing or doesn't carry the ClassVar.
        `str(exc)` never reaches chat.
        """
        label = self._label_for(event)
        payload = event.get("payload") or {}
        body_lines = [self._caller_message_for(payload)]
        body_lines.extend(self._event_id_subscript(event))
        self._capsule_with_indent(
            source="workflow_failed",
            label=label,
            body_lines=body_lines,
        )

    # ------------------------------------------------------------------
    # Helpers.
    # ------------------------------------------------------------------

    def _label_for(self, event: dict[str, Any]) -> str:
        """Resolve the node's display label.

        Calls [`display_label`][workflows.labels.display_label] —
        the sole label source. Looks up the `NodeDef` by `node_name`
        from the parsed `workflow`. When the workflow is unavailable
        (tests) or the node_name is missing from `Workflow.nodes`,
        synthesizes a stub object so the helper's cleaned-name
        fallback runs.
        """
        node_name = str(event.get("node_name") or "")
        node_def = self._lookup_node_def(node_name)
        type_label = self._type_label_for(node_def) if node_def is not None else None
        if node_def is not None:
            return display_label(node_def, type_label)
        # Stub for the cleaned-name fallback path. The helper consults
        # ``getattr(node, "description", None)`` (will be None on the
        # stub) and ``getattr(node, "__name__", None)`` (falls through
        # to ``type(node).__name__``). Wrap the node_name in a class
        # whose __name__ matches so the cleaned-name path is honest.
        return display_label(_NodeNameStub(node_name), type_label)

    def _lookup_node_def(self, node_name: str) -> Any | None:
        if self._workflow is None or not node_name:
            return None
        return self._workflow.nodes.get(node_name)

    def _format_output_body(self, event: dict[str, Any]) -> list[str]:
        """Render the completed node's captured output as body lines.

        Collapses the payload's `output` field (when present) to a
        single-line preview.
        """
        payload = event.get("payload") or {}
        output = payload.get("output")
        if output is None:
            return ["(no output)"]
        return [_single_line_preview(output)]

    def _event_id_subscript(self, event: dict[str, Any]) -> list[str]:
        """One dim subscript line carrying the event's short id.

        Operators copy this for `/fork`. The short form keeps the
        capsule compact; `/fork` accepts the short form via
        prefix-matching.
        """
        event_id = str(event.get("event_id") or "")
        if not event_id:
            return []
        short = _short_event_id(event_id)
        return [f"  {short}"]

    def _caller_message_for(self, payload: dict[str, Any]) -> str:
        """Resolve the failing class's `caller_message`.

        Falls back to `WorkflowError.caller_message` (generic blurb)
        when the named class is missing or doesn't carry the
        ClassVar. Engineer-targeted `str(exc)` never reaches chat.
        """
        from workflows import errors as workflow_errors  # noqa: PLC0415

        class_name = str(payload.get("error_class") or "")
        cls = getattr(workflow_errors, class_name, None)
        if cls is None or not isinstance(cls, type):
            cls = WorkflowError
        return str(getattr(cls, "caller_message", WorkflowError.caller_message))

    def _capsule_with_indent(
        self,
        *,
        source: str,
        label: str,
        body_lines: list[str],
    ) -> None:
        """Emit a capsule whose every line is prefixed with the renderer's indent.

        `cli._rich_capsule._capsule` writes `│ <source> · ts` as the
        header — the workflow renderer wants the NODE LABEL in the
        header slot (the identity of what just ran), so pass `label`
        as `source` and override the bar color directly from the
        `workflow*` palette entry indicated by the `source` kwarg
        here.

        `_capsule` itself is ignorant of indent depth (the existing
        chat capsules — tool, permission, agent — never nest). For
        nested sub-workflows render manually below, mirroring
        `_capsule`'s shape line-for-line with an extra space-prefix
        on every line.
        """
        color = _CAPSULE_PALETTE.get(source, "white")
        if self._indent_depth == 0:
            _capsule(
                self._console,
                source=label,
                body_lines=body_lines,
                color=color,
            )
            return
        # Manual indented render — mirrors `_capsule` exactly but
        # prefixes every line with `self._indent`.
        from datetime import datetime  # noqa: PLC0415

        ts = datetime.now().strftime("%H:%M:%S")  # noqa: DTZ005
        header = Text()
        header.append(self._indent)
        header.append(f"{_BAR} ", style=color)
        header.append(label, style=color)
        header.append(f" · {ts}", style="grey50")
        self._console.print(header)
        for line in body_lines:
            body = Text()
            body.append(self._indent)
            body.append(f"{_BAR} ", style=color)
            body.append(line)
            self._console.print(body)
        self._console.print()


class _NodeNameStub:
    """Minimal stand-in for a `NodeDef` when no parsed workflow is available.

    Carries the node_name as an instance-level `__name__` attribute
    so [`display_label`][workflows.labels.display_label]'s
    cleaned-name fallback (which consults
    `getattr(node, "__name__", None)` before falling back to
    `type(node).__name__`) returns a legible label. `description`
    is intentionally absent so the short-circuit branch doesn't
    fire.
    """

    def __init__(self, name: str) -> None:
        self.__name__ = name or "unknown"


def _short_event_id(event_id: str) -> str:
    """Truncate `ev_<uuid>` to `ev_<first_8>`.

    The `ev_` prefix is kept intact (operators visually anchor on
    it); the suffix is the first `_EVENT_ID_SHORT_LEN` characters
    of the UUID body. Non-`ev_`-prefixed ids are returned verbatim.
    """
    if not event_id.startswith("ev_"):
        return event_id
    body = event_id[len("ev_") :]
    if len(body) <= _EVENT_ID_SHORT_LEN:
        return event_id
    return f"ev_{body[:_EVENT_ID_SHORT_LEN]}"


def _single_line_preview(value: Any) -> str:
    """Collapse a value to a single-line preview string.

    Strings are stripped of newlines; everything else passes through
    `repr` for an unambiguous shape.
    """
    if isinstance(value, str):
        return value.replace("\n", " ").strip()
    return repr(value)

"""``ChatApp`` — the Textual ``App`` shell for ``kolsch chat``.

Composes the transcript, turn status, info bar, and composer;
wires the session bundle to the engine; hooks the approval
callback into the runtime's authz provider. Keeps widget state on
widgets (per Design Principle 2 — Modular) and event dispatch on
``EventAdapter`` (per Principle 1 — Framework-aligned).

Runs ``inline=True`` — see AGENTS.md CHAT-R9 and TC-CHAT-18. Never
alt-screen / full-screen.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from cli._slash import (
    COMMAND_REGISTRY,
    ChatSessionState,
    ControlSignal,
    SlashCommand,
    SlashContext,
    _resolve_command_name,
)
from cli.chat.approval import InlineApprovalCallback
from cli.chat.cancel import request_cancel
from cli.chat.event_adapter import EventAdapter
from cli.chat.keybindings import CHAT_BINDINGS
from cli.chat.service_registry import ChatServiceRegistry
from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.prompts_impl import PromptsImpl
from cli.chat.services.workflows_impl import WorkflowsImpl
from cli.chat.session import ChatSessionBundle, _build_user_trigger, replay_session
from cli.chat.widgets import (
    ApprovalBlock,
    AssistantProse,
    Composer,
    InfoBar,
    ScrollbackCue,
    SlashPopup,
    ToolCallBlock,
    TurnStatus,
    UserChatbox,
)
from cli.chat.widgets.notice import NoticeWidget
from textual.widgets import TextArea as _TextArea  # noqa: E402 — Changed msg source
from engine import ExecutionContext
from providers import RuleBasedAuthorizationProvider
from schemas.events import Event, EventType

if TYPE_CHECKING:
    pass


_LOGGER = logging.getLogger(__name__)


def register_chat_slash_commands() -> None:
    """Register ChatApp-only slash commands on the shared registry.

    Called from the ``kolsch chat`` entry point (``cli.main._cmd_chat``)
    rather than at module import time so that merely importing
    ``cli.chat`` — as any test that touches the chat package will —
    does NOT mutate the process-global ``COMMAND_REGISTRY``. Tests
    that assert the base registry's shape (e.g. ``test_chat_slash``)
    would otherwise see the extra ``/export`` entry whenever collection
    order landed ``cli.chat`` first.

    Registers:
      * ``/export`` — dumps this thread's transcript to a file. Handler
        is ``None`` because ``ChatApp._handle_slash`` intercepts by
        name and dispatches locally; the export needs transcript
        access that free-standing handlers don't have. The entry
        exists on the registry only so SlashPopup discovery, ``/help``,
        and ``_resolve_command_name`` all see it.

    Idempotent — safe to call more than once per process.
    """
    if "/export" not in COMMAND_REGISTRY:
        COMMAND_REGISTRY["/export"] = SlashCommand(
            name="/export",
            description="dump this thread's transcript to a file",
            usage=(
                "/export [<path>]   write full transcript; "
                "default path in ~/.kolsch/exports/"
            ),
            handler=None,
            state_changing=False,
        )


_THEMES_DIR = Path(__file__).parent / "themes"


def _resolve_css_path() -> str:
    """Return the theme file to load based on ``NO_COLOR``.

    Called at ``ChatApp`` construction time (before ``super().__init__``)
    so Textual picks up the right stylesheet.
    """
    theme = "mono.tcss" if os.environ.get("NO_COLOR") is not None else "theme.tcss"
    return str(_THEMES_DIR / theme)


class ChatApp(App):
    """Textual chat surface.

    Constructor:
        session: ``ChatSessionBundle`` from ``build_session``.
        engine: The agent engine handle (``runtime.engine``).
        full_history: If ``True``, replay the full JSONL. If
            ``False`` (default), replay the tail-20 turns and mount
            a ``ScrollbackCue`` at the top when truncating.
    """

    BINDINGS = CHAT_BINDINGS

    # In inline mode, Textual's Screen sizes itself to
    # `get_content_height` (sum of children) unless the Screen has an
    # explicit height. Without this, an empty transcript collapses to
    # zero rows and the whole app is just composer + infobar (~4 rows
    # tall). New widgets mounted into the empty transcript get pushed
    # straight into terminal scrollback above the app region — visible
    # to `scroll up`, invisible to the user looking at the composer.
    # See `.venv/lib/python3.13/site-packages/textual/screen.py` →
    # `Screen._get_inline_height`.
    #
    # `height: 100vh` makes the Screen fill the full terminal height
    # (capped by `app.size.height` in `_get_inline_height`). The
    # transcript takes the remaining space via `height: 1fr` and
    # scrolls internally when content exceeds that.
    CSS = """
    Screen {
        height: 100vh;
    }
    #transcript {
        height: 1fr;
    }
    /* The bottom stack (turn status + info bar + composer) lives in
     * a single Vertical docked to the bottom of the screen. Docking
     * each widget individually made them all fight for the last row
     * — the composer painted over the InfoBar. Wrapping them lets
     * the container own the dock and the children stack naturally
     * top-to-bottom inside it. */
    #bottom-stack {
        dock: bottom;
        height: auto;
    }
    #bottom-stack > * {
        /* Belt & braces: override any child's own `dock: bottom`
         * so it participates in the container's flow. */
        dock: none;
    }
    """

    def __init__(
        self,
        *,
        session: ChatSessionBundle,
        engine: Any,
        full_history: bool = False,
    ) -> None:
        # Resolve theme path per-instance so NO_COLOR toggles work
        # in tests that monkeypatch the env between constructions.
        self.CSS_PATH = _resolve_css_path()
        super().__init__()
        self.session = session
        self.engine = engine
        self.event_adapter = EventAdapter(self)
        # spec-004: `InlineApprovalCallback` needs a live `PromptsImpl`
        # handle for `.choice(...)` routing; construction now defers
        # to `on_mount` where `PromptsImpl` is built with the session
        # store + thread handles the runtime carries. Populated in
        # `on_mount` before authz registration.
        self.approval_callback: InlineApprovalCallback | None = None
        self.full_history = full_history
        # Set by the exchange worker; cleared in a finally.
        self.active_cancel: Any = None
        self.last_cancel_reason: str | None = None
        # Track the last streaming assistant widget so newer inline
        # widgets can be appended after it. Also used for the
        # cancellation UI to append `⚠ Cancelled` to the partial.
        self._last_assistant_widget: AssistantProse | None = None
        self._last_assistant_text: str = ""
        # Mutable session state used by the legacy slash handlers.
        # Populated in `on_mount` once the runtime handles are wired.
        self._chat_state: ChatSessionState | None = None
        # spec-003: service registry. Placeholder / all-None today —
        # concrete impls wire in specs 004/006/012. `on_unmount`
        # iterates the registry's fields and calls `aclose` on any
        # non-None value; the plumbing is in place for M2+ specs.
        self._service_registry: ChatServiceRegistry | None = None
        # spec-016: multi-workflow layout manager. Constructed in
        # `on_mount` once the `#transcript` container and the event
        # adapter are up. None in headless / adapter-less fixtures —
        # `WorkflowsImpl._notify_layout` silently no-ops in that case.
        self._workflow_layout_manager: Any = None

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        with Vertical(id="bottom-stack"):
            yield TurnStatus(id="turn-status")
            yield SlashPopup(id="slash-popup")
            yield Composer(id="composer")
            yield InfoBar(id="info-bar")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        """Register the approval callback and replay history if resuming."""
        # Wire the InfoBar with the session-visible identity.
        info = self.query_one(InfoBar)
        info.agent = self.session.config.name
        info.model = self.session.model_label
        info.session = str(self.session.session_id)[:8]
        info.cwd = self.session.root.name

        # Initialise the mutable chat state slash handlers touch.
        # `stdout` is captured per-invocation in `_handle_slash`;
        # `state` carries the pending flags the exchange worker
        # reads (force-skill, model override, session lifecycle).
        self._chat_state = ChatSessionState(
            runtime=self.session.runtime,
            config=self.session.config,
            session_id=self.session.session_id,
            thread_id=self.session.thread_id,
        )

        # spec-004: construct `PromptsImpl` and register it in the
        # service registry so tools that `requires=["prompts"]` can
        # reach it (spec-014 wires the declaration). Also feeds the
        # `InlineApprovalCallback` so approval prompts route through
        # the event bus instead of directly mounting a widget.
        #
        # spec-006: same construction pattern for `CardsImpl` — tools
        # that `requires=["cards"]` reach it through the registry.
        #
        # Preserve any pre-set services on the registry (test fixtures
        # inject stubs before mount to exercise the teardown loop);
        # only fill in slots that are still `None`.
        prompts_impl: PromptsImpl | None = None
        cards_impl: CardsImpl | None = None
        workflows_impl: WorkflowsImpl | None = None
        existing = self._service_registry
        pre_set_prompts = existing.prompts if existing is not None else None
        pre_set_cards = existing.cards if existing is not None else None
        pre_set_workflows = existing.workflows if existing is not None else None
        if pre_set_prompts is None:
            try:
                prompts_impl = PromptsImpl(
                    session_store=self.session.runtime.store,
                    session_id=self.session.session_id,
                    thread_id=self.session.thread_id,
                )
            except Exception:  # noqa: BLE001 — headless fixtures may lack a real store
                _LOGGER.debug(
                    "ChatApp.on_mount: PromptsImpl construction skipped",
                    exc_info=True,
                )
                prompts_impl = None
        if pre_set_cards is None:
            try:
                cards_impl = CardsImpl(
                    session_store=self.session.runtime.store,
                    session_id=self.session.session_id,
                    thread_id=self.session.thread_id,
                )
            except Exception:  # noqa: BLE001 — headless fixtures may lack a real store
                _LOGGER.debug(
                    "ChatApp.on_mount: CardsImpl construction skipped",
                    exc_info=True,
                )
                cards_impl = None
        # spec-012: same construction pattern for `WorkflowsImpl`.
        # Headless fixtures may lack a real runtime / workspace root;
        # boot even if construction fails.
        if pre_set_workflows is None:
            try:
                from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

                run_store = WorkflowRunStore(self.session.runtime.root)
                workflows_impl = WorkflowsImpl(
                    runtime=self.session.runtime,
                    cards=cards_impl,
                    prompts=prompts_impl,
                    session_store=self.session.runtime.store,
                    session_id=self.session.session_id,
                    thread_id=self.session.thread_id,
                    run_store=run_store,
                )
            except Exception:  # noqa: BLE001 — headless fixtures may lack a real runtime
                _LOGGER.debug(
                    "ChatApp.on_mount: WorkflowsImpl construction skipped",
                    exc_info=True,
                )
                workflows_impl = None
        if existing is None:
            self._service_registry = ChatServiceRegistry(
                cards=cards_impl,
                prompts=prompts_impl,
                workflows=workflows_impl,
            )
        elif (
            (pre_set_prompts is None and prompts_impl is not None)
            or (pre_set_cards is None and cards_impl is not None)
            or (pre_set_workflows is None and workflows_impl is not None)
        ):
            # Merge: keep the pre-set fields; fill in what was empty.
            self._service_registry = ChatServiceRegistry(
                cards=cards_impl if pre_set_cards is None else existing.cards,
                prompts=(
                    prompts_impl if pre_set_prompts is None else existing.prompts
                ),
                workflows=(
                    workflows_impl
                    if pre_set_workflows is None
                    else existing.workflows
                ),
                session=existing.session,
            )

        # Wire the adapter so `.answered` / `.cancelled` UI events
        # resolve the impl's pending future. Skip when no impl exists
        # (headless test fixtures).
        active_prompts = (
            self._service_registry.prompts
            if self._service_registry is not None
            else None
        )
        if active_prompts is not None and hasattr(
            active_prompts, "resolve_pending",
        ):
            self.event_adapter.register_prompts_impl(active_prompts)

        # spec-008: wire the ``Cards`` handle into the adapter so the
        # boot-time interruption notice from
        # ``_scan_dangling_prompts`` fires through the same
        # ``Cards.notice`` seam every other notice uses.
        active_cards = (
            self._service_registry.cards
            if self._service_registry is not None
            else None
        )
        if active_cards is not None and hasattr(active_cards, "notice"):
            self.event_adapter.register_cards_impl(active_cards)

        # spec-014: wire ``WorkflowsImpl`` into the adapter so the
        # prompt-routing workflow branch can emit the durable
        # ``awaiting_approval`` / ``approval_resolved`` UI events
        # without ``PromptsImpl`` learning about workflows.
        active_workflows = (
            self._service_registry.workflows
            if self._service_registry is not None
            else None
        )
        if active_workflows is not None and hasattr(
            active_workflows, "emit_awaiting_approval",
        ):
            self.event_adapter.register_workflows_impl(active_workflows)

        # spec-016: construct the WorkflowLayoutManager so multi-workflow
        # cards reorder into the two-group layout with the divider. The
        # manager needs a live `#transcript` VerticalScroll and the
        # adapter's `_card_widgets` map to resolve card widgets. Skip
        # when either is unavailable (headless fixtures) — the notify
        # hook on WorkflowsImpl silently no-ops without a manager.
        if active_workflows is not None and hasattr(
            active_workflows, "register_layout_manager",
        ):
            try:
                from cli.chat.services.workflow_layout import (  # noqa: PLC0415
                    WorkflowLayoutManager,
                )

                transcript = self.query_one("#transcript", VerticalScroll)
                registry = getattr(
                    active_workflows, "_workflow_registry", None,
                )
                card_widgets = getattr(
                    self.event_adapter, "_card_widgets", None,
                )
                if registry is not None and card_widgets is not None:
                    layout_manager = WorkflowLayoutManager(
                        transcript=transcript,
                        get_records=lambda reg=registry: list(reg.values()),
                        get_card_widget=(
                            lambda cid, cw=card_widgets: cw.get(cid)
                        ),
                    )
                    active_workflows.register_layout_manager(layout_manager)
                    self._workflow_layout_manager = layout_manager
            except Exception:  # noqa: BLE001 — layout wiring best-effort
                _LOGGER.debug(
                    "ChatApp.on_mount: WorkflowLayoutManager wiring skipped",
                    exc_info=True,
                )

        # `InlineApprovalCallback` needs a live `Prompts` handle; skip
        # authz registration when no impl exists (the headless
        # teardown fixtures don't exercise the approval flow).
        if active_prompts is not None:
            self.approval_callback = InlineApprovalCallback(
                self, active_prompts,
            )
            # Register the approval callback with the runtime's authz
            # provider so tool checks with `prompt` effects surface as
            # inline ApprovalBlock widgets (via the adapter's routing).
            authz = self.session.runtime.authz
            if isinstance(authz, RuleBasedAuthorizationProvider):
                authz.register_prompt_callback(self.approval_callback)

        # feat-015: run the in-process orphan sweep BEFORE replay so
        # any synthetic `workflow.run.aborted` events land in the run
        # JSONLs before `EventAdapter` picks them up. Subprocess-hosted
        # runs are UNTOUCHED — they use the PID-based sweep at
        # `workflows.orphan_sweep.sweep`, which runs on CLI dispatch.
        # Best-effort: a failing sweep (headless fixtures without a
        # real run store, permission errors) is logged and swallowed.
        if active_workflows is not None and hasattr(
            active_workflows, "sweep_orphans",
        ):
            try:
                await active_workflows.sweep_orphans()
                if hasattr(active_workflows, "fire_orphan_notice"):
                    active_workflows.fire_orphan_notice()
            except Exception:  # noqa: BLE001 — sweep is boot-time best-effort
                _LOGGER.exception(
                    "ChatApp.on_mount: workflow orphan sweep raised",
                )

        # Replay history (tail-20 by default) if this is a resume.
        if self.session.is_resume:
            await self._replay_history()

        # Focus the composer so the user can start typing.
        try:
            self.query_one(Composer).focus()
        except Exception:
            pass

    async def on_unmount(self) -> None:
        """Clear the modal registration on exit + tear down services.

        Load-bearing for tests that share a runtime across cases —
        a leaked callback would fire against a torn-down App.

        spec-003: iterates `self._service_registry` fields and calls
        `await service.aclose()` on any non-None value with an
        `aclose` attribute. Today the registry is all-None so this is
        dead code, but the plumbing is in place for M2+ specs.
        Per-service exceptions are caught and logged so one bad
        `aclose` doesn't stop teardown of the others.
        """
        authz = self.session.runtime.authz
        if isinstance(authz, RuleBasedAuthorizationProvider):
            try:
                authz.clear_prompt_callback()
            except Exception:
                pass

        if self._service_registry is not None:
            for field_name in ("cards", "prompts", "workflows", "session"):
                service = getattr(self._service_registry, field_name, None)
                if service is None:
                    continue
                aclose = getattr(service, "aclose", None)
                if aclose is None:
                    continue
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 — one bad aclose can't stop the others
                    _LOGGER.exception(
                        "ChatApp.on_unmount: service %r aclose raised",
                        field_name,
                    )

    async def _replay_history(self) -> None:
        """Replay the thread's JSONL through the event adapter.

        Non-``full_history``: keep only the last 20 turns (each turn
        starts at a ``USER_MESSAGE``) and mount a ``ScrollbackCue``
        at the top when truncating.
        """
        events: list[Event] = []
        async for event in self.session.runtime.store.read_path_to_leaf(
            self.session.thread_id,
        ):
            events.append(event)

        if self.full_history:
            selected = events
            truncated = False
        else:
            # 40 turns by default. Larger than the spec's original
            # tail-20 default after user feedback that scrolled-away
            # content isn't preserved in terminal scrollback (see
            # `/export` for a full-transcript dump). The bigger tail
            # gives more visible history to scroll through in-app.
            selected, truncated = _tail_turns(events, n=40)

        transcript = self.query_one("#transcript", VerticalScroll)
        if truncated:
            await transcript.mount(ScrollbackCue())
        for event in selected:
            try:
                await self.event_adapter.handle(event)
            except Exception:
                # Replay is best-effort; a bad event shouldn't crash
                # the chat. Continue.
                continue

        # spec-016 AC #10: at end of chronological replay, flush the
        # layout manager once so multi-workflow cards land in canonical
        # order (terminal successes at top by `terminal_at` ASC, failed
        # pinned at boundary, in-progress by `started_at` ASC). Ordering
        # is deterministic because `terminal_at` + `started_at` are
        # durable fields from spec-002 + spec-012.
        if self._workflow_layout_manager is not None:
            try:
                await self._workflow_layout_manager._flush_reorder()
            except Exception:  # noqa: BLE001 — flush is best-effort
                _LOGGER.exception(
                    "ChatApp._replay_history: layout flush raised",
                )

    # ------------------------------------------------------------------
    # Exchange worker
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def _run_exchange(self, user_text: str) -> None:
        """Send ``user_text`` to the engine and dispatch each event.

        Consumes ``ChatSessionState.pending_force_skill`` when
        building the trigger — any refs queued by ``/skill`` since
        the last exchange promote the trigger from ``user_message``
        to ``user_message_with_force_skill``. The list is cleared
        after use so a subsequent unqualified message returns to
        the default trigger shape.
        """
        from cli.chat.cancel import new_cancel_token  # noqa: PLC0415

        cancel = new_cancel_token()
        self.active_cancel = cancel
        self.last_cancel_reason = None
        try:
            force_skills: list[str] = []
            if self._chat_state is not None and self._chat_state.pending_force_skill:
                force_skills = list(self._chat_state.pending_force_skill)
                self._chat_state.pending_force_skill.clear()
            trigger = _build_user_trigger(user_text, force_skills)
            ctx = ExecutionContext(
                thread_id=self.session.thread_id,
                trigger=trigger,
                cancellation=cancel,
            )
            event_stream = self.engine.execute_stream(self.session.config, ctx)
            try:
                async for event in event_stream:
                    await self.event_adapter.handle(event)
            except Exception as exc:
                await self.mount_exchange_failed(str(exc))
        finally:
            self.active_cancel = None
            # Ensure turn status clears after any exchange path.
            try:
                turn = self.query_one(TurnStatus)
                turn.verb = ""
                turn.target = ""
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Mount helpers — called by EventAdapter and InlineApprovalCallback
    # ------------------------------------------------------------------

    async def mount_user_message(self, text: str, timestamp: datetime) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(UserChatbox(text, timestamp))
        transcript.scroll_end(animate=False)

    def _assistant_border_title(self) -> str:
        """Build the `AssistantProse` border title: `<agent> · HH:MM:SS`.

        Mirrors ``UserChatbox``'s ``You · HH:MM:SS`` format so the two
        boxes read as a matched pair.
        """
        agent = self.session.config.name
        return f"{agent} · {datetime.now().strftime('%H:%M:%S')}"

    async def mount_assistant_message(self, text: str) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        prose = AssistantProse()
        prose.border_title = self._assistant_border_title()
        await transcript.mount(prose)
        await prose.append(text)
        self._last_assistant_widget = prose
        transcript.scroll_end(animate=False)

    async def mount_assistant_streaming_widget(self) -> AssistantProse:
        transcript = self.query_one("#transcript", VerticalScroll)
        prose = AssistantProse()
        prose.border_title = self._assistant_border_title()
        await transcript.mount(prose)
        self._last_assistant_widget = prose
        transcript.scroll_end(animate=False)
        return prose

    async def mount_tool_call(
        self,
        tool_name: str,
        args: str,
        body: str,
        *,
        failed: bool = False,
        recovery_hint: str | None = None,
    ) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        block = ToolCallBlock(
            tool_name,
            args,
            body,
            failed=failed,
            recovery_hint=recovery_hint,
        )
        await transcript.mount(block)
        transcript.scroll_end(animate=False)

    async def _mount_transcript_notice(
        self,
        text: str,
        *,
        kind: str = "info",
    ) -> None:
        """Internal helper — mount a
        [`NoticeWidget`][cli.chat.widgets.notice.NoticeWidget] at the
        transcript root.

        spec-008 retired the public ``ChatApp.mount_notice`` in favour
        of ``Cards.notice()`` as the single notice-mount seam. This
        private helper preserves the "slash output → transcript
        notice" and "internal error line" paths that don't go through
        the ``Cards`` service (slash commands, ``/export`` results).
        Callers outside ``ChatApp`` should route through
        ``Cards.notice(...)`` instead.
        """
        transcript = self.query_one("#transcript", VerticalScroll)
        notice = NoticeWidget(text=text, kind=kind)
        await transcript.mount(notice)
        transcript.scroll_end(animate=False)

    async def mount_exchange_failed(self, message: str) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        notice = Static(f"[exchange failed — {message}]", classes="error")
        notice.styles.color = "red"
        await transcript.mount(notice)
        transcript.scroll_end(animate=False)

    async def mount_approval_block(
        self,
        rule_id: str | None,
        action: str,
        target: str | None,
        *,
        parent_hint: str | None = None,
    ) -> ApprovalBlock:
        transcript = self.query_one("#transcript", VerticalScroll)
        block = ApprovalBlock(
            rule_id, action, target, parent_hint=parent_hint,
        )
        await transcript.mount(block)
        # Freeze composer.
        try:
            composer = self.query_one(Composer)
            composer.awaiting_approval = True
        except Exception:
            pass
        try:
            turn = self.query_one(TurnStatus)
            turn.awaiting_approval = True
        except Exception:
            pass
        transcript.scroll_end(animate=False)
        return block

    def clear_awaiting_approval(self) -> None:
        try:
            composer = self.query_one(Composer)
            composer.awaiting_approval = False
        except Exception:
            pass
        try:
            turn = self.query_one(TurnStatus)
            turn.awaiting_approval = False
        except Exception:
            pass

    async def set_turn_verb(self, verb: str, *, target: str = "") -> None:
        try:
            turn = self.query_one(TurnStatus)
            turn.verb = verb
            turn.target = target
        except Exception:
            pass

    def last_assistant_text(self) -> str:
        return self._last_assistant_text

    def set_last_assistant_text(self, text: str) -> None:
        self._last_assistant_text = text

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def on_text_area_changed(self, event: _TextArea.Changed) -> None:
        """Drive the slash autocomplete popup off composer keystrokes.

        `Composer` subclasses `TextArea`, so the framework emits
        `TextArea.Changed` for every content edit. We forward the
        current text into ``SlashPopup.prefix`` — the popup handles
        its own show / hide via CSS class + reactive.
        """
        if event.text_area.id != "composer":
            return
        try:
            popup = self.query_one(SlashPopup)
        except Exception:  # noqa: BLE001 — popup may be unmounted
            return
        popup.prefix = event.text_area.text

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        """Composer sent a submit — route through slash / engine.

        The `UserChatbox` mount is NOT done here; the engine emits a
        ``USER_MESSAGE`` event which ``EventAdapter._handle_user_message``
        routes to ``mount_user_message``. Mounting here would produce
        two chatboxes per submission (see the adapter's own docstring:
        the adapter is the single mount site so replay + live paths
        share it).
        """
        message.stop()
        text = message.content
        composer = self.query_one(Composer)
        # Clear the composer for the next input.
        composer.text = ""
        # Collapse the slash popup, if it was showing suggestions.
        try:
            self.query_one(SlashPopup).prefix = ""
        except Exception:  # noqa: BLE001 — popup may be unmounted
            pass
        text = text.strip()
        if not text:
            return
        # Slash / @ / ! prefix routing.
        if text.startswith("/"):
            await self._handle_slash(text)
            return
        self._run_exchange(text)

    async def _handle_slash(self, text: str) -> None:
        """Dispatch a slash command through ``cli._slash``'s registry.

        Splits ``text`` into a head token (``/name``) and an argument
        string, resolves the handler via ``_resolve_command_name`` so
        aliases (``/quit`` → ``/exit``) route correctly, and invokes
        it with a synthetic ``SlashContext`` whose ``stdout`` is a
        ``StringIO``. Handler output is mounted as a chat notice; the
        ``ControlSignal`` return decides whether to exit; any
        ``ChatSessionState`` pending-flags the handler set are
        picked up here or on the next exchange.
        """
        parts = text.strip().split(maxsplit=1)
        head = parts[0] if parts else ""
        arg_text = parts[1] if len(parts) > 1 else ""

        command = _resolve_command_name(head)
        if command is None:
            await self._mount_transcript_notice(
                f"unknown command: {head}. Try /help for the list."
            )
            return

        # Textual-local intercepts (commands with `handler=None` in
        # the registry whose behaviour needs UI access).
        if command.name == "/export":
            await self._handle_export(arg_text)
            return

        if command.handler is None:
            await self._mount_transcript_notice(
                f"{command.name}: registered but no handler wired yet."
            )
            return
        if self._chat_state is None:
            await self._mount_transcript_notice(
                f"{command.name}: chat runtime not ready yet."
            )
            return

        stdout = io.StringIO()
        ctx = SlashContext(
            stdout=stdout,
            agent_config=self.session.config,
            authz=self.session.runtime.authz,
            agent_name=self.session.config.name,
            state=self._chat_state,
        )

        try:
            signal = await command.handler(arg_text, ctx)
        except Exception as exc:  # noqa: BLE001 — handler safety net
            await self._mount_transcript_notice(
                f"{command.name}: {type(exc).__name__}: {exc}"
            )
            return

        output = stdout.getvalue()

        # `/clear` writes raw ANSI (`\x1b[H\x1b[2J\x1b[3J`) to stdout
        # to wipe a terminal; the Textual surface can't display those
        # bytes, so interpret them as "wipe the transcript" and drop
        # the escape from the mounted notice.
        if "\x1b[2J" in output:
            await self._clear_transcript()
            output = ""

        stripped = output.rstrip("\n")
        if stripped:
            await self._mount_transcript_notice(stripped)

        # Pending flags: consume the ones we can honour locally now;
        # flag the others as "coming soon" so behaviour is honest.
        state = self._chat_state
        if state.pending_clear:
            await self._clear_transcript()
            state.pending_clear = False
        if state.pending_new_session or state.pending_session_id:
            await self._mount_transcript_notice(
                "session lifecycle (/new, /session) is deferred to the "
                "slash follow-up spec; state noted, no switch performed."
            )
            state.pending_new_session = False
            state.pending_session_id = None
        if state.pending_workflow_run_id:
            await self._mount_transcript_notice(
                "/workflow binding recorded; workflow-runner integration "
                "arrives with the workflow bridge event spec."
            )

        if signal is ControlSignal.EXIT:
            self.exit(0)

    async def _handle_export(self, arg_text: str) -> None:
        """Dump the current thread's transcript to a text file.

        Called by ``/export [<path>]``. Reads every event on the
        thread via ``runtime.store.read_path_to_leaf`` and formats
        each into a human-readable block (user turn, assistant
        response, tool call/result, notices). Writes to
        ``arg_text.strip()`` if provided, else to
        ``~/.kolsch/exports/chat-<session>-<yyyy-mm-dd-hhmm>.txt``.

        Mount a notice with the resolved path on success so the
        user knows where to find the file. On failure, mount the
        error message.
        """
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        session_slug = str(self.session.session_id)[:12]
        target: Path
        if arg_text.strip():
            target = Path(arg_text.strip()).expanduser().resolve()
        else:
            exports_dir = Path.home() / ".kolsch" / "exports"
            exports_dir.mkdir(parents=True, exist_ok=True)
            target = exports_dir / f"chat-{session_slug}-{stamp}.txt"
        try:
            lines = [
                f"# kolsch chat transcript",
                f"# session: {self.session.session_id}",
                f"# thread:  {self.session.thread_id}",
                f"# agent:   {self.session.config.name}",
                f"# model:   {self.session.model_label}",
                f"# exported: {datetime.now().isoformat(timespec='seconds')}",
                "",
            ]
            async for event in self.session.runtime.store.read_path_to_leaf(
                self.session.thread_id,
            ):
                block = _format_event_for_export(event)
                if block:
                    lines.append(block)
            target.write_text("\n".join(lines) + "\n")
        except Exception as exc:  # noqa: BLE001
            await self._mount_transcript_notice(
                f"/export failed: {type(exc).__name__}: {exc}"
            )
            return
        await self._mount_transcript_notice(f"transcript exported → {target}")

    async def _clear_transcript(self) -> None:
        """Remove every mounted widget from the transcript.

        Called on `/clear`. Mirrors the legacy REPL's screen wipe:
        session + JSONL untouched, only the visible transcript is
        emptied.
        """
        try:
            transcript = self.query_one("#transcript", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        for child in list(transcript.children):
            await child.remove()
        self._last_assistant_widget = None
        self._last_assistant_text = ""

    # ------------------------------------------------------------------
    # Action handlers (from BINDINGS)
    # ------------------------------------------------------------------

    def action_cancel_exchange(self) -> None:
        """``escape`` / ``ctrl+c`` — cancel the active exchange."""
        request_cancel(self)

    def action_scroll_transcript(self, direction: str) -> None:
        """Scroll the transcript ``VerticalScroll`` by keyboard.

        Bound to PgUp/PgDn/Home/End. Textual's inline mode reserves
        a fixed region, so `VerticalScroll` internal buffer holds
        widgets that don't fit on the current screen — these
        bindings let the user navigate that internal buffer without
        the mouse (matches conventional pager keys).
        """
        try:
            transcript = self.query_one("#transcript", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        if direction == "up":
            transcript.scroll_page_up(animate=False)
        elif direction == "down":
            transcript.scroll_page_down(animate=False)
        elif direction == "home":
            transcript.scroll_home(animate=False)
        elif direction == "end":
            transcript.scroll_end(animate=False)

    def action_scroll_to_workflow_card(self, run_id: str) -> None:
        """feat-018 — scroll the transcript to a workflow card by ``run_id``.

        Invoked by :class:`WorkflowAnchorButton` widgets mounted in
        :class:`AssistantProse` when the assistant's prose contains
        ``[<label>](kolsch-anchor://workflow_card/<run_id>)`` markup.
        Delegates to
        :meth:`cli.chat.event_adapter.EventAdapter._scroll_to_workflow_card`
        so the actual lookup + scroll live at the adapter level next
        to :attr:`_card_widgets`.
        """
        adapter = getattr(self, "event_adapter", None)
        if adapter is None:
            return
        adapter._scroll_to_workflow_card(run_id)

    def action_quit_if_idle(self) -> None:
        """``ctrl+d`` — exit if the composer is empty."""
        try:
            composer = self.query_one(Composer)
        except Exception:
            self.exit(0)
            return
        if not composer.text:
            self.exit(0)



# ---------------------------------------------------------------------------
# Turn slicing for tail-20 replay
# ---------------------------------------------------------------------------


def _format_event_for_export(event: Event) -> str:
    """Render one event into a human-readable transcript block.

    Returns an empty string for events that aren't worth surfacing
    in an export (bookkeeping like ``config.set``, ``session.opened``).
    Format aims to be quick to read in ``less`` or an editor without
    trying to be structured for machine consumption — anyone who
    needs raw JSONL can read it via
    ``kolsch sessions events <session-id>``.
    """
    ts = getattr(event, "created_at", None) or getattr(event, "ts", None)
    when = ts.strftime("%H:%M:%S") if ts is not None else ""
    et = event.type.value if hasattr(event.type, "value") else str(event.type)
    data = getattr(event, "data", None)
    data_dict = data if isinstance(data, dict) else getattr(data, "__dict__", {})

    if et == "user.message":
        content = data_dict.get("content") or {}
        text = content.get("text") if isinstance(content, dict) else str(content)
        return f"=== You · {when} ===\n{text}\n"
    if et == "agent.message":
        content = data_dict.get("content") or {}
        text = content.get("text") if isinstance(content, dict) else str(content)
        return f"=== Assistant · {when} ===\n{text}\n"
    if et == "tool.called":
        name = data_dict.get("tool_name", "?")
        args = data_dict.get("arguments") or {}
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if isinstance(args, dict) else str(args)
        return f"# tool called · {when} · {name}({args_str})"
    if et == "tool.result":
        name = data_dict.get("tool_name", "?")
        out = data_dict.get("output") or ""
        return f"# tool result · {when} · {name}\n{out}\n"
    if et == "tool.failed":
        name = data_dict.get("tool_name", "?")
        err = data_dict.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return f"# tool FAILED · {when} · {name}: {msg}\n"
    if et == "thread.exchange_failed":
        err = data_dict.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return f"# exchange failed · {when}: {msg}\n"
    return ""


def _tail_turns(
    events: list[Event],
    *,
    n: int,
) -> tuple[list[Event], bool]:
    """Return the last ``n`` turns of ``events`` plus a truncated flag.

    A "turn" is delimited by consecutive ``USER_MESSAGE`` events —
    everything between two ``USER_MESSAGE`` events belongs to the
    turn that ended with the earlier of the two.

    Coordinator decision 5: default N=20.
    """
    # Find USER_MESSAGE indexes.
    user_indexes = [
        i for i, e in enumerate(events) if e.type == EventType.USER_MESSAGE
    ]
    if len(user_indexes) <= n:
        return events, False
    cutoff = user_indexes[-n]
    return events[cutoff:], True


__all__ = ["ChatApp"]

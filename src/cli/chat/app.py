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
from textual.widgets import TextArea as _TextArea  # noqa: E402 — Changed msg source
from engine import ExecutionContext
from providers import RuleBasedAuthorizationProvider
from schemas.events import Event, EventType

if TYPE_CHECKING:
    pass


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
        self.approval_callback = InlineApprovalCallback(self)
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

        # Register the approval callback with the runtime's authz
        # provider so tool checks with `prompt` effects surface as
        # inline ApprovalBlock widgets.
        authz = self.session.runtime.authz
        if isinstance(authz, RuleBasedAuthorizationProvider):
            authz.register_prompt_callback(self.approval_callback)

        # Replay history (tail-20 by default) if this is a resume.
        if self.session.is_resume:
            await self._replay_history()

        # Focus the composer so the user can start typing.
        try:
            self.query_one(Composer).focus()
        except Exception:
            pass

    async def on_unmount(self) -> None:
        """Clear the modal registration on exit.

        Load-bearing for tests that share a runtime across cases —
        a leaked callback would fire against a torn-down App.
        """
        authz = self.session.runtime.authz
        if isinstance(authz, RuleBasedAuthorizationProvider):
            try:
                authz.clear_prompt_callback()
            except Exception:
                pass

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

    async def mount_notice(self, text: str) -> None:
        """Mount a dim italic notice line (used by MODEL_FALLBACK etc.)."""
        transcript = self.query_one("#transcript", VerticalScroll)
        notice = Static(text, classes="notice")
        notice.styles.text_style = "dim italic"
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
    ) -> ApprovalBlock:
        transcript = self.query_one("#transcript", VerticalScroll)
        block = ApprovalBlock(rule_id, action, target)
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
            await self.mount_notice(
                f"unknown command: {head}. Try /help for the list."
            )
            return

        # Textual-local intercepts (commands with `handler=None` in
        # the registry whose behaviour needs UI access).
        if command.name == "/export":
            await self._handle_export(arg_text)
            return

        if command.handler is None:
            await self.mount_notice(
                f"{command.name}: registered but no handler wired yet."
            )
            return
        if self._chat_state is None:
            await self.mount_notice(
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
            await self.mount_notice(
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
            await self.mount_notice(stripped)

        # Pending flags: consume the ones we can honour locally now;
        # flag the others as "coming soon" so behaviour is honest.
        state = self._chat_state
        if state.pending_clear:
            await self._clear_transcript()
            state.pending_clear = False
        if state.pending_new_session or state.pending_session_id:
            await self.mount_notice(
                "session lifecycle (/new, /session) is deferred to the "
                "slash follow-up spec; state noted, no switch performed."
            )
            state.pending_new_session = False
            state.pending_session_id = None
        if state.pending_workflow_run_id:
            await self.mount_notice(
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
            await self.mount_notice(
                f"/export failed: {type(exc).__name__}: {exc}"
            )
            return
        await self.mount_notice(f"transcript exported → {target}")

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

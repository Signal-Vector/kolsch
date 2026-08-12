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

import asyncio
import contextlib
import io
import logging
import os
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _threads_queues, _worker
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static
from textual.widgets import TextArea as _TextArea

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
from cli.chat.services.cards_impl import UI_EVENT_TOPIC, CardsImpl
from cli.chat.services.prompts_impl import PromptsImpl
from cli.chat.services.workflows_impl import WorkflowsImpl
from cli.chat.session import ChatSessionBundle, _build_user_trigger
from cli.chat.widgets import (
    ApprovalBlock,
    AssistantProse,
    Composer,
    InfoBar,
    ScrollbackCue,
    SlashPopup,
    StatusBar,
    ToolCallBlock,
    UserChatbox,
)
from cli.chat.widgets.notice import NoticeWidget
from cli.chat.widgets.working_placeholder import TerminalKind, WorkingPlaceholder
from engine import ExecutionContext
from errors import ExchangeCancelled
from providers import RuleBasedAuthorizationProvider
from schemas.events import Event, EventType

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


def _parse_compact_args(
    arg_text: str,
) -> tuple[bool, str | None, str | None]:
    """Parse the ``/compact`` argument shape.

    feat-054 Phase 3. Accepts:

    - ``""`` — force compaction with no extra instructions.
    - ``"--preview"`` — preview mode (no side-effect on live context).
    - ``"--instructions '<text>'"`` — extra summariser guidance.
    - Both flags in either order.

    Returns ``(preview, instructions, error)``. ``error`` is ``None``
    on success; on parse failure it is a one-line message the caller
    surfaces as a notice. Uses ``shlex`` so the operator can quote
    multi-word instructions with either single or double quotes.
    """
    import shlex  # noqa: PLC0415

    text = arg_text.strip()
    if not text:
        return False, None, None
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        return False, None, f"could not parse arguments: {exc}"
    preview = False
    instructions: str | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--preview":
            preview = True
            i += 1
        elif tok == "--instructions":
            if i + 1 >= len(tokens):
                return False, None, "--instructions requires a value"
            instructions = tokens[i + 1]
            i += 2
        else:
            return (
                False,
                None,
                f"unknown token {tok!r} (expected --preview or --instructions)",
            )
    return preview, instructions, None


_THEMES_DIR = Path(__file__).parent / "themes"


def _resolve_css_path() -> str:
    """Return the theme file to load based on ``NO_COLOR``.

    Called at ``ChatApp`` construction time (before ``super().__init__``)
    so Textual picks up the right stylesheet.
    """
    theme = "mono.tcss" if os.environ.get("NO_COLOR") is not None else "theme.tcss"
    return str(_THEMES_DIR / theme)


# bug-067 — shutdown-watchdog backstop. spec-069 (owned daemon executor +
# bounded LLM/network reads + node timeouts) is the primary fix for the
# `Py_Finalize → ThreadHandle_join` hang; the watchdog exists so a
# hung teardown from an unforeseen wedge (third-party thread we cannot
# daemonize, unbounded native call, wedged asyncio task) can never again
# strand a `kolsch chat` process for hours.
#
# Design:
#   * Armed at the top of `ChatApp.on_unmount`; disarmed at the end
#     after every `aclose()` + the owned-executor shutdown succeeded.
#   * Uses a `threading.Timer` (daemon thread) — reliable across any
#     teardown-path wedge, including a hung event loop or a hung main
#     thread. Neither a signal-based `SIGALRM` (Windows-hostile,
#     conflicts with other handlers) nor an asyncio-scheduled callback
#     (dies if the loop itself wedges) has that property.
#   * On expiry the callback calls the module-level `_EXIT_HOOK`
#     (`os._exit` in production, a spy in tests). It deliberately
#     does NOT attempt a graceful sqlite `conn.close()` / WAL
#     checkpoint (that is the exact step spec-069's Technical Context
#     identifies as hangable) and does NOT `join()` non-daemon workers.
#     Store contract makes `os._exit` safe: JSONL append is
#     `O_APPEND + flock + fsync` (recoverable via
#     `recover_partial_final_line` on next read); sqlite is WAL
#     autocommit so every statement is durable on disk and WAL
#     recovery handles an abrupt exit.
#   * Window default is 5s (matches spec-069 AC5's ≤5s teardown
#     target); overridable via `KOLSCH_SHUTDOWN_WATCHDOG_SECONDS`
#     (float; blank / `0` / negative disables the watchdog entirely,
#     restoring the pre-bug-067 behaviour).
_DEFAULT_SHUTDOWN_WATCHDOG_SECONDS: float = 5.0


def _read_shutdown_watchdog_seconds() -> float | None:
    """Return the watchdog window in seconds, or `None` if disabled.

    `None` means "do not arm the watchdog" — mirrors the shape of
    `_read_llm_call_timeout_seconds` in `engine/_crewai_adapter.py`.
    """
    raw = os.environ.get("KOLSCH_SHUTDOWN_WATCHDOG_SECONDS")
    if raw is None:
        return _DEFAULT_SHUTDOWN_WATCHDOG_SECONDS
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        value = float(stripped)
    except ValueError:
        return _DEFAULT_SHUTDOWN_WATCHDOG_SECONDS
    if value <= 0:
        return None
    return value


# Module-level exit hook, injectable for tests. Production: `os._exit`
# (fast exit — no atexit hooks, no interpreter finalization = no
# thread-join hang). Tests install a spy via `_set_exit_hook` to assert
# firing without actually killing the pytest process.
_EXIT_HOOK: Callable[[int], None] = os._exit


def _set_exit_hook(hook: Callable[[int], None]) -> None:
    """Install a replacement exit hook (test seam).

    Prod code MUST NOT call this — the default `os._exit` is what
    makes the fast-exit path safe (no atexit → no thread-join hang).
    Tests use this to swap in a spy.
    """
    global _EXIT_HOOK  # noqa: PLW0603 — module-level test seam
    _EXIT_HOOK = hook


def force_process_exit(code: int) -> None:
    """Hard-exit the process through the module-level `_EXIT_HOOK`.

    bug-116: `kolsch chat`'s Textual inline driver runs its input reader
    as a NON-daemon thread. When the controlling terminal goes away
    (window closed / SSH drop) stdin becomes a dead tty at permanent EOF
    and that thread busy-loops (`select()` returns instantly on EOF, the
    read yields empty, the `while not exit_event` loop spins), pinning a
    CPU core. Because the thread is non-daemon the interpreter then blocks
    in `Py_Finalize`'s `threading._shutdown()` join and never exits — a
    100%-CPU zombie that outlives its terminal for days.

    The chat entrypoint calls this the moment `App.run_async()` returns so
    the interpreter never reaches that join. In production `_EXIT_HOOK` is
    `os._exit`, safe here by the store contract: JSONL append is
    `O_APPEND + flock + fsync` (recoverable via
    `recover_partial_final_line`) and sqlite runs WAL autocommit (durable;
    WAL recovery handles an abrupt exit). Tests install a spy via
    `_set_exit_hook`, so this returns instead of killing the process.

    Same class of fix as the bug-067 shutdown watchdog, which guards only
    the `on_unmount` teardown window and disarms before interpreter
    finalization; this covers the later `run_async()`-returned → finalize
    window that the watchdog never reaches.
    """
    _EXIT_HOOK(code)


_ESC_TOAST_SECONDS = 3.0
"""feat-019: the Esc-Esc toast auto-dismiss window.

After the user cancels a butler turn with the first ``Esc`` — and at
least one workflow is live — the toast surfaces for this many seconds.
A second ``Esc`` within the window cancels the oldest workflow; past
the window, ``Esc`` returns to FIFO semantics (cancel oldest workflow
directly). Coordinator lock in the spec.
"""

_ESC_TOAST_TEXT = "↳ butler cancelled · press Esc again to cancel workflow"
"""feat-019 AC #6: the exact toast string set on the status bar.

Best-effort 78-char target (Coordinator Q2 lock) — this line is 60
chars so it fits comfortably on standard terminals.
"""


# bug-024 (AC #11): the exact WARN message the guardrail fires when
# `WorkflowsImpl` is live but the mounted agent's declared tool set
# has no workflow-service tool. The message names the file the
# operator has to edit and the ref they're missing — no diagnostic
# pass needed. Extracted as a module constant so unit tests can
# assert on it byte-identically.
_WORKFLOW_TOOLS_MISSING_WARNING = (
    "WorkflowsImpl is running but the agent's declared tool set has "
    "no workflow-service tools (no tool declares "
    '`requires=["workflows"]`). Users prompting for workflow '
    "invocation will see the model fall back to `bash kolsch "
    "workflows run` instead of mounting a live WorkflowCard. Add "
    "`kolsch/tools/run_workflow` (and optionally "
    "`kolsch/tools/workflow_status`, `kolsch/tools/cancel_workflow`) "
    "to helper.yaml's `subsystems.tools.config.declared:` list."
)


def _declared_tool_refs(config: Any) -> list[str] | None:
    """Read `subsystems.tools.config.declared[*].ref` off a resolved config.

    Returns `None` when the resolved config has no `tools` subsystem
    or when the shape doesn't match the expected `declared:` list —
    the caller then falls back to the runtime-registry check.
    Returns an empty list when `tools` is declared but the list is
    empty (the agent explicitly opted out of every tool).
    """
    subsystems = getattr(config, "subsystems", None) or {}
    tools_entry = subsystems.get("tools") if isinstance(subsystems, dict) else None
    if tools_entry is None:
        return None
    body = getattr(tools_entry, "config", None)
    if not isinstance(body, dict):
        return None
    declared = body.get("declared")
    if not isinstance(declared, list):
        return None
    refs: list[str] = []
    for entry in declared:
        if isinstance(entry, dict):
            ref = entry.get("ref")
            if isinstance(ref, str):
                refs.append(ref)
        elif isinstance(entry, str):
            refs.append(entry)
    return refs


#: Suffix on declared refs (e.g. `kolsch/tools/run_workflow`) that the
#: shipped bundle uses to mark tools declaring `requires=["workflows"]`.
#: If a bundle adds a differently-named workflow tool, it must also add
#: its final segment here or edit `_warn_if_workflow_tools_missing`. The
#: guardrail is best-effort — it errs on the side of NOT warning when
#: it can't tell.
_WORKFLOW_TOOL_REF_NAMES: frozenset[str] = frozenset(
    {"run_workflow", "workflow_status", "cancel_workflow"},
)


def _warn_if_workflow_tools_missing(
    *,
    logger: logging.Logger,
    tools_subsystem: Any,
    agent_id: Any,
    workflows_impl: Any,
    declared_refs: list[str] | None = None,
) -> None:
    """Emit AC #11's WARN when `WorkflowsImpl` is live but no workflow tool.

    Two signals in priority order:

    1. If the caller supplied `declared_refs`, inspect them — the
       agent config's `subsystems.tools.config.declared:` list. This
       runs at `on_mount` (BEFORE the engine's per-exchange
       `_register_tools_with_subsystem` fires), so it's the earliest
       reliable signal. A ref's final segment matches
       `_WORKFLOW_TOOL_REF_NAMES` → the tool is (assumed to be) a
       workflow-service tool → no warning.
    2. If `declared_refs` is `None`, fall back to the runtime
       registry: iterate `list_registered_tools(agent_id)` and check
       each `ToolDefinition.requires`. This is what the operator's
       live subsystem would report AFTER an exchange has fired.

    Skips silently on any headless-fixture shape the runtime hands us
    (missing tools_subsystem, no agent id, or `WorkflowsImpl` not
    constructed — a common case in the unit-test suite). The warning
    fires only for the specific composition mismatch the guardrail
    exists to catch.
    """
    if workflows_impl is None:
        return

    # Signal 1: declared refs (config-time, most reliable at mount).
    if declared_refs is not None:
        for ref in declared_refs:
            tail = ref.rsplit("/", 1)[-1]
            if tail in _WORKFLOW_TOOL_REF_NAMES:
                return
        logger.warning(_WORKFLOW_TOOLS_MISSING_WARNING)
        return

    # Signal 2: runtime registry (registered-tools introspection).
    if tools_subsystem is None or agent_id is None:
        return
    try:
        registered = tools_subsystem.list_registered_tools(agent_id)
    except Exception:  # noqa: BLE001 — headless fixtures may lack the accessor
        logger.debug(
            "ChatApp.on_mount: workflow-tools guardrail: "
            "list_registered_tools raised; skipping",
            exc_info=True,
        )
        return

    def _has_workflows_requirement(tool_info: Any) -> bool:
        # The internal registry carries the full `ToolDefinition`
        # (which has `requires`). Walk through the accessor the
        # engine already uses so we honour identical semantics.
        registry = getattr(tools_subsystem, "_registry", None)
        get_tool = getattr(registry, "get_tool", None) if registry is not None else None
        if get_tool is None:
            return False
        try:
            defn = get_tool(agent_id, tool_info.name)
        except Exception:  # noqa: BLE001 — defensive; stub registries vary
            return False
        requires = getattr(defn, "requires", None) or []
        return "workflows" in list(requires)

    if any(_has_workflows_requirement(t) for t in registered):
        return

    logger.warning(_WORKFLOW_TOOLS_MISSING_WARNING)


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """Daemon-worker pool detached from `_threads_queues` at shutdown.

    spec-069 Part B. The stdlib default `ThreadPoolExecutor` spawns
    NON-daemon workers; if any is blocked in a synchronous call (a
    stalled LLM/provider read is the canonical case) at interpreter
    shutdown, CPython's `_python_exit` atexit hook joins every
    registered worker — hanging `Py_Finalize →
    wait_for_thread_shutdown → ThreadHandle_join` forever.

    **Why daemon alone is insufficient** (empirical Reviewer finding):
    `concurrent.futures.thread._python_exit` is registered via
    `threading._register_atexit` and runs BEFORE Py_Finalize's
    daemon-thread skip. Its body iterates every executor's threads:

        items = list(_threads_queues.items())
        for t, q in items: q.put(None)
        for t, q in items: t.join()   # unconditional; daemon flag irrelevant

    `t.join()` blocks on a wedged worker whether or not it's a
    daemon. Repro: with the executor's workers registered into
    `_threads_queues`, the interpreter took 15s+ to exit (killed by
    timeout) with a wedged worker; with those entries removed at
    `shutdown()`, exit is sub-second.

    **Two defenses, both required:**

    1. `daemon=True` on the worker `Thread` — so if any code path
       reaches Py_Finalize with a wedged worker still alive (bypassing
       our `shutdown()` — e.g. an unhandled exception before
       `on_unmount`), Py_Finalize's daemon skip still kicks in.
    2. `_threads_queues.pop(t, None)` at `shutdown()` — so
       `_python_exit`'s join loop does not iterate our workers at all
       for the normal teardown path (where `on_unmount` runs).

    Belt AND suspenders. Defense 1 alone leaves the `_python_exit`
    hang (this bug). Defense 2 alone would leave the fallback window
    where `on_unmount` never ran.

    Implementation mirrors the stdlib
    `ThreadPoolExecutor._adjust_thread_count` verbatim except for
    `daemon=True` on the `Thread` constructor. This reaches into
    private stdlib helpers (`_worker`, `_threads_queues`) — pinned
    to Python 3.13's shape; regression-tested via the stdlib import
    at module load. We deliberately KEEP `_threads_queues[t] = ...`
    during normal operation so the stdlib's weakref-cb sentinel
    delivery still works if a worker dies unexpectedly; the pop
    happens only at `shutdown()`.
    """

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def _weakref_cb(_: Any, q: Any = self._work_queue) -> None:
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, _weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            # `_threads` is typed as `AbstractSet[Thread]` in stdlib
            # stubs but the runtime shape is a plain `set` — mutating
            # matches the stdlib's own `_adjust_thread_count`. Same
            # for `_threads_queues` (`Mapping` in stubs, mutable dict
            # at runtime).
            self._threads.add(t)  # type: ignore[attr-defined]
            _threads_queues[t] = self._work_queue  # type: ignore[index]

    def shutdown(
        self,
        wait: bool = True,  # noqa: FBT001, FBT002 — matches stdlib signature
        *,
        cancel_futures: bool = False,
    ) -> None:
        """Detach workers from `_threads_queues` before delegating.

        Load-bearing for spec-069 AC5. `_python_exit` (the
        `concurrent.futures.thread` atexit hook, registered via
        `threading._register_atexit`) iterates
        `_threads_queues.items()` at interpreter shutdown and joins
        every listed worker UNCONDITIONALLY — the daemon flag is
        checked only later by Py_Finalize, which never runs while
        `_python_exit` is blocked in `t.join()`.

        Removing our workers from `_threads_queues` here means
        `_python_exit`'s join loop does not iterate them at all,
        so a wedged worker cannot hang interpreter exit.

        Kept in sync with the stdlib name (`_threads_queues`) —
        pinned to Python 3.13's shape; regression-tested via the
        top-of-module import.
        """
        # Reach into the same private stdlib map that
        # `_adjust_thread_count` writes to. `_threads_queues` is
        # already imported at the top of this module.
        for t in list(self._threads):
            _threads_queues.pop(t, None)  # type: ignore[attr-defined]
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


class ChatApp(App[None]):
    """Textual chat surface.

    Constructor:
        session: ``ChatSessionBundle`` from ``build_session``.
        engine: The agent engine handle (``runtime.engine``).
        full_history: If ``True``, replay the full JSONL. If
            ``False`` (default), replay the tail-20 turns and mount
            a ``ScrollbackCue`` at the top when truncating.
        auto_approve: feat-126. If ``True``, every live
            tool-authorization ``ApprovalBlock`` resolves itself to
            allow-once on mount (footer ``✓ Auto-approved (once)``,
            no persisted rule) instead of waiting for a keypress.
    """

    BINDINGS = CHAT_BINDINGS

    # feat-019: reactive flag for the Esc-Esc toast window. True during
    # the 3s window after the first ``Esc`` cancelled a butler turn
    # AND ≥ 1 workflow is live. A second ``Esc`` inside the window
    # cancels the oldest workflow; past the window the flag flips back
    # to False via ``call_later`` and ``Esc`` returns to FIFO
    # semantics. Watched by :meth:`watch__esc_toast_active` which
    # pushes / clears the toast text on the :class:`StatusBar`.
    _esc_toast_active: reactive[bool] = reactive(False)  # noqa: FBT003 — Textual reactive API

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
        auto_approve: bool = False,
    ) -> None:
        # Resolve theme path per-instance so NO_COLOR toggles work
        # in tests that monkeypatch the env between constructions.
        # Textual reads `self.CSS_PATH` inside `App.__init__` (see
        # site-packages/textual/app.py: `css_path or self.CSS_PATH`), so
        # this per-instance override MUST land before `super().__init__()`.
        # `CSS_PATH` is a Textual `ClassVar`; the instance-level set is the
        # supported way to resolve the theme per-construction (NO_COLOR).
        self.CSS_PATH = _resolve_css_path()  # type: ignore[misc]  # intentional per-instance ClassVar override, read by App.__init__
        super().__init__()
        self.session = session
        self.engine = engine
        # bug-021: wire the session_store + ids into the adapter so
        # the widget→store bridge task (spawned from
        # `_handle_prompt_requested`) can append `WIRE_PROMPT_ANSWERED`
        # to the durable JSONL. Pre-bug-021 the adapter was constructed
        # bare, so the bridge silently skipped the append and the
        # `.answered` event never dispatched through the live bus —
        # Symptom 2 in bug-021.
        #
        # `getattr(...)` guards so headless / partial-bundle fixtures
        # (spec-003 replay-only paths) that construct a bare bundle
        # without a runtime still work — the adapter's constructor
        # already accepts `None` for every kwarg.
        _session_store = getattr(
            getattr(session, "runtime", None),
            "store",
            None,
        )
        _session_id = getattr(session, "session_id", None)
        _thread_id = getattr(session, "thread_id", None)
        self.event_adapter = EventAdapter(
            self,
            session_store=_session_store,
            session_id=_session_id,
            thread_id=_thread_id,
        )
        # spec-004: `InlineApprovalCallback` needs a live `PromptsImpl`
        # handle for `.choice(...)` routing; construction now defers
        # to `on_mount` where `PromptsImpl` is built with the session
        # store + thread handles the runtime carries. Populated in
        # `on_mount` before authz registration.
        self.approval_callback: InlineApprovalCallback | None = None
        self.full_history = full_history
        # feat-126: whole-session auto-approve mode. Read by the
        # adapter's `_handle_prompt_requested` live-primary mount site
        # (gated there on `kind == "choice"`) and forwarded into each
        # live `ApprovalBlock`.
        self.auto_approve = auto_approve
        # Set by the exchange worker; cleared in a finally.
        self.active_cancel: Any = None
        self.last_cancel_reason: str | None = None
        # Track the last streaming assistant widget so newer inline
        # widgets can be appended after it. Also used for the
        # cancellation UI to append `⚠ Cancelled` to the partial.
        self._last_assistant_widget: AssistantProse | None = None
        self._last_assistant_text: str = ""
        # spec-130 Step 6: turn/call-id-keyed registries of currently
        # mounted, not-yet-terminal placeholder-ish widgets. Mirrors
        # `EventAdapter._live_prompt_blocks`'s shape (a plain `dict[str,
        # Any]`, entries added on mount, removed on morph/terminal) —
        # see that dict's docstring at `event_adapter.py:634-645` for
        # the bug-110 prior art this generalizes.
        #
        # Two PARALLEL dicts (one per widget kind) rather than one
        # dict-with-a-type-discriminant: `WorkingPlaceholder` and
        # `ToolCallBlock` have different terminal-flip APIs
        # (`render_terminal(kind)` vs `render_interrupted()` — a tool
        # call's running state has no natural "error" terminal the way
        # the placeholder does), so a unified dict would still need an
        # `isinstance` branch at every consumer. This also mirrors
        # `WorkflowNodeRow`'s own `_tool_sections`/`_tool_blocks`
        # parallel-dict precedent (`workflow_node_row.py:666-667`)
        # exactly. Both dicts are swept together by
        # `_interrupt_live_registries()` below — never one without the
        # other — so they always stay consistent as a pair.
        #
        # Keyed by `self._active_turn_id` (minted fresh per
        # `_run_exchange` call, see that method) rather than any
        # existing event/call id: the turn-start `WorkingPlaceholder`
        # mount happens BEFORE the event stream yields anything, so no
        # `tool_call_id`/`call_id` exists yet at that point to key by.
        # Using one turn-scoped id for both dicts keeps a turn's
        # placeholder AND its pending tool-call block swept as a unit
        # when that turn is superseded — see `_interrupt_live_registries`.
        self._live_placeholders: dict[str, WorkingPlaceholder] = {}
        self._live_tool_call_blocks: dict[str, ToolCallBlock] = {}
        # spec-130 Step 6: id of the turn currently mounting widgets,
        # minted fresh at the top of each `_run_exchange` call. This is
        # the registries' key space — NOT cleared in `_run_exchange`'s
        # `finally` (a stale value here is harmless: it's only ever
        # used to key a NEW registry insert, and every insert site
        # guards on the widget it just mounted, not on this id's
        # freshness).
        self._active_turn_id: str | None = None
        # spec-130 Step 2/5: convenience aliases for "the current
        # turn's" placeholder / pending tool-call block. KEPT (not
        # removed) alongside the Step 6 registries — existing Step 2-5
        # code and tests read these directly (e.g.
        # `app._working_placeholder is placeholder`), and within a
        # single non-superseded turn there is exactly one live entry
        # per registry, so the alias and the registry entry are always
        # the same object. Every mount site sets both; every
        # morph/clear site clears both. The one place they can
        # legitimately diverge is immediately after
        # `_interrupt_live_registries()` sweeps an ORPHANED (superseded)
        # turn's entry that is no longer the alias's target — see that
        # method's docstring.
        self._working_placeholder: WorkingPlaceholder | None = None
        self._pending_tool_call_block: ToolCallBlock | None = None
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
        # feat-019: handle for the 3s Esc-Esc toast auto-dismiss timer.
        # Set by ``on_key`` when the first ``Esc`` cancels a butler
        # turn AND a workflow is live; cleared by the second ``Esc``
        # inside the window (which cancels the oldest workflow) or by
        # the timer callback (which flips the reactive back to False).
        self._esc_toast_timer: asyncio.TimerHandle | None = None
        # bug-020: subscription handle for the runtime-bus
        # ``UI_EVENT_TOPIC`` subscription. ``on_mount`` registers
        # ``event_adapter.handle`` on the bus so ``CardsImpl`` /
        # ``WorkflowsImpl`` publishes surface in the SAME session (not
        # only on next-boot replay); ``on_unmount`` unsubscribes via
        # this handle so a re-mount does not stack duplicate handlers
        # (D-008 aclose idempotence).
        self._ui_event_bus_sub: Any = None
        # spec-069 Part B — app-owned default `ThreadPoolExecutor` for
        # the running loop. Installed in `on_mount` and torn down in
        # `on_unmount` via `shutdown(wait=False)` so no non-daemon
        # worker (an `asyncio.to_thread` / `run_in_executor` task
        # blocked on a synchronous LLM/provider read) can wedge
        # `Py_Finalize → wait_for_thread_shutdown → ThreadHandle_join`
        # at interpreter shutdown. The wait-free shutdown does NOT
        # join the workers; combined with Part A's bounded LLM read
        # this ensures the interpreter can always exit promptly.
        # `None` if installation was skipped (rare — currently only
        # if the loop already had a non-default executor set from
        # outside the app, or the install raised).
        self._owned_executor: ThreadPoolExecutor | None = None
        # bug-067 — shutdown watchdog. Armed at the top of
        # `on_unmount`; disarmed at the bottom after every service
        # `aclose()` + owned-executor shutdown returned. If teardown
        # overruns the window (default 5s, tunable via
        # `KOLSCH_SHUTDOWN_WATCHDOG_SECONDS`) the timer's daemon
        # thread fires `_EXIT_HOOK` (`os._exit` in prod) so the
        # process exits promptly even when spec-069's defenses miss
        # a wedge (unforeseen third-party thread, hung asyncio task).
        # `_shutdown_watchdog_fired` is the disarm race guard: if
        # the timer fires the SAME tick we call `.cancel()` on it,
        # `_shutdown_watchdog_fired` remains False and the callback
        # skips the exit — a cleanly torn-down app must never be
        # force-exited.
        self._shutdown_watchdog_timer: threading.Timer | None = None
        self._shutdown_watchdog_disarmed: bool = False

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Yield the chat surface's child widgets in mount order."""
        yield VerticalScroll(id="transcript")
        with Vertical(id="bottom-stack"):
            # spec-130 step 8: ``TurnStatus`` (the bottom-docked turn
            # spinner) is retired — the in-transcript
            # ``WorkingPlaceholder`` now owns turn-scoped "working"
            # indication. ``StatusBar``'s ``turn_active`` flag (set by
            # ``set_turn_verb``, still driven by ``EventAdapter``) keeps
            # suppressing the workflow strip during an active turn
            # (feat-019, Coordinator Q1 lock) — that precedence rule
            # survives even though its companion widget is gone.
            yield StatusBar(id="status-bar")
            yield SlashPopup(id="slash-popup")
            yield Composer(id="composer")
            yield InfoBar(id="info-bar")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        """Register the approval callback and replay history if resuming."""
        # spec-069 Part B — install an app-owned default executor on
        # the running loop BEFORE any wiring that could schedule an
        # `asyncio.to_thread` / `loop.run_in_executor` task. All
        # subsequent `to_thread` callers land on this pool; at
        # `on_unmount` we call `shutdown(wait=False)` so a worker
        # blocked on a synchronous LLM/provider read cannot wedge
        # `Py_Finalize → wait_for_thread_shutdown → ThreadHandle_join`.
        # Part A bounds the LLM/network seam so the worker itself
        # unblocks quickly in practice; Part B closes the hole for any
        # third-party thread we cannot bound.
        #
        # Best-effort: if the install raises for any reason (loop
        # policy quirks, tests that pre-set an executor) we log and
        # continue — the interpreter would only re-hit the pre-Part-B
        # behaviour, not regress further.
        self._install_owned_default_executor()

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
                    # bug-020: inject the runtime bus so prompt
                    # WIRE_PROMPT_* events publish to the live
                    # adapter subscription (ApprovalBlock mounts in
                    # the SAME session).
                    bus=getattr(self.session.runtime, "bus", None),
                    # bug-114: bound in-workflow approval prompts by the
                    # configured window (None/OFF by default).
                    approval_response_seconds=getattr(
                        self.session.runtime,
                        "workflow_approval_response_seconds",
                        None,
                    ),
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
                    # bug-020: inject the runtime bus so
                    # `_publish_to_bus` reaches the live adapter
                    # subscription in the SAME session.
                    bus=getattr(self.session.runtime, "bus", None),
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
                    # bug-020: inject the runtime bus so
                    # `_emit_ui_event` publishes reach the live
                    # adapter subscription (workflow progress card
                    # updates in the SAME session).
                    bus=getattr(self.session.runtime, "bus", None),
                    # `platform.yaml:workflows.default_timeout_seconds`,
                    # populated on the runtime at bootstrap. Tools read
                    # this via `services.workflows.default_timeout_seconds`.
                    default_timeout_seconds=getattr(
                        self.session.runtime,
                        "workflow_default_timeout_seconds",
                        300,
                    ),
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
                prompts=(prompts_impl if pre_set_prompts is None else existing.prompts),
                workflows=(
                    workflows_impl if pre_set_workflows is None else existing.workflows
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
            active_prompts,
            "resolve_pending",
        ):
            self.event_adapter.register_prompts_impl(active_prompts)

        # spec-008 / spec-068 Part B: wire the ``Cards`` handle into
        # the adapter so the boot-time interruption notice from
        # ``stage_replay_prompt_outcomes`` (the render-only detector
        # for interrupted prompt rows on replay) fires through the
        # same ``Cards.notice`` seam every other notice uses. The
        # durable side of the pre-spec-068 ``_scan_dangling_prompts``
        # helper moved into the engine's resume reconciliation; only
        # the UI-facing notice remains renderer-side.
        active_cards = (
            self._service_registry.cards if self._service_registry is not None else None
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
            active_workflows,
            "emit_awaiting_approval",
        ):
            self.event_adapter.register_workflows_impl(active_workflows)

            # bug-128: wire a run_id → pending-prompt_id lookup into
            # `WorkflowsImpl` so `cancel(run_id)` can find the in-flight
            # approval prompt bound to a run and drive
            # `WIRE_PROMPT_CANCELLED` — unwinding a node parked on a bare
            # `await prompts.choice(...)` future the cancel token can't
            # wake. The (prompt_id → run_id) map lives on the adapter;
            # `PromptsImpl` is serial (≤1 pending prompt), so matching the
            # single pending prompt's owner against the target run_id
            # suffices. No-op when the impls lack the hooks (headless).
            if hasattr(active_workflows, "set_pending_prompt_lookup") and (
                active_prompts is not None
                and hasattr(active_prompts, "pending_prompt_id")
            ):

                def _pending_prompt_for_run(
                    run_id: Any,
                    _prompts: Any = active_prompts,
                ) -> str | None:
                    pid = _prompts.pending_prompt_id
                    if pid is None:
                        return None
                    owner = self.event_adapter.run_id_for_prompt(str(pid))
                    return str(pid) if owner == str(run_id) else None

                active_workflows.set_pending_prompt_lookup(_pending_prompt_for_run)

        # bug-020: thread the finalised service registry into the
        # slash-handler `ChatSessionState` so `/workflow` can reach
        # `cards.open` + `workflows.start` without reconstructing
        # anything. Handlers guard on `state.service_registry is None`
        # for headless slash tests that construct a bare state.
        if self._chat_state is not None:
            self._chat_state.service_registry = self._service_registry

        # bug-020: inject the `ChatServiceRegistry` into the runtime's
        # `ToolsSubsystem` so `invoke_tool` forwards it into
        # `dispatch_tool` as `chat_services=...`. Without this,
        # `_populate_service_fields` short-circuits, every service
        # field on `ToolContext` stays `None`, and butler workflow
        # tools like `run_workflow` raise `ToolNotAvailable` on
        # `require_services(["cards", "workflows"])` — the model then
        # falls back to raw `bash`/`read_file`/`write_file`.
        try:
            tools_sub = getattr(
                self.session.runtime,
                "tools_subsystem",
                None,
            )
            if tools_sub is not None and hasattr(
                tools_sub,
                "set_chat_services",
            ):
                tools_sub.set_chat_services(self._service_registry)
        except Exception:  # noqa: BLE001 — headless fixtures may lack the subsystem
            _LOGGER.debug(
                "ChatApp.on_mount: tools_subsystem chat_services wiring skipped",
                exc_info=True,
            )

        # bug-024: guardrail. If `WorkflowsImpl` is live in the
        # service registry but the mounted agent's declared tool
        # set contains no `run_workflow` / `workflow_status` /
        # `cancel_workflow` ref, the model will fall back to
        # `bash kolsch workflows run` when asked to invoke a
        # workflow. Read the declared refs off the resolved config
        # so the check fires at mount (BEFORE the engine's
        # per-exchange `_register_tools_with_subsystem`); the
        # runtime-registry fallback in
        # `_warn_if_workflow_tools_missing` runs later if config
        # inspection isn't available.
        _warn_if_workflow_tools_missing(
            logger=_LOGGER,
            tools_subsystem=getattr(
                self.session.runtime,
                "tools_subsystem",
                None,
            ),
            agent_id=getattr(self.session.config, "agent_id", None),
            workflows_impl=active_workflows,
            declared_refs=_declared_tool_refs(self.session.config),
        )

        # bug-020: subscribe the event adapter's `handle` to the
        # runtime bus on `UI_EVENT_TOPIC` so every UI_EVENT
        # `CardsImpl` / `WorkflowsImpl` publishes reaches the live
        # adapter in the SAME session (not only on next-boot replay).
        # Store the subscription handle on `self._ui_event_bus_sub`
        # so `on_unmount` can unwind cleanly (D-008). Skipped when
        # the runtime has no bus (headless fixtures).
        try:
            bus = getattr(self.session.runtime, "bus", None)
            if bus is not None and hasattr(bus, "subscribe"):
                self._ui_event_bus_sub = await bus.subscribe(
                    UI_EVENT_TOPIC,
                    self.event_adapter.handle,
                )
        except Exception:
            _LOGGER.exception(
                "ChatApp.on_mount: UI-event bus subscription failed",
            )

        # spec-016: construct the WorkflowLayoutManager so multi-workflow
        # cards reorder into the two-group layout with the divider. The
        # manager needs a live `#transcript` VerticalScroll and the
        # adapter's `_card_widgets` map to resolve card widgets. Skip
        # when either is unavailable (headless fixtures) — the notify
        # hook on WorkflowsImpl silently no-ops without a manager.
        if active_workflows is not None and hasattr(
            active_workflows,
            "register_layout_manager",
        ):
            try:
                from cli.chat.services.workflow_layout import (  # noqa: PLC0415
                    WorkflowLayoutManager,
                )

                transcript = self.query_one("#transcript", VerticalScroll)
                registry = getattr(
                    active_workflows,
                    "_workflow_registry",
                    None,
                )
                card_widgets = getattr(
                    self.event_adapter,
                    "_card_widgets",
                    None,
                )
                if registry is not None and card_widgets is not None:
                    layout_manager = WorkflowLayoutManager(
                        transcript=transcript,
                        # mypy can't infer a lambda whose default-arg
                        # capture value is `Any` (the `getattr(..., None)`
                        # results); the default-arg is a late-binding
                        # idiom over vars that are never rebound here.
                        get_records=lambda reg=registry: list(reg.values()),  # type: ignore[misc]
                        get_card_widget=(lambda cid, cw=card_widgets: cw.get(cid)),  # type: ignore[misc]
                    )
                    active_workflows.register_layout_manager(layout_manager)
                    self._workflow_layout_manager = layout_manager
                    # feat-019: register the StatusBar handle so the
                    # layout manager's `_flush_reorder` pushes the
                    # live-workflow strip on every tick.
                    try:
                        status_bar = self.query_one("#status-bar", StatusBar)
                        layout_manager.register_status_bar(status_bar)
                    except Exception:  # noqa: BLE001 — headless / unmounted
                        _LOGGER.debug(
                            "ChatApp.on_mount: StatusBar wire skipped",
                            exc_info=True,
                        )
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
                self,
                active_prompts,
            )
            # Register the approval callback with the runtime's authz
            # provider so tool checks with `prompt` effects surface as
            # inline ApprovalBlock widgets (via the adapter's routing).
            authz = self.session.runtime.authz
            if isinstance(authz, RuleBasedAuthorizationProvider):
                # bug-025 (closes bug-022): wire the `on_rule_added`
                # hook so a Rule the user builds via `a`/`d` in the
                # ApprovalBlock survives chat exit — the hook writes
                # the updated rules list back to `platform.yaml` via
                # the same `cli.permissions._write_back` helper the
                # `kolsch permissions add` slash uses. Without this
                # wire-up the (effect, Rule) tuple branch at
                # `src/providers/authz.py:1106` fires (in-memory
                # insert works) but the rule vanishes on chat exit.
                # The legacy modal path (`src/cli/chat/_legacy.py:922`)
                # already wired this hook; the modern Textual
                # ChatApp lost it during the spec-004 refactor.
                from cli.permissions import (  # noqa: PLC0415
                    _write_back as _persist_authz_rules,
                )

                yaml_path = self.session.root / "platform.yaml"

                def _on_rule_added(rules: list[Any]) -> None:
                    """Atomically rewrite `platform.yaml` with the new rules list.

                    Called by the authz provider inside its persist
                    branch (see ``src/providers/authz.py:1117-1127``)
                    AFTER the in-memory insert completes. Failures
                    are swallowed by the provider — a bad write
                    means the rule vanishes on chat exit but the
                    current exchange still sees it.
                    """
                    _persist_authz_rules(yaml_path, rules)

                authz.register_prompt_callback(
                    self.approval_callback,
                    on_rule_added=_on_rule_added,
                )

        # feat-015: run the in-process orphan sweep BEFORE replay so
        # any synthetic `workflow.run.aborted` events land in the run
        # JSONLs before `EventAdapter` picks them up. Subprocess-hosted
        # runs are UNTOUCHED — they use the PID-based sweep at
        # `workflows.orphan_sweep.sweep`, which runs on CLI dispatch.
        # Best-effort: a failing sweep (headless fixtures without a
        # real run store, permission errors) is logged and swallowed.
        if active_workflows is not None and hasattr(
            active_workflows,
            "sweep_orphans",
        ):
            try:
                await active_workflows.sweep_orphans()
                if hasattr(active_workflows, "fire_orphan_notice"):
                    active_workflows.fire_orphan_notice()
            except Exception:
                _LOGGER.exception(
                    "ChatApp.on_mount: workflow orphan sweep raised",
                )

        # Replay history (tail-20 by default) if this is a resume.
        if self.session.is_resume:
            await self._replay_history()

        # bug-058 Part B + qol-061: adopt Textual's ``Widget.anchor()``
        # primitive so the transcript stays pinned to the bottom until
        # the user scrolls away (and auto-releases on user scroll).
        # Replaces the ~13 per-mount ``scroll_end(animate=False)`` calls
        # that used to hand-roll the same behaviour — see
        # ``widget.py:769``. qol-061: hoisted BEFORE the eager compact
        # await so mounts during the summariser hang (up to 30-90s on
        # the whale session) land inside an anchored transcript and
        # follow the tail. Textual's built-in ``_check_anchor``
        # (``widget.py:792-799``) handles scroll-back-to-tail re-engage
        # natively — no subclass required.
        # headless fixtures may lack #transcript
        with contextlib.suppress(Exception):
            transcript = self.query_one("#transcript", VerticalScroll)
            transcript.anchor()

        # feat-054 Phase 5 — session-open eager compaction. Fire BEFORE
        # the user's first post-resume message so the summariser delay
        # is absorbed into the resume UX. Fresh sessions no-op via the
        # empty-events guard in the engine method. The composer is
        # frozen for the duration so the user can't type ahead of an
        # unstable context state.
        await self._maybe_eager_compact_on_open()

        # Focus the composer so the user can start typing.
        with contextlib.suppress(Exception):
            self.query_one(Composer).focus()

    async def _maybe_eager_compact_on_open(self) -> None:
        """Run the feat-054 Phase 5 session-open eager compaction check.

        Wraps :meth:`AgentEngine.maybe_compact_thread_eagerly` with the
        chat-side UX:

        - Freezes the composer while the check runs.
        - Mounts a progress notice at the top of the transcript if the
          check triggers a summariser call.
        - Mounts a warning notice if the ladder hard-fails (tier 3).

        No notice, no LLM call when the thread is below threshold —
        the engine method's fast-path returns ``None`` before touching
        the summariser.
        """
        engine = getattr(self, "engine", None)
        maybe = getattr(engine, "maybe_compact_thread_eagerly", None)
        if maybe is None:
            return
        # Freeze the composer. Direct `.disabled` toggle keeps the
        # semantic distinct from `awaiting_approval` (which routes
        # approvals, not "engine-busy").
        composer: Composer | None = None
        try:
            composer = self.query_one(Composer)
            composer.disabled = True
        except Exception:  # noqa: BLE001 — headless fixtures may lack a mounted composer
            composer = None

        # Read the runtime-declared context length to decide whether
        # to bother mounting the "compacting…" notice. We only mount
        # after the engine tells us it needs to compact (via a fired
        # CONTEXT_COMPACTED event on the bus — the adapter mounts its
        # own notice). Between the check and the emit we don't yet
        # know if compaction will fire, so we emit a lightweight
        # in-progress notice up front and let the event adapter's
        # standard handler mount its notice afterwards.
        notice_widget: Any | None = None
        try:
            transcript = self.query_one("#transcript", VerticalScroll)
        except Exception:  # noqa: BLE001
            transcript = None
        # Determine whether we should mount the "compacting…" progress
        # notice. Cheap early estimate: only mount if the thread has
        # ANY events (fresh session has none — no delay to advertise).
        should_mount_progress = False
        try:
            has_events = False
            async for _ in self.session.runtime.store.read_path_to_leaf(
                self.session.thread_id,
            ):
                has_events = True
                break
            should_mount_progress = has_events and self.session.is_resume
        except Exception:  # noqa: BLE001
            should_mount_progress = False

        if should_mount_progress and transcript is not None:
            try:
                from cli.chat.widgets.compaction_block import (  # noqa: PLC0415
                    CompactionBlock,
                )

                notice_widget = CompactionBlock(
                    state="running",
                    title_text="● Compacting resumed session…",
                    body_text="Resumed session exceeds context budget; summarising…",
                )
                await transcript.mount(notice_widget)
                # Hand off to the event adapter so the CONTEXT_COMPACTED
                # / _FAILED handler transitions this widget in place.
                self.event_adapter._compaction_widgets["pending"] = notice_widget
                # The 'finally' clean-up below removes the widget; we
                # skip that because the transition handler owns it now.
                notice_widget = None
            except Exception:  # noqa: BLE001 — best-effort
                notice_widget = None

        try:
            await maybe(
                thread_id=self.session.thread_id,
                spec=self.session.config.model,
            )
        except Exception as exc:  # noqa: BLE001 — hard-fail path
            # Tier 3 uncompactable or other unexpected error. Mount a
            # warning notice; do NOT hard-fail the chat boot.
            _LOGGER.warning(
                "ChatApp._maybe_eager_compact_on_open: eager compaction raised %s",
                type(exc).__name__,
                exc_info=True,
            )
            if transcript is not None:
                with contextlib.suppress(Exception):
                    from cli.chat.widgets.notice import (  # noqa: PLC0415
                        NoticeWidget,
                    )

                    await transcript.mount(
                        NoticeWidget(
                            text=(
                                "↳ eager compaction failed — the first "
                                "message may take longer while the "
                                "engine falls back."
                            ),
                            kind="warning",
                        )
                    )
        finally:
            # Remove the transient "compacting…" progress notice —
            # the event adapter's standard handler mounted the real
            # CONTEXT_COMPACTED notice via bus publish.
            if notice_widget is not None:
                with contextlib.suppress(Exception):
                    await notice_widget.remove()
            # Re-enable the composer regardless of outcome.
            if composer is not None:
                with contextlib.suppress(Exception):
                    composer.disabled = False

    def _install_owned_default_executor(self) -> None:
        """Install an app-owned daemon-thread executor on the loop.

        spec-069 Part B. Called at the top of `on_mount` (before any
        wiring that could schedule `asyncio.to_thread` /
        `loop.run_in_executor`) so every subsequent worker lands on
        our owned pool — one whose shutdown detaches its workers
        from `concurrent.futures.thread._threads_queues`, so
        `_python_exit`'s atexit join loop cannot hang on a wedged
        worker at interpreter shutdown.

        Sizing follows the stdlib default (`min(32, cpu_count + 4)`)
        so we don't shrink the pool from what upstream picks. The
        `thread_name_prefix` makes workers easy to spot in a
        `sample`/`py-spy` dump.

        The safety property has two defenses (see
        :class:`_DaemonThreadPoolExecutor` docstring): (1) daemon
        workers so Py_Finalize skips them if `on_unmount` never ran;
        (2) `_threads_queues` cleanup at `shutdown()` so
        `_python_exit`'s unconditional `t.join()` loop does not
        iterate them at all on the normal teardown path. Empirical
        Reviewer finding (2026-07-14): (1) alone leaves an 8+ second
        interpreter-exit hang because `_python_exit` runs BEFORE
        Py_Finalize's daemon skip.

        Best-effort: any failure is logged and left as a no-op —
        `_owned_executor` stays `None`, the loop keeps its stdlib
        default (pre-Part-B non-daemon behaviour, i.e. no regression
        vs. pre-spec-069 — Part A's bounded LLM read already helps).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # `on_mount` runs on the loop that drives the App; a
            # missing loop here would mean the App is being mounted
            # outside `run_async` / `run_test`, which we don't
            # support. Log defensively rather than raise.
            _LOGGER.debug(
                "ChatApp.on_mount: no running loop; owned executor skipped",
            )
            return
        try:
            max_workers = min(32, (os.cpu_count() or 1) + 4)
            pool = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="kolsch-exec-",
            )
            loop.set_default_executor(pool)
        except Exception:  # noqa: BLE001 — best-effort install
            _LOGGER.debug(
                "ChatApp.on_mount: owned default executor install skipped",
                exc_info=True,
            )
            return
        self._owned_executor = pool

    def _arm_shutdown_watchdog(self) -> None:
        """Arm the bug-067 shutdown watchdog.

        Called at the top of `on_unmount`. If graceful teardown does
        not disarm this timer within the configured window, the
        timer's daemon thread invokes `_EXIT_HOOK` (production:
        `os._exit(1)`) to force-exit the process.

        Idempotent: a second arm before disarm is a no-op (the first
        timer keeps running).

        No-op when the environment disables the watchdog
        (`KOLSCH_SHUTDOWN_WATCHDOG_SECONDS` set to blank / `0` /
        negative) — restores pre-bug-067 behaviour, where a wedged
        teardown can hang forever. Also no-op when the timer library
        raises (unlikely — `threading.Timer` is pure Python).
        """
        if self._shutdown_watchdog_timer is not None:
            # Already armed. Second arm is a no-op — the first timer
            # is still running against the same disarm signal.
            return
        window = _read_shutdown_watchdog_seconds()
        if window is None:
            _LOGGER.debug(
                "ChatApp._arm_shutdown_watchdog: disabled via env; skipping",
            )
            return
        self._shutdown_watchdog_disarmed = False
        try:
            timer = threading.Timer(window, self._on_shutdown_watchdog_fire)
            timer.daemon = True
            timer.name = "kolsch-shutdown-watchdog"
            timer.start()
        except Exception:  # noqa: BLE001 — best-effort backstop
            _LOGGER.debug(
                "ChatApp._arm_shutdown_watchdog: timer install skipped",
                exc_info=True,
            )
            return
        self._shutdown_watchdog_timer = timer

    def _disarm_shutdown_watchdog(self) -> None:
        """Disarm the bug-067 shutdown watchdog.

        Called at the end of `on_unmount` after every `aclose()` and
        the owned-executor shutdown returned. Sets the "disarmed"
        flag BEFORE cancelling the timer so a race — the timer fires
        the same tick we call `.cancel()` — is caught by the
        callback and short-circuits the exit.

        Idempotent and safe when the watchdog was never armed
        (headless CLI, tests that don't invoke `on_unmount`).
        """
        self._shutdown_watchdog_disarmed = True
        timer = self._shutdown_watchdog_timer
        if timer is None:
            return
        try:
            timer.cancel()
        except Exception:  # noqa: BLE001 — best-effort disarm
            _LOGGER.debug(
                "ChatApp._disarm_shutdown_watchdog: cancel skipped",
                exc_info=True,
            )
        finally:
            self._shutdown_watchdog_timer = None

    def _on_shutdown_watchdog_fire(self) -> None:
        """Watchdog expiry callback (runs on the Timer's daemon thread).

        DELIBERATELY minimal work:

        * Checks the "disarmed" flag first — a cleanly torn-down app
          must never be force-exited (AC2). If teardown finished
          between the timer's alarm and this callback's entry, the
          flag is True and we return without calling the exit hook.
        * If we do proceed, calls `_EXIT_HOOK(1)` (production
          `os._exit(1)` — non-zero to signal abnormal exit; tests
          install a spy).

        The exit hook must NOT attempt a graceful sqlite
        `conn.close()` / WAL checkpoint (AC3) — the spec's
        Technical Context flags that as the exact step that could
        hang. It must NOT `join()` non-daemon workers for the same
        reason. Store contract makes `os._exit` safe: JSONL append
        is `O_APPEND + flock + fsync` (recoverable on next read via
        `recover_partial_final_line`); sqlite is WAL autocommit so
        every statement is already durable — WAL recovery handles
        an abrupt exit cleanly (AC4).
        """
        if self._shutdown_watchdog_disarmed:
            return
        _LOGGER.warning(
            "ChatApp shutdown watchdog fired — teardown exceeded "
            "the bounded window; force-exiting via os._exit(1) to "
            "avoid a Py_Finalize hang.",
        )
        try:
            _EXIT_HOOK(1)
        except Exception:  # noqa: BLE001 — last-resort backstop
            # The exit hook raised. Nothing else we can meaningfully
            # do from a daemon thread — swallow so the logging above
            # is preserved.
            _LOGGER.debug(
                "ChatApp shutdown watchdog: exit hook raised",
                exc_info=True,
            )

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

        spec-069 Part B: AFTER the `aclose()` chain (services may
        schedule `to_thread` work during their own shutdown), tear
        down the app-owned default executor with
        `shutdown(wait=False)`. The wait-free shutdown does NOT join
        workers — combined with Part A's bounded LLM read this
        guarantees no non-daemon worker can block `Py_Finalize`.
        Skipped when no executor was installed (`_owned_executor is
        None`, e.g. headless fixtures that never called `on_mount`).

        bug-067: arm the shutdown watchdog at the TOP of this method,
        disarm at the BOTTOM (in `finally:` so an exception in
        teardown still disarms — otherwise a raised teardown would
        both surface the error AND get force-exited). The watchdog
        is the last-resort backstop for any wedge spec-069 misses
        (unforeseen third-party thread, hung asyncio task); when
        spec-069's defenses hold — as they do in every existing
        test in `test_shutdown_owned_executor.py` — teardown
        completes in milliseconds and disarms the watchdog well
        before it fires.
        """
        self._arm_shutdown_watchdog()
        try:
            await self._on_unmount_body()
        finally:
            self._disarm_shutdown_watchdog()

    async def _on_unmount_body(self) -> None:
        """The pre-bug-067 body of `on_unmount`, wrapped by arm/disarm.

        Split so the arm/disarm bracket around it is a single trivial
        `try/finally` — easier to reason about than sprinkling
        disarm calls through every early-return / except branch.
        """
        authz = self.session.runtime.authz
        if isinstance(authz, RuleBasedAuthorizationProvider):
            with contextlib.suppress(Exception):
                authz.clear_prompt_callback()

        # bug-020: unsubscribe the UI-event bus handler BEFORE the
        # service registry tear-down so any late-fired publish from
        # a not-yet-cancelled drain worker does not re-enter the
        # torn-down adapter. Idempotent on unknown handles (see
        # `InProcessEventBus.unsubscribe`). Also detach the tools
        # subsystem's `chat_services` sidecar so a subsequent process
        # reboot of the same runtime does not carry a stale registry
        # into a fresh `ChatApp` (D-008 unwind contract).
        if self._ui_event_bus_sub is not None:
            try:
                bus = getattr(self.session.runtime, "bus", None)
                if bus is not None and hasattr(bus, "unsubscribe"):
                    await bus.unsubscribe(self._ui_event_bus_sub)
            except Exception:
                _LOGGER.exception(
                    "ChatApp.on_unmount: UI-event bus unsubscribe raised",
                )
            finally:
                self._ui_event_bus_sub = None
        try:
            tools_sub = getattr(
                self.session.runtime,
                "tools_subsystem",
                None,
            )
            if tools_sub is not None and hasattr(
                tools_sub,
                "set_chat_services",
            ):
                tools_sub.set_chat_services(None)
        except Exception:  # noqa: BLE001 — teardown best-effort
            _LOGGER.debug(
                "ChatApp.on_unmount: tools_subsystem detach skipped",
                exc_info=True,
            )

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
                except Exception:
                    _LOGGER.exception(
                        "ChatApp.on_unmount: service %r aclose raised",
                        field_name,
                    )

        # spec-069 Part B — retire the app-owned daemon-thread pool
        # LAST, after every service `aclose()` above (some of which
        # may schedule their own `to_thread` work during teardown).
        #
        # We do TWO things here, in this order:
        #
        # 1. Install a fresh stdlib default executor on the loop so
        #    any post-teardown `asyncio.to_thread` call still works
        #    (existing tests routinely close SQLite backends and
        #    other resources in a `finally:` block AFTER `run_test`
        #    exits — those closes use `to_thread`, and would raise
        #    `RuntimeError: cannot schedule new futures after
        #    shutdown` if we simply shut ours down). This swap is
        #    also load-bearing for AC5 in production: asyncio's own
        #    `shutdown_default_executor` (called at loop close) does
        #    `default_executor.shutdown(wait=True)`, which would join
        #    any wedged worker. Swapping in a fresh empty pool means
        #    asyncio joins THAT (idle → returns immediately), never
        #    ours.
        # 2. Shutdown OUR pool with `wait=False, cancel_futures=True`.
        #    Our overridden `shutdown` also detaches workers from
        #    `_threads_queues` so `_python_exit`'s atexit join loop
        #    does not iterate them. `wait=False` is load-bearing: even
        #    with the `_threads_queues` cleanup, `wait=True` here
        #    would still join our own `self._threads` inside
        #    `super().shutdown` — recreating the hang on a wedged
        #    worker. See `_DaemonThreadPoolExecutor` docstring for
        #    the full two-defenses rationale.
        if self._owned_executor is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.set_default_executor(
                    ThreadPoolExecutor(thread_name_prefix="kolsch-exec-post-"),
                )
            except Exception:  # noqa: BLE001 — best-effort loop swap
                _LOGGER.debug(
                    "ChatApp.on_unmount: default-executor swap skipped",
                    exc_info=True,
                )
            try:
                self._owned_executor.shutdown(
                    wait=False,
                    cancel_futures=True,
                )
            except Exception:
                _LOGGER.exception(
                    "ChatApp.on_unmount: owned executor shutdown raised",
                )
            finally:
                self._owned_executor = None

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
        # bug-056: stage a point-in-time snapshot of the slice's
        # `.requested → outcome` map BEFORE dispatch. `_handle_prompt_requested`
        # consults it so historical prompts render terminal (no active
        # widget, no bridge, composer stays usable) while genuinely
        # live post-resume prompts arriving on the live bus AFTER this
        # loop finishes have no entry and take the interactive path
        # (AC #13).
        self.event_adapter.stage_replay_prompt_outcomes(selected)
        # bug-058 Part A: batch the per-event dispatch so N per-widget
        # layout passes collapse into one layout + one paint on exit.
        # `VerticalScroll.batch()` is `Widget.batch()` (widget.py:4364);
        # it composes the widget's mount lock with `App.batch_update()`,
        # which is safe with sub-container mounts (WorkflowCard →
        # WorkflowNodeRow inside `#transcript`) — Textual doesn't require
        # the batch to be hoisted to a common ancestor.
        async with transcript.batch():
            for event in selected:
                try:
                    await self.event_adapter.handle(event)
                except Exception:  # noqa: BLE001, S112 — replay is best-effort; a bad event shouldn't crash the chat
                    # Replay is best-effort; a bad event shouldn't crash
                    # the chat. Continue.
                    continue
        # bug-056: safety recompute — after replay every terminal block
        # is mounted `is_resolved=True`, so this un-freezes the composer
        # unconditionally when no live prompt is in flight (AC #2).
        self.recompute_awaiting_approval()

        # spec-016 AC #10: at end of chronological replay, flush the
        # layout manager once so multi-workflow cards land in canonical
        # order (terminal successes at top by `terminal_at` ASC, failed
        # pinned at boundary, in-progress by `started_at` ASC). Ordering
        # is deterministic because `terminal_at` + `started_at` are
        # durable fields from spec-002 + spec-012.
        if self._workflow_layout_manager is not None:
            try:
                await self._workflow_layout_manager._flush_reorder()
            except Exception:
                _LOGGER.exception(
                    "ChatApp._replay_history: layout flush raised",
                )

        # Land the viewport at the tail after replay. Deferred to
        # after_refresh so widget layouts have settled (Collapsibles
        # + nested widgets change height post-mount).
        def _scroll_tail() -> None:
            transcript.scroll_end(animate=False)

        self.call_after_refresh(_scroll_tail)

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

        spec-130 Step 2 (amended post-ship): mounts a
        ``WorkingPlaceholder`` into ``#transcript`` right after the
        ``USER_MESSAGE`` event is handled — not before ``event_stream``
        iteration begins, which is what the original design did and
        which reliably landed the placeholder *above* the user's own
        just-submitted message (``UserChatbox`` only mounts in
        response to ``USER_MESSAGE``, never synchronously at submit
        time — see ``on_composer_submitted``'s docstring). The
        ``not placeholder_mounted and event.type == EventType.USER_MESSAGE``
        gate is per-event, not an index-0 assumption, so it correctly
        waits through any preflight events that may precede
        ``USER_MESSAGE`` (``CONFIG_SET`` on every fresh session,
        ``AGENT_TEMPLATE_WARNING``, synthetic force-skill/reconciliation
        events). The reference is stashed on ``self._working_placeholder``
        so later steps (morph-on-content, cancel/error terminal states)
        can find and replace/flip it.

        spec-130 Step 6: mints a fresh ``turn_id`` and registers it as
        ``self._active_turn_id`` BEFORE mounting the placeholder, so
        every mount call site this turn reaches (here and in
        ``EventAdapter``'s ``TOOL_CALLED``/streaming-token handlers)
        keys its registry entry by the same id. Two cancellation shapes
        can end this method without ever reaching a content-morph:

        - **Cooperative** (Esc / ``request_cancel``): the engine polls
          ``cancel.is_cancelled()`` at safe points and raises
          ``ExchangeCancelled`` from inside ``event_stream`` — a plain
          ``Exception``, caught below. Previously this fell into the
          generic ``except Exception`` branch and mounted an unrelated
          "[exchange failed — ...]" notice, leaving this turn's
          placeholder (if still mounted) orphaned. Now handled
          explicitly: sweep the registries (flips this turn's
          placeholder/pending tool-call block to interrupted) instead
          of mounting the generic failure notice.
        - **Exclusive-worker preemption** (a second turn started via
          ``on_composer_submitted`` -> ``self._run_exchange(...)``
          while this one is still running): Textual's
          ``@work(exclusive=True)`` cancels THIS worker outright —
          confirmed by direct reproduction against this repo's actual
          Textual version: a real ``asyncio.CancelledError`` is raised
          at whatever ``await`` this turn was parked on, bypassing the
          inner ``except Exception`` entirely. Caught by the outer
          ``except asyncio.CancelledError`` below, which sweeps the
          registries (interrupting the turn Textual just killed) and
          then re-raises — swallowing ``CancelledError`` would corrupt
          asyncio's own cancellation bookkeeping and Textual's worker
          state tracking, so it must always propagate.

        Either shape reaches the same sweep helper,
        ``_interrupt_live_registries()`` — see its docstring for why
        the sweep is over EVERY live entry, not just this turn's: by
        the time cancellation is observed, a *different* turn may
        already be active, and this dead turn's placeholder is the one
        that actually needs flipping, not whatever the fresh
        ``self._working_placeholder`` alias now points at.

        spec-130 Step 7: the inner ``try`` (feeding the ``except
        ExchangeCancelled`` / generic ``except Exception`` branches
        below) now starts BEFORE ``mount_working_placeholder()``,
        not just around the ``async for event in event_stream`` loop.
        Previously a mount-time failure (the placeholder mount itself
        raising) sat outside that inner ``try`` entirely, so it skipped
        straight past ``mount_exchange_failed()``'s transcript-visible
        failure notice and propagated to Textual's default worker
        exception handling instead — silent from the transcript's
        point of view. Widening the inner ``try`` to cover the mount
        call gives it the exact same treatment as an event-stream-time
        failure: a real error there is now caught by the generic
        ``except Exception`` branch below, same as any other exchange
        failure. This does NOT change the ``ExchangeCancelled`` /
        ``asyncio.CancelledError`` handling: ``mount_working_placeholder()``
        can itself be cancelled mid-``await`` by an exclusive-worker
        preemption exactly as Step 6 already documented for the
        mount-call sites inside ``app.py`` — that still surfaces as
        ``asyncio.CancelledError``, which the inner ``try`` doesn't
        catch (no matching ``except`` clause here), so it still
        propagates straight to the outer ``except asyncio.CancelledError``
        below, unchanged from before this step. ``ExchangeCancelled``
        itself cannot be raised by the mount call (it's an engine-side
        exception type, only ever raised from inside ``event_stream``
        iteration) — including the mount inside this inner ``try``
        does not create any new way to reach that branch.

        spec-130 Step 7: a real (non-cancellation) error now also
        sweeps the live registries — via ``_fail_live_registries()``,
        the error-flavored sibling of ``_interrupt_live_registries()``
        — before mounting the generic "[exchange failed — ...]"
        notice. Without this, an error occurring before any content
        exists for the turn (including a mount-time failure, per the
        above) left this turn's ``WorkingPlaceholder``/pending
        ``ToolCallBlock`` mounted, non-terminal, and orphaned — spinning
        forever underneath the new failure notice. See
        ``_fail_live_registries()`` for why this is a separate method
        from ``_interrupt_live_registries()`` rather than the same
        method taking a ``kind`` argument.
        """
        from cli.chat.cancel import new_cancel_token  # noqa: PLC0415

        cancel = new_cancel_token()
        self.active_cancel = cancel
        self.last_cancel_reason = None
        turn_id = str(uuid4())
        self._active_turn_id = turn_id
        try:
            try:
                force_skills: list[str] = []
                if (
                    self._chat_state is not None
                    and self._chat_state.pending_force_skill
                ):
                    force_skills = list(self._chat_state.pending_force_skill)
                    self._chat_state.pending_force_skill.clear()
                trigger = _build_user_trigger(user_text, force_skills)
                ctx = ExecutionContext(
                    thread_id=self.session.thread_id,
                    trigger=trigger,
                    cancellation=cancel,
                )
                event_stream = self.engine.execute_stream(self.session.config, ctx)
                placeholder_mounted = False
                async for event in event_stream:
                    await self.event_adapter.handle(event)
                    # spec-130 step 2 (amended post-ship): UserChatbox is
                    # only ever mounted in response to the engine's
                    # USER_MESSAGE event (EventAdapter._handle_user_message)
                    # -- never synchronously at submit time, so the adapter
                    # stays the single mount site shared by the live and
                    # replay paths (see on_composer_submitted's docstring).
                    # Mounting the placeholder before this loop started (the
                    # original design) landed it ABOVE the user's own
                    # message every time. USER_MESSAGE is the first event
                    # *of interest* here, not necessarily the first event
                    # overall -- preflight can emit CONFIG_SET (every
                    # fresh session), AGENT_TEMPLATE_WARNING, or synthetic
                    # force-skill/reconciliation events first, none of
                    # which mount a UserChatbox. The `not placeholder_mounted`
                    # gate is per-event, not an index-0 check, so it
                    # correctly waits through any of those before mounting
                    # right after USER_MESSAGE lands the placeholder below
                    # the just-mounted UserChatbox.
                    if not placeholder_mounted and event.type == EventType.USER_MESSAGE:
                        self._working_placeholder = (
                            await self.mount_working_placeholder()
                        )
                        placeholder_mounted = True
            except ExchangeCancelled:
                # spec-130 Step 6: cooperative cancel (Esc / ctrl+c) —
                # presentation-only; no durable-write future is touched
                # here (bug-110 precedent).
                await self._interrupt_live_registries()
            except Exception as exc:  # noqa: BLE001 — surface any exchange failure to the transcript
                # spec-130 Step 7: a real error (model/tool failure, or
                # now also a mount-time failure — see this method's
                # docstring) — flip any still-live placeholder/pending
                # tool-call block to the error terminal state before
                # mounting the generic failure notice, so nothing is
                # left orphaned and spinning underneath it.
                await self._fail_live_registries()
                await self.mount_exchange_failed(str(exc))
        except asyncio.CancelledError:
            # spec-130 Step 6: exclusive-worker hard preemption — a
            # second turn's `_run_exchange` call cancelled this one via
            # Textual's `@work(exclusive=True)`. Sweep before
            # re-raising so this (now-dead) turn's placeholder/pending
            # tool-call block don't orphan underneath the turn that
            # superseded it.
            await self._interrupt_live_registries()
            raise
        finally:
            self.active_cancel = None
            # spec-130 step 8: ``TurnStatus`` is gone, but
            # ``set_turn_verb("")`` still clears ``StatusBar.turn_active``
            # (feat-019) so the workflow strip un-suppresses after any
            # exchange path — that sync is independent of the retired
            # widget.
            with contextlib.suppress(Exception):
                await self.set_turn_verb("")

    async def _interrupt_live_registries(self) -> None:
        """spec-130 Step 6: flip EVERY live, not-yet-terminal entry to interrupted.

        Sweeps ``self._live_placeholders`` and
        ``self._live_tool_call_blocks`` — never just "the current
        turn's" entry. This matters because ``_run_exchange`` is
        ``@work(exclusive=True)``: when a second turn supersedes a
        first (``on_composer_submitted`` calling ``self._run_exchange``
        again while the first is still in flight — the same worker-
        cancellation shape ``_retire_butler_turn``'s hard-cancel
        escalation drives for ``/new``, confirmed by direct
        reproduction against this repo's actual Textual version), the
        FIRST turn's placeholder/pending tool-call block are what need
        flipping — but by the time either turn's cancel path calls this
        method, ``self._working_placeholder`` / ``self._active_turn_id``
        may already have been overwritten by the second turn's own
        ``mount_working_placeholder()`` call. Reading off the
        registries (keyed by each turn's own id, not "whichever is
        current") is what makes this correct regardless of ordering.

        spec-130 Step 7: thin wrapper around ``_sweep_live_registries("interrupted")``
        — see that method for the shared sweep-iteration logic and
        ``_fail_live_registries`` for the error-flavored sibling. Kept
        as its own named method (rather than inlining the call at both
        ``_run_exchange`` call sites) because tests spy on it by name
        (``app._interrupt_live_registries = _spy_sweep``) to confirm
        the cooperative-cancel and exclusive-worker-preemption paths
        both route through the same cancel sweep.

        Presentation-only, per the bug-110 precedent
        (``EventAdapter._handle_prompt_cancelled``): this never
        resolves a durable-write future, never touches
        ``PromptsImpl``/``WorkflowsImpl``, and never appends to the
        session JSONL. It only flips already-mounted widgets to a
        terminal visual state.
        """
        await self._sweep_live_registries("interrupted")

    async def _fail_live_registries(self) -> None:
        """spec-130 Step 7: flip EVERY live, not-yet-terminal entry to error.

        Sibling of ``_interrupt_live_registries()`` — same sweep-over-
        every-entry shape (not just "the current turn's"), same
        registry pair, same presentation-only guarantee, but for the
        "a real, un-recovered exchange error occurred" case rather than
        a cancellation: ``_run_exchange``'s generic ``except Exception``
        handler (a real model/tool error, distinct from the
        ``ExchangeCancelled``/``asyncio.CancelledError`` branches that
        route to ``_interrupt_live_registries`` instead) calls this
        before falling back to ``mount_exchange_failed()``'s notice.

        Deliberately a SEPARATE method rather than parameterizing every
        call site with a ``kind`` argument: the two call sites
        (cancel vs. error) are already textually distinct in
        ``_run_exchange`` (different ``except`` clauses), and a
        same-named-but-different-outcome method at each site reads more
        clearly there than a shared name with a kind argument threaded
        through. Both delegate to the same private
        ``_sweep_live_registries()`` so the iteration/registry-pop/
        alias-clear logic itself is written exactly once.
        """
        await self._sweep_live_registries("error")

    async def _sweep_live_registries(self, kind: TerminalKind) -> None:
        """Shared sweep body for the cancel/error registry sweeps.

        Called only by ``_interrupt_live_registries()`` (``kind ==
        "interrupted"``) and ``_fail_live_registries()`` (``kind ==
        "error"``) — see those methods for why each gets its own named
        wrapper rather than callers passing ``kind`` directly.

        Flips every live, not-yet-terminal ``WorkingPlaceholder`` to
        ``render_terminal(kind)`` and every live, not-yet-filled
        ``ToolCallBlock`` to ``render_interrupted()`` (cancel) or
        ``render_failed()`` (error) — the two widget kinds don't share
        a terminal-flip API (see the registries' own docstring at
        ``__init__`` for why they're two parallel dicts, not one), so
        this still branches on ``kind`` once per widget kind rather
        than being fully polymorphic, but the iteration, registry
        pop, and alias-clear bookkeeping — the part that actually needs
        to be written once, not duplicated per cancel/error path — is
        shared here.

        Idempotent — sweeping an already-empty pair of registries (the
        common case: most turns morph away cleanly before either
        cancellation or an error ever occurs) is a no-op. Safe to call
        from multiple teardown paths in the same turn (cooperative
        ``ExchangeCancelled``, hard ``asyncio.CancelledError``, and the
        generic ``except Exception`` error branch are mutually
        exclusive within one ``_run_exchange`` call, but nothing here
        would break if that ever changed).

        Clears the convenience aliases (``self._working_placeholder`` /
        ``self._pending_tool_call_block``) ONLY when they still point at
        a widget this sweep just flipped — a superseding turn's fresh
        alias (already re-pointed at ITS OWN new placeholder by the
        time this sweep runs — ``mount_working_placeholder()`` /
        ``mount_tool_call_running()`` set the alias and registry entry
        SYNCHRONOUSLY, before their own ``await transcript.mount(...)``,
        so there is no window where a fresh alias could still be stale
        when a DIFFERENT turn's cleanup runs) must survive the sweep
        untouched.

        Also safe to call while an entry's widget is registered but not
        yet actually attached to the DOM (a superseding turn's cancel
        can land while ``mount_working_placeholder()`` /
        ``mount_tool_call_running()`` are still suspended inside their
        own ``await transcript.mount(...)`` — see those methods'
        "review fixup" docstring notes): ``WorkingPlaceholder.render_terminal()``,
        ``ToolCallBlock.render_interrupted()``, and
        ``ToolCallBlock.render_failed()`` are all safe to call on a
        not-yet-mounted widget (verified directly against bare,
        never-mounted instances) — the widget's internal state still
        flips correctly even though there's nothing on screen yet to
        visually update.
        """
        for placeholder in self._live_placeholders.values():
            if not placeholder.is_terminal:
                with contextlib.suppress(Exception):
                    placeholder.render_terminal(kind)
            if self._working_placeholder is placeholder:
                self._working_placeholder = None
        self._live_placeholders.clear()

        for block in self._live_tool_call_blocks.values():
            with contextlib.suppress(Exception):
                if kind == "interrupted":
                    block.render_interrupted()
                else:
                    block.render_failed()
            if self._pending_tool_call_block is block:
                self._pending_tool_call_block = None
        self._live_tool_call_blocks.clear()

    # ------------------------------------------------------------------
    # Mount helpers — called by EventAdapter, InlineApprovalCallback, and
    # _run_exchange (turn-start placeholder)
    # ------------------------------------------------------------------

    async def mount_user_message(
        self,
        text: str,
        timestamp: datetime | None = None,
        *,
        event_id: str | None = None,
        content: dict[str, Any] | None = None,
    ) -> Any | None:
        """Mount a user message chatbox at the transcript root."""
        _ = event_id, content  # bug-036d/037 sink kwargs — App ignores.
        ts = timestamp if timestamp is not None else datetime.now()  # noqa: DTZ005 — local time is right for the wall-clock header
        transcript = self.query_one("#transcript", VerticalScroll)
        widget = UserChatbox(text, ts)
        await transcript.mount(widget)
        # bug-060 Part 1: return the widget so `EventAdapter` can
        # register it under the source event's id. Row sinks stay
        # ``None``-returning (their mounts are queued closures) —
        # covered widgets inside a row aren't reached by the outer
        # compaction unmount pass.
        return widget

    async def mount_working_placeholder(self) -> WorkingPlaceholder:
        """Mount a turn-start ``WorkingPlaceholder`` at the transcript root.

        spec-130 Step 2 (amended post-ship): called from inside
        ``_run_exchange``'s ``event_stream`` loop, gated per-event on
        ``not placeholder_mounted and event.type == EventType.USER_MESSAGE``
        — i.e. right after the engine's ``USER_MESSAGE`` event has been
        handled, not before the loop starts. This lands the placeholder
        inline at the exact spot the reply will occupy, directly below
        the ``UserChatbox`` that ``EventAdapter._handle_user_message``
        mounts in response to that same event. ``USER_MESSAGE`` is the
        first event *of interest* here, not necessarily the very first
        event in the stream — preflight events (``CONFIG_SET`` on every
        fresh session, ``AGENT_TEMPLATE_WARNING``, synthetic force-skill/
        reconciliation events) can precede it. The caller stashes the
        returned widget (``self._working_placeholder``) so later steps
        can find and remove/replace it once real content (streamed
        token, tool call, complete response) or a terminal outcome
        (cancel, error) arrives.

        spec-130 Step 6: also registers the placeholder into
        ``self._live_placeholders`` keyed by ``self._active_turn_id``
        (set by ``_run_exchange`` just before calling this) — this is
        the entry ``_interrupt_live_registries()`` sweeps on cancel/
        supersession. ``self._active_turn_id`` is always set by the
        time this is called (``_run_exchange`` is its only caller), but
        the guard keeps this method safe to call standalone (e.g. from
        a test) without silently mounting an unregistered placeholder.

        spec-130 Step 6 (review fixup): registers BEFORE ``await
        transcript.mount(...)``, not after. Two independent reviewers
        reproduced (under natural async scheduling, no monkeypatching)
        that a superseding turn's ``@work(exclusive=True)`` cancel can
        land while this coroutine is suspended INSIDE the mount await
        — the widget is then attached to the DOM but the post-await
        registration line never runs, leaving it a permanent orphan
        the sweep can never find. Registering first closes that window:
        if cancellation lands mid-mount-await, the registry entry
        already exists, so the sweep finds and interrupts it —
        ``WorkingPlaceholder.render_terminal()`` is safe to call on a
        widget that isn't mounted yet (its query-based updates are
        already wrapped in ``contextlib.suppress(Exception)``; verified
        directly against a never-mounted instance), so this is safe
        regardless of whether the mount ultimately completes.
        """
        transcript = self.query_one("#transcript", VerticalScroll)
        placeholder = WorkingPlaceholder()
        if self._active_turn_id is not None:
            self._live_placeholders[self._active_turn_id] = placeholder
        await transcript.mount(placeholder)
        return placeholder

    async def _remove_working_placeholder(self) -> None:
        """Unmount the turn's placeholder and deregister it, if any.

        spec-130 Step 6: shared by every morph/clear call site
        (``clear_working_placeholder``, ``mount_assistant_message``,
        ``mount_assistant_streaming_widget``, ``mount_tool_call_running``)
        so the registry pop (``self._live_placeholders``) can't drift
        out of sync with the alias clear/unmount — previously each site
        duplicated the same four lines independently. Pops by identity
        (removes whichever registry entry currently maps to THIS
        widget instance) rather than by ``self._active_turn_id``, so a
        stale/late call after the turn id has moved on still cleans up
        the correct entry.
        """
        placeholder = self._working_placeholder
        if placeholder is None:
            return
        self._working_placeholder = None
        for key, value in list(self._live_placeholders.items()):
            if value is placeholder:
                del self._live_placeholders[key]
        if placeholder.is_mounted:
            await placeholder.remove()

    def _deregister_tool_call_block(self, block: ToolCallBlock) -> None:
        """Pop ``block``'s entry from ``self._live_tool_call_blocks``, if any.

        spec-130 Step 6: companion to ``_remove_working_placeholder``'s
        registry pop, for the tool-call-block registry. Pops by
        identity (whichever key currently maps to THIS widget), not by
        ``self._active_turn_id`` — a fill/fallback call arriving after
        the turn id has moved on (e.g. this is a superseded turn's
        stale pending block) must still deregister the correct entry,
        not whatever the CURRENT turn id happens to be. Synchronous —
        unlike the placeholder's counterpart, there's no widget removal
        here (the block either gets filled in place or was never
        mounted by the running-block path to begin with).
        """
        for key, value in list(self._live_tool_call_blocks.items()):
            if value is block:
                del self._live_tool_call_blocks[key]

    async def clear_working_placeholder(self) -> None:
        """Remove the turn's placeholder without mounting a replacement.

        spec-130 Step 5 (fixup): every OTHER morph path
        (``mount_assistant_streaming_widget``, ``mount_assistant_message``,
        ``mount_tool_call_running``) removes the placeholder as a side
        effect of mounting the widget that takes its place. Card-
        mounting tool calls (``run_workflow`` and friends) have no
        such replacement here — the tool opens its own ``WorkflowCard``
        through a separate mount path (``EventAdapter._get_or_mount_card``)
        that doesn't know about ``_working_placeholder`` at all. Without
        this, a card-mounting ``TOOL_CALLED`` leaves the placeholder
        mounted, non-terminal, and orphaned forever alongside the
        eventual card — spinning with nothing left to remove it.

        spec-130 Step 6: delegates to ``_remove_working_placeholder()``
        so the registry entry is popped alongside the unmount, same as
        every other morph site.
        """
        await self._remove_working_placeholder()

    async def mount_assistant_message(
        self,
        text: str,
        *,
        event_id: str | None = None,
    ) -> Any | None:
        """Mount a completed assistant prose message at the transcript root.

        spec-130 Step 4: called for a complete, non-streamed response
        (``EventAdapter._handle_agent_message`` / a non-streaming
        ``_handle_model_response``), i.e. the reply text is already
        fully known at mount time — unlike
        ``mount_assistant_streaming_widget``, there is no later
        ``append()`` phase. If a ``WorkingPlaceholder`` is still
        mounted for this turn (``self._working_placeholder``), this is
        the morph point: remove the placeholder first, then mount
        ``AssistantProse`` with the full text already appended — one
        clean swap, never empty-then-fill. Removing before mounting
        means the prose widget becomes the new last child of
        ``#transcript`` in the placeholder's former position (mirrors
        ``mount_assistant_streaming_widget``'s Step 3 pattern). The
        reference is cleared (``None``) so later steps (cancel/error)
        don't act on an already-removed widget.

        spec-130 Step 6: the removal goes through
        ``_remove_working_placeholder()`` so the ``_live_placeholders``
        registry entry is popped in lockstep with the alias clear.
        """
        _ = event_id  # bug-036d: sink-protocol dedupe key — App ignores.
        transcript = self.query_one("#transcript", VerticalScroll)
        await self._remove_working_placeholder()
        prose = AssistantProse(
            agent_name=self.session.config.name,
            timestamp=datetime.now(),  # noqa: DTZ005 — local time is right for the wall-clock header
        )
        await transcript.mount(prose)
        await prose.append(text)
        self._last_assistant_widget = prose
        # bug-060 Part 1: return the widget for event-id registration.
        return prose

    async def mount_assistant_streaming_widget(self) -> AssistantProse:
        """Mount an empty assistant prose widget for incremental streaming.

        spec-130 Step 3: called once per streaming call (only when no
        widget exists yet for that ``call_id`` — see
        ``EventAdapter._handle_model_streaming_token``), i.e. exactly on
        the first token of a turn. If a ``WorkingPlaceholder`` is still
        mounted for this turn (``self._working_placeholder``), this is
        the morph point: remove the placeholder first, then mount
        ``AssistantProse`` — removing before mounting means the prose
        widget becomes the new last child of ``#transcript`` in the
        placeholder's former position (nothing else has been mounted
        for this turn yet at this point). The reference is cleared
        (``None``) so later steps (cancel/error) don't act on an
        already-removed widget.

        spec-130 Step 6: the removal goes through
        ``_remove_working_placeholder()`` so the ``_live_placeholders``
        registry entry is popped in lockstep with the alias clear.
        """
        transcript = self.query_one("#transcript", VerticalScroll)
        await self._remove_working_placeholder()
        prose = AssistantProse(
            agent_name=self.session.config.name,
            timestamp=datetime.now(),  # noqa: DTZ005 — local time is right for the wall-clock header
        )
        await transcript.mount(prose)
        self._last_assistant_widget = prose
        return prose

    async def mount_tool_call_running(
        self,
        tool_name: str,
        args: str,
    ) -> ToolCallBlock:
        """Mount a ``ToolCallBlock`` in a running/pending state, empty body.

        spec-130 Step 5: called from
        ``EventAdapter._handle_tool_called`` — the moment a tool call
        *starts*, not when it finishes. Mirrors
        ``WorkflowNodeRow.mount_tool_call``'s two-phase pattern
        (``workflow_node_row.py:1550-1642``): an empty body signals
        "running" the same way it does there (``ToolCallBlock``
        itself has no distinct running/failed/success state — the
        empty body plus the absence of the ``-failed`` class *is* the
        running state, matching the row's ``state = "running" if not
        body and not failed`` rule). If a ``WorkingPlaceholder`` is
        still mounted for this turn, this is a morph point: remove the
        placeholder first, then mount the running block — same
        remove-before-mount ordering ``mount_assistant_streaming_widget``
        / ``mount_assistant_message`` use, so the block lands in the
        placeholder's former transcript position.

        The returned block is stashed on
        ``self._pending_tool_call_block`` so the matching
        ``TOOL_RESULT`` (``mount_tool_call`` below) can find it and
        call ``update_body()`` in place instead of mounting a second,
        separate block.

        spec-130 Step 6: the placeholder removal goes through
        ``_remove_working_placeholder()`` (registry pop in lockstep),
        and the new running block is registered into
        ``self._live_tool_call_blocks`` keyed by ``self._active_turn_id``
        — the entry ``_interrupt_live_registries()`` sweeps on cancel/
        supersession, mirroring the placeholder registry exactly.

        spec-130 Step 6 (review fixup): registers (and sets the
        ``self._pending_tool_call_block`` alias) BEFORE ``await
        transcript.mount(...)``, not after — same ordering bug and
        same fix as ``mount_working_placeholder()``'s fixup above. A
        superseding turn's cancel landing while this coroutine is
        suspended inside the mount await must not leave the block
        attached to the DOM but unregistered/un-sweepable.
        ``ToolCallBlock.render_interrupted()`` is safe to call on a
        not-yet-mounted block (verified directly).
        """
        transcript = self.query_one("#transcript", VerticalScroll)
        await self._remove_working_placeholder()
        block = ToolCallBlock(tool_name, args, "")
        self._pending_tool_call_block = block
        if self._active_turn_id is not None:
            self._live_tool_call_blocks[self._active_turn_id] = block
        await transcript.mount(block)
        return block

    async def mount_tool_call(
        self,
        tool_name: str,
        args: str,
        body: str,
        *,
        failed: bool = False,
        recovery_hint: str | None = None,
        matched_rule: dict[str, object] | None = None,
        event_id: str | None = None,
    ) -> Any | None:
        """Mount a tool-call block at the transcript root, or fill a running one.

        spec-130 Step 5: this is the ``ChatMountSink`` Protocol method
        (``services/mount_sink.py``) — its signature is polymorphic
        (also implemented by ``WorkflowNodeRow``) and must not change.
        If ``mount_tool_call_running()`` above already mounted a
        running block for this turn (``self._pending_tool_call_block``
        is set), fill that SAME widget in place via
        ``ToolCallBlock.update_body()`` rather than constructing a
        second, separate block — this is the "fill" half of the
        two-phase morph, mirroring
        ``WorkflowNodeRow.mount_tool_call``'s transition branch.

        A ``TOOL_CALLED`` event always precedes exactly one of
        ``TOOL_RESULT`` (``failed=False``) or ``TOOL_FAILED``
        (``failed=True``) — both ``_handle_tool_result`` and
        ``_handle_tool_failed`` call this same method, so both must
        correctly consume the pending running block, not just the
        success path. When ``failed`` is True and a pending block is
        being filled: ``ToolCallBlock.set_failed_state(tool_name,
        args)`` rewrites the title to the same ``✗ {tool_name}(...)``
        form the constructor would have produced for ``failed=True``
        (title-glyph parity between the single-shot and fill paths)
        and adds the ``-failed`` CSS class (border-left rail + title
        color flip, per ``DEFAULT_CSS`` — also mirrors
        ``WorkflowNodeRow._populate_tool_call_result``'s existing
        transition precedent, which only had the class half of this).

        ``matched_rule`` (feat-041's authz-denial hint): at
        construction time ``ToolCallBlock`` appends a 2-line denial
        hint into the body text itself (``tool_call_block.py``) —
        that's plain text, so this fill path replicates the same
        composition via ``format_denial_hint`` before calling
        ``update_body()``, keeping single-shot and fill-path parity.

        ``recovery_hint`` is rendered as a separate child ``Static``
        that ``ToolCallBlock.__init__`` now ALWAYS creates (hidden/
        empty when no hint is supplied — see that widget's
        ``_apply_recovery_hint``), specifically so it can be safely
        revealed/set post-mount via ``set_recovery_hint()`` without
        mounting a NEW child into an already-live ``Collapsible``
        (unsafe — ``Collapsible``'s ``Contents`` pane composes its
        children at mount time). This fill path calls
        ``set_recovery_hint(recovery_hint)`` unconditionally — passing
        ``None`` clears/re-hides it, matching a result with no hint.

        Falls back to the original single-shot mount (construct a
        fresh block with the final body, `failed` styling baked in at
        construction as before) whenever no running block is pending
        — e.g. this sink method invoked directly outside the normal
        ``TOOL_CALLED`` -> ``TOOL_RESULT``/``TOOL_FAILED`` flow, or a
        card-mounting tool call (``mount_tool_call_running`` is
        skipped for those — see ``EventAdapter._handle_tool_called``).

        spec-130 Step 6: both branches that consume/clear
        ``self._pending_tool_call_block`` also pop the matching entry
        from ``self._live_tool_call_blocks`` (via
        ``_deregister_tool_call_block()``) — the block just reached a
        terminal-ish outcome (filled with a result, or the slot is
        simply being reset), so it must leave the registry
        ``_interrupt_live_registries()`` sweeps, the same way the
        placeholder registry is kept in lockstep by
        ``_remove_working_placeholder()``.
        """
        _ = event_id  # bug-036d: sink-protocol dedupe key — App ignores.
        pending = self._pending_tool_call_block
        if pending is not None and pending.is_mounted:
            self._pending_tool_call_block = None
            self._deregister_tool_call_block(pending)
            fill_body = body
            if failed and matched_rule:
                from cli.chat.services.authz_introspection import (  # noqa: PLC0415
                    format_denial_hint,
                )

                hint = format_denial_hint(matched_rule)
                fill_body = f"{body}\n{hint}" if body else hint
            pending.update_body(fill_body)
            pending.set_recovery_hint(recovery_hint)
            if failed:
                pending.set_failed_state(tool_name, args)
            # bug-060 Part 1: return the (same) widget for event-id
            # registration — identity matters here, not just content.
            return pending
        if pending is not None:
            self._deregister_tool_call_block(pending)
        self._pending_tool_call_block = None
        transcript = self.query_one("#transcript", VerticalScroll)
        block = ToolCallBlock(
            tool_name,
            args,
            body,
            failed=failed,
            recovery_hint=recovery_hint,
            matched_rule=matched_rule,
        )
        await transcript.mount(block)
        # bug-060 Part 1: return the widget for event-id registration.
        return block

    async def mount_notice(
        self,
        notice_id: str,
        text: str,
        *,
        level: str = "info",
        event_id: str | None = None,
    ) -> Any | None:
        """bug-036d: sink-protocol notice mount at transcript root.

        Mirrors ``_mount_transcript_notice`` but takes the sink
        protocol shape so ``EventAdapter._handle_model_fallback`` /
        ``_handle_agent_template_warning`` can call the sink without
        knowing whether the sink is the App or a workflow row.
        Idempotent per ``notice_id`` is NOT enforced here — the
        adapter's ``_applied_notice_ids`` LRU handles that upstream.

        bug-060 Part 1: returns the mounted widget so the adapter can
        register it under the source event's id.
        """
        _ = notice_id, event_id  # tracked by the adapter's LRU
        transcript = self.query_one("#transcript", VerticalScroll)
        notice = NoticeWidget(text=text, kind=level)
        await transcript.mount(notice)
        return notice

    async def _mount_transcript_notice(
        self,
        text: str,
        *,
        kind: str = "info",
    ) -> None:
        """Mount a ``NoticeWidget`` at the transcript root (internal helper).

        Links to
        [`NoticeWidget`][cli.chat.widgets.notice.NoticeWidget].
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

    async def mount_exchange_failed(self, message: str) -> Any | None:
        """Mount a red error notice reporting a failed exchange."""
        transcript = self.query_one("#transcript", VerticalScroll)
        notice = Static(f"[exchange failed — {message}]", classes="error", markup=False)
        notice.styles.color = "red"
        await transcript.mount(notice)
        # bug-060 Part 1: return the widget for event-id registration.
        return notice

    async def mount_approval_block(
        self,
        rule_id: str | None,
        action: str,
        target: str | None,
        *,
        parent_hint: str | None = None,
        terminal: tuple[bool | None, str] | None = None,
        auto_approve: bool = False,
    ) -> ApprovalBlock:
        """Mount an approval block and recompute the composer gate."""
        transcript = self.query_one("#transcript", VerticalScroll)
        block = ApprovalBlock(
            rule_id,
            action,
            target,
            parent_hint=parent_hint,
            terminal_state=terminal,
            auto_approve=auto_approve,
        )
        await transcript.mount(block)
        # bug-056: a terminal (replay) block does not gate the composer
        # and does not carry an in-flight PromptsImpl future, so skip
        # the freeze entirely and just recompute the gate over currently
        # mounted blocks. The recompute is the single source of truth
        # for the gate, replacing the pre-bug-056 single-boolean toggle.
        self.recompute_awaiting_approval()
        return block

    def recompute_awaiting_approval(self) -> None:
        """Recompute the composer gate from currently mounted approvals.

        bug-056: the composer is frozen iff **any** mounted
        `ApprovalBlock` is unresolved. Replaces the pre-bug-056
        single-boolean toggle that clobbered the gate on the first
        resolution when two live approvals were stacked (a pre-existing
        latent bug the bug-056 review lifted into scope). Also called
        on session resume so historical (terminal) blocks — which
        report `is_resolved=True` from birth — never gate the composer
        (AC #2).
        """
        try:
            blocks = list(self.query(ApprovalBlock))
        except Exception:  # noqa: BLE001 — headless fixtures may lack a mounted DOM
            blocks = []
        awaiting = any(not block.is_resolved for block in blocks)
        with contextlib.suppress(Exception):
            composer = self.query_one(Composer)
            composer.awaiting_approval = awaiting
        # spec-130 step 8: ``TurnStatus`` (and its own
        # ``awaiting_approval`` reactive, which drove the retired
        # warning-glyph slot) is gone. The ``Composer`` gate above is
        # the actual approval-freeze mechanism and is untouched.
        # bug-129: deliberate focus successor. Resolving/removing an
        # approval pane clears focus first (see
        # `ApprovalBlock._release_focus_if_inside_pane`) — without a
        # named successor, Textual would pick the nearest previous
        # focusable transcript widget and scroll-CENTER it (the
        # mid-conversation jump + anchor release). Gated on
        # `focused is None` so deliberately-parked focus (e.g. the user
        # Tab'd onto a transcript widget before an engine-driven
        # cancel) is never stolen. Also runs at boot/replay, where the
        # gate reduces it to a benign composer-focus matching the
        # explicit boot focus in `on_mount`. The composer's `disabled`
        # is already current here: the reactive watcher fires
        # synchronously on the assignment above.
        with contextlib.suppress(Exception):
            if self.screen.focused is None:
                handed_off = False
                for block in blocks:
                    if block.focus_pane():
                        handed_off = True
                        break
                if not handed_off:
                    composer = self.query_one(Composer)
                    if not composer.disabled:
                        composer.focus()

    def clear_awaiting_approval(self) -> None:
        """Recompute the composer gate after an approval resolves."""
        # bug-056: the sole clear site (InlineApprovalCallback.finally)
        # used to hard-set the gate to False, which raced with a second
        # still-open live approval. Recompute instead so a stacked live
        # approval keeps the composer frozen until every mounted block
        # resolves (AC #10).
        self.recompute_awaiting_approval()

    async def set_turn_verb(
        self,
        verb: str,
        *,
        target: str = "",  # noqa: ARG002 — kept for call-site/protocol compatibility; see docstring
    ) -> None:
        """Sync the StatusBar's turn-active flag for the given verb.

        spec-130 step 8: this used to also drive the now-deleted
        ``TurnStatus`` bar's ``verb``/``target`` reactives (the verb
        text itself is no longer rendered anywhere — the in-transcript
        ``WorkingPlaceholder`` doesn't show per-tool verb text, per the
        spec's decided glyph/color table). The method — and its
        ``target`` parameter — are kept because every call site in
        ``EventAdapter`` (10 of them) and several test-double protocols
        still call it with this exact signature, and its ONE surviving
        responsibility, keeping ``StatusBar.turn_active`` in lockstep
        (feat-019, Coordinator Q1 lock — suppresses the workflow strip
        during an active turn), is independent of ``TurnStatus`` and
        still required. ``target`` is accepted for call-site
        compatibility but unused now that nothing renders it.
        """
        with contextlib.suppress(Exception):
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.set_turn_active(bool(verb))

    def last_assistant_text(self) -> str:
        """Return the most recent assistant message text."""
        return self._last_assistant_text

    def set_last_assistant_text(self, text: str) -> None:
        """Record the most recent assistant message text."""
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

    #: bug-023 — bare REPL-style exit verbs. A submission whose
    #: trimmed, case-insensitive text is in this set is treated as
    #: ``/exit`` so users with muscle memory from ``python`` / ``bash``
    #: / ``sqlite3`` / ``psql`` / ``node`` / ``ipython`` don't send
    #: prose to the model when they meant to quit. Kept intentionally
    #: narrow — anything longer (``"I exit the room"``) falls through
    #: to prose.
    _BARE_EXIT_VERBS = frozenset({"exit", "quit", ":q", ":wq"})

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        """Composer sent a submit — route through slash / engine.

        The `UserChatbox` mount is NOT done here; the engine emits a
        ``USER_MESSAGE`` event which ``EventAdapter._handle_user_message``
        routes to ``mount_user_message``. Mounting here would produce
        two chatboxes per submission (see the adapter's own docstring:
        the adapter is the single mount site so replay + live paths
        share it).

        bug-023: bare exit verbs (``exit`` / ``quit`` / ``:q`` /
        ``:wq``, case-insensitive) are routed to ``/exit`` before the
        ``/`` prefix check, so REPL muscle-memory doesn't send prose
        to the engine.
        """
        message.stop()
        text = message.content
        composer = self.query_one(Composer)
        # Clear the composer for the next input.
        composer.text = ""
        # Collapse the slash popup, if it was showing suggestions.
        with contextlib.suppress(Exception):  # popup may be unmounted
            self.query_one(SlashPopup).prefix = ""
        text = text.strip()
        if not text:
            return
        # bug-023 — bare REPL-style exit verbs → treat as `/exit`.
        # Must run BEFORE the `/` prefix check (bare `exit` has no
        # leading slash). Uses `_handle_slash("/exit")` so the exit
        # logic stays in one place.
        if text.casefold() in self._BARE_EXIT_VERBS:
            await self._handle_slash("/exit")
            return
        # Slash / @ / ! prefix routing.
        if text.startswith("/"):
            await self._handle_slash(text)
            return
        self._run_exchange(text)

    def on_composer_quit_requested(
        self,
        message: Composer.QuitRequested,
    ) -> None:
        """bug-023 — Ctrl+D on an empty composer → quit if idle.

        Composer intercepted ``ctrl+d`` (TextArea's own binding for
        ``delete_right`` would otherwise consume the key before the
        app-level ``ctrl+d`` binding at ``keybindings.py:32`` fired).
        Route to ``action_quit_if_idle`` — the existing single quit
        site — so the "only quit when idle" semantics stay in one
        place. Direct ``self.exit(0)`` here would bypass that guard.
        """
        message.stop()
        self.action_quit_if_idle()

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
        if command.name == "/why":
            await self._handle_why(arg_text)
            return
        if command.name == "/reload":
            await self._handle_reload()
            return
        if command.name == "/compact":
            await self._handle_compact(arg_text)
            return
        if command.name == "/uncompact":
            await self._handle_uncompact()
            return
        if command.name == "/compaction-history":
            await self._handle_compaction_history()
            return
        if command.name == "/why-compact":
            await self._handle_why_compact(arg_text)
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
        # `pending_clear` currently has no setter in the Textual app: `/clear`
        # wipes via the ANSI-escape path above, and feat-117 moved `/new`'s
        # clear into `_open_new_session`. The branch is retained (harmless) as
        # the seam a future flag-driven clear would reuse.
        if state.pending_clear:
            await self._clear_transcript()
            state.pending_clear = False
        if state.pending_new_session:
            # feat-117: `/new` now performs the real session switch (idle-
            # guarded inside `_open_new_session`). It owns its own transcript
            # clear, so `_handler_new` no longer sets `pending_clear`.
            await self._open_new_session()
            state.pending_new_session = False
        if state.pending_session_id:
            await self._mount_transcript_notice(
                "session lifecycle (/session) is deferred to the slash "
                "follow-up spec; state noted, no switch performed."
            )
            state.pending_session_id = None
        if state.pending_workflow_run_id:
            await self._mount_transcript_notice(
                "/workflow binding recorded; workflow-runner integration "
                "arrives with the workflow bridge event spec."
            )

        if signal is ControlSignal.EXIT:
            self.exit(return_code=0)

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
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")  # noqa: DTZ005 — local time is right for the export filename
        session_slug = str(self.session.session_id)[:12]
        target: Path
        if arg_text.strip():
            target = Path(arg_text.strip()).expanduser().resolve()  # noqa: ASYNC240 — sync IO is intentional
        else:
            exports_dir = Path.home() / ".kolsch" / "exports"
            exports_dir.mkdir(parents=True, exist_ok=True)
            target = exports_dir / f"chat-{session_slug}-{stamp}.txt"
        try:
            lines = [
                "# kolsch chat transcript",
                f"# session: {self.session.session_id}",
                f"# thread:  {self.session.thread_id}",
                f"# agent:   {self.session.config.name}",
                f"# model:   {self.session.model_label}",
                f"# exported: {datetime.now().isoformat(timespec='seconds')}",  # noqa: DTZ005 — local time is right for the export header
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

    async def _handle_why(self, arg_text: str) -> None:
        """Mount the deny card for the last (or specified) tool denial.

        feat-041 — walks the current thread's events via
        ``runtime.store.read_path_to_leaf`` (same pattern as
        ``/export``), correlates the last ``tool.failed`` to its
        ``policy.decision.made``, and mounts a ``Static`` carrying
        the card text.

        ``markup=False`` on the Static so rule patterns containing
        ``[...]`` render literally (bug-040 lesson).
        """
        from cli.chat.services.authz_introspection import (  # noqa: PLC0415
            find_last_denial_via_store,
            render_denial_card,
        )

        tool_call_id = arg_text.strip() or None
        try:
            data = await find_last_denial_via_store(
                self.session.runtime,
                self.session.thread_id,
                tool_call_id=tool_call_id,
            )
        except Exception as exc:  # noqa: BLE001
            await self._mount_transcript_notice(
                f"/why failed: {type(exc).__name__}: {exc}"
            )
            return
        if data is None:
            hint = (
                "No recent denial in this thread. If you meant a "
                "specific tool call, use /why <tool_call_id>."
            )
            await self._mount_transcript_notice(hint)
            return
        text = render_denial_card(data)
        transcript = self.query_one("#transcript", VerticalScroll)
        card = Static(text, markup=False)
        await transcript.mount(card)

    async def _handle_reload(self) -> None:
        """Reload the runtime's authz provider from disk.

        feat-041 — atomic swap on success; leaves the OLD provider
        active on any parse / construct error. See
        ``authz_introspection.handle_reload`` for the contract.
        """
        from cli.chat.services.authz_introspection import (  # noqa: PLC0415
            handle_reload,
        )

        result = await handle_reload(self.session.runtime)
        if result.error is not None:
            await self._mount_transcript_notice(f"/reload failed: {result.error}")
            return
        summary = result.summary or "reload complete."
        await self._mount_transcript_notice(f"↳ {summary}")

    async def _handle_compact(self, arg_text: str) -> None:
        """Force-compact the current thread now (or emit a preview).

        feat-054 Phase 3 — parses the ``[--preview] [--instructions
        '<text>']`` argument shape, delegates to
        :meth:`AgentEngine.manual_compact`, and mounts a chat notice
        with the outcome. The notice mounted by the event adapter
        (from the emitted ``context.compacted`` event) is the primary
        surface; this handler only mounts an inline error notice when
        compaction cannot be attempted (provider disabled / no
        context_length / summariser failure).
        """
        preview, instructions, parse_err = _parse_compact_args(arg_text)
        if parse_err is not None:
            await self._mount_transcript_notice(f"/compact: {parse_err}")
            return
        engine = getattr(self, "engine", None)
        manual_compact = getattr(engine, "manual_compact", None)
        if manual_compact is None:
            await self._mount_transcript_notice(
                "/compact: engine has no compaction provider wired."
            )
            return
        # Mount a running CompactionBlock so the user has a live signal
        # while the summariser call is in flight. The event adapter's
        # terminal handler consumes it and transitions in place.
        running_block = None
        try:
            transcript = self.query_one("#transcript", VerticalScroll)
            from cli.chat.widgets.compaction_block import (  # noqa: PLC0415
                CompactionBlock,
            )

            title = "◎ Compaction preview…" if preview else "● Compacting…"
            body = (
                "Running summariser (preview — will NOT alter live context)"
                if preview
                else "Running summariser…"
            )
            running_block = CompactionBlock(
                state="running",
                title_text=title,
                body_text=body,
            )
            await transcript.mount(running_block)
            self.event_adapter._compaction_widgets["pending"] = running_block
        except Exception:  # noqa: BLE001
            running_block = None
        try:
            emitted = await manual_compact(
                config=self.session.config,
                thread_id=self.session.thread_id,
                preview=preview,
                extra_instructions=instructions,
            )
        except Exception as exc:  # noqa: BLE001
            # Transition the running block to failure if it was mounted;
            # otherwise fall back to the plain notice.
            pending = self.event_adapter._compaction_widgets.pop(
                "pending",
                None,
            )
            if pending is not None:
                try:
                    pending.transition_to_failure(
                        message=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    await self._mount_transcript_notice(
                        f"/compact failed: {type(exc).__name__}: {exc}"
                    )
            else:
                await self._mount_transcript_notice(
                    f"/compact failed: {type(exc).__name__}: {exc}"
                )
            return
        if emitted is None:
            # No summariser call happened — remove the placeholder and
            # surface the standard nothing-to-do notice.
            pending = self.event_adapter._compaction_widgets.pop(
                "pending",
                None,
            )
            if pending is not None:
                with contextlib.suppress(Exception):
                    await pending.remove()
            await self._mount_transcript_notice(
                "↳ /compact: nothing to compact (short session, "
                "compaction disabled, or model has no declared "
                "context_length)."
            )
            return
        # Happy path: EventAdapter._handle_context_compacted transitions
        # the pending block to success/preview via the bus publish
        # inside manual_compact. Nothing more to do here.

    async def _handle_uncompact(self) -> None:
        """Revoke the most-recent active compaction on this thread.

        feat-054 Phase 3 — appends a
        ``CONTEXT_COMPACTION_REVOKED`` event pointing at the newest
        still-active ``CONTEXT_COMPACTED``. Assembly (Phase 2) filters
        both from projection, so the NEXT exchange sees the restored
        context. The live model has already forgotten, so this does
        NOT restart the current exchange.
        """
        engine = getattr(self, "engine", None)
        manual_uncompact = getattr(engine, "manual_uncompact", None)
        if manual_uncompact is None:
            await self._mount_transcript_notice(
                "/uncompact: engine has no compaction provider wired."
            )
            return
        try:
            emitted = await manual_uncompact(
                thread_id=self.session.thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            await self._mount_transcript_notice(
                f"/uncompact failed: {type(exc).__name__}: {exc}"
            )
            return
        if emitted is None:
            await self._mount_transcript_notice("↳ no active compaction to revoke")
            return
        # Notice is mounted by EventAdapter._handle_context_compaction_revoked.

    async def _handle_compaction_history(self) -> None:
        """Walk the thread; mount a compact table of every compaction event.

        feat-054 Phase 3 — read-only. Columns: event_id (short), local
        HH:MM:SS, tokens_before → tokens_after, first..last event span,
        preview flag, revoked_by id (or ``-``).
        """
        from cli.chat.services.compaction_introspection import (  # noqa: PLC0415
            build_compaction_history_table,
        )

        try:
            text = await build_compaction_history_table(
                self.session.runtime,
                self.session.thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            await self._mount_transcript_notice(
                f"/compaction-history failed: {type(exc).__name__}: {exc}"
            )
            return
        transcript = self.query_one("#transcript", VerticalScroll)
        card = Static(text, markup=False)
        await transcript.mount(card)

    async def _handle_why_compact(self, arg_text: str) -> None:
        """Dump ``pre_compaction`` + ``summary`` for one compaction event.

        feat-054 Phase 3 — reads the specific ``CONTEXT_COMPACTED``
        event by id and mounts a plain-text card carrying both JSON
        blobs. When no event with that id exists, mount the "no
        compaction event with id" hint.
        """
        event_id = arg_text.strip()
        if not event_id:
            await self._mount_transcript_notice(
                "/why-compact: pass the compaction event_id (see /compaction-history)."
            )
            return
        from cli.chat.services.compaction_introspection import (  # noqa: PLC0415
            build_why_compact_card,
        )

        try:
            text = await build_why_compact_card(
                self.session.runtime,
                self.session.thread_id,
                event_id=event_id,
            )
        except Exception as exc:  # noqa: BLE001
            await self._mount_transcript_notice(
                f"/why-compact failed: {type(exc).__name__}: {exc}"
            )
            return
        if text is None:
            await self._mount_transcript_notice(
                f"↳ no compaction event with id {event_id}"
            )
            return
        transcript = self.query_one("#transcript", VerticalScroll)
        card = Static(text, markup=False)
        await transcript.mount(card)

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
        # bug-115: the children just removed include the workflow cards
        # and the layout manager's divider. Reset the manager so its
        # compact-mode count / divider state don't carry stale cards into
        # the post-clear (or `/new`) session — a fresh card would
        # otherwise render compact because the old cards' records still
        # count toward the overload threshold.
        if self._workflow_layout_manager is not None:
            self._workflow_layout_manager.reset()

    async def _open_new_session(self) -> None:
        """feat-117 / feat-127: ALWAYS mint + switch to a fresh session.

        Called from `_handle_slash` when `/new` set `pending_new_session`.
        feat-127: `/new` never refuses. It first RETIRES all live work — the
        workflows (bug-128's `cancel` unwinds an approval-parked node;
        `user_input`-suspended runs stay resumable on disk), the in-flight
        prompt, and the butler turn — driving every writer to completion BEFORE
        repointing anything, because `rebind_session` clears the registry
        without draining and an orphaned writer would corrupt the new thread.
        Then mint a new session + thread, repoint every per-session handle
        (`self.session`, `_chat_state` [incl. clearing its per-turn flags], the
        `EventAdapter`, and the three durable-writing impls
        `PromptsImpl`/`CardsImpl`/`WorkflowsImpl`), reset the transcript (which
        also resets the workflow layout manager, bug-115), refresh the info
        bar, announce the new id, and report how many workflows were cancelled.
        The old session stays intact on disk (resumable via
        `kolsch chat <agent> --session <old_id>`).
        """
        # feat-127: `/new` ALWAYS proceeds. Retire all live work BEFORE
        # repointing any handle — `rebind_session` clears the registry without
        # draining, so an orphaned writer (a per-run drain, the FIFO-hosted
        # `_finalize_terminal` tail, or the prompt path) would append the old
        # run's / prompt's tail into the NEW thread. Order: workflows (bug-128's
        # cancel unwinds an approval-parked node; `user_input`-suspended runs
        # stay resumable on disk), then the in-flight prompt (a butler-turn
        # approval parked on a bare await), then the butler turn itself.
        cancelled_workflows = await self._retire_workflows()
        await self._cancel_pending_prompt()
        await self._retire_butler_turn()

        runtime = self.session.runtime
        try:
            session = await runtime.store.create_session(
                str(self.session.config.agent_id),
                name=f"chat:{self.session.agent_ref}",
            )
            threads = await runtime.store.list_threads(session.session_id)
        except Exception as exc:  # noqa: BLE001 — any store failure → notice, not crash
            await self._mount_transcript_notice(
                f"/new: could not create a new session: {type(exc).__name__}: {exc}",
                kind="error",
            )
            return
        if not threads:
            await self._mount_transcript_notice(
                f"/new: new session {session.session_id} has no thread; aborting.",
                kind="error",
            )
            return
        new_session_id = session.session_id
        new_thread_id = threads[0].thread_id

        # Repoint every per-session handle. The three impls each cache the
        # ids and `append_event(self._thread_id, ...)`, so missing one would
        # silently write the new session's events into the old thread.
        self.session.session_id = new_session_id
        self.session.thread_id = new_thread_id
        self.session.is_resume = False
        if self._chat_state is not None:
            self._chat_state.session_id = new_session_id
            self._chat_state.thread_id = new_thread_id
            # The fresh session must start with a clean per-turn slate.
            # Without this, `/skill X` then `/new` would silently force-invoke
            # X on the FIRST message of the new session, and a paused-workflow
            # / awaited-input flag would leak across the session boundary. (The
            # legacy REPL cleared `pending_force_skill` on switch; the workflow
            # flags were cleared by its workflow slash handlers.)
            self._chat_state.pending_force_skill.clear()
            self._chat_state.pending_workflow_run_id = None
            self._chat_state.expecting_user_input_response = False
        self.event_adapter.reset_for_session(new_session_id, new_thread_id)
        registry = self._service_registry
        if registry is not None:
            for impl in (registry.prompts, registry.cards, registry.workflows):
                rebind = getattr(impl, "rebind_session", None)
                if rebind is not None:
                    rebind(new_session_id, new_thread_id)

        # Reset the visible transcript (also resets the layout manager).
        await self._clear_transcript()

        # Refresh the info-bar identity + announce the new session.
        with contextlib.suppress(Exception):
            self.query_one(InfoBar).session = str(new_session_id)[:8]
        await self._mount_transcript_notice(
            f"kolsch chat • {self.session.config.name} • "
            f"{self.session.model_label} • {new_session_id}"
        )
        # feat-127: honest notice of what `/new` cancelled to make room.
        if cancelled_workflows:
            plural = "s" if cancelled_workflows != 1 else ""
            await self._mount_transcript_notice(
                f"Cancelled {cancelled_workflows} in-progress workflow{plural}.",
                kind="warning",
            )

    async def _retire_workflows(
        self,
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 — bound is the point
    ) -> int:
        """feat-127: retire live workflows for a `/new` switch; return the count.

        Delegates to `WorkflowsImpl.retire_live_for_switch`, which drives every
        drain to `done()` before returning (so the id-repoint that follows
        can't race a live writer). `getattr` guards headless / adapter-less
        fixtures. A retire fault must not wedge `/new`.
        """
        workflows = self._active_workflows()
        retire = getattr(workflows, "retire_live_for_switch", None)
        if retire is None:
            return 0
        try:
            return int(await retire(timeout=timeout))
        except Exception:
            _LOGGER.exception(
                "ChatApp._open_new_session: retire_live_for_switch failed",
            )
            return 0

    async def _cancel_pending_prompt(self) -> None:
        """feat-127: cancel any in-flight prompt before the switch.

        Unblocks a butler-turn approval parked on a bare `await` (a
        workflow-node approval was already handled by `_retire_workflows` →
        `WorkflowsImpl.cancel`; a duplicate is deduped by the adapter). No-op
        when no prompt is pending or the impl lacks the hook.
        """
        registry = self._service_registry
        prompts = registry.prompts if registry is not None else None
        cancel = getattr(prompts, "cancel_pending_prompt", None)
        if cancel is None:
            return
        with contextlib.suppress(Exception):
            await cancel()

    async def _retire_butler_turn(
        self,
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 — bound is the point
    ) -> None:
        """feat-127: cancel the in-flight butler turn and await its unwind.

        Fires the cancel token, then polls `active_cancel` until it clears or
        `timeout` elapses. `_run_exchange`'s finally nulls the token BEFORE
        any further statement, so a cleared token means the worker's event
        loop (its `event_adapter.handle` writer) has exited — the quiescence
        signal the switch relies on.

        The token is cooperative (checked at engine attempt boundaries), so
        a turn wedged in a long tool call can outlive the bound. Walking
        away then would leave the worker pumping old-turn events into the
        repointed adapter — durably appended to the NEW thread. On expiry,
        escalate: hard-cancel the `_run_exchange` worker (`CancelledError`
        bypasses its `except Exception` and lands in the finally) and
        re-poll briefly, then proceed regardless — `/new` never hangs.
        """
        if not self._butler_turn_active():
            return
        request_cancel(self)
        for _ in range(max(1, int(timeout / 0.05))):
            if self.active_cancel is None:
                return
            await asyncio.sleep(0.05)
        for worker in list(self.workers):
            if worker.name == "_run_exchange" and not worker.is_finished:
                worker.cancel()
        for _ in range(max(1, int(min(2.0, timeout) / 0.05))):
            if self.active_cancel is None:
                return
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # Action handlers (from BINDINGS)
    # ------------------------------------------------------------------

    def action_cancel_exchange(self) -> None:
        """``escape`` / ``ctrl+c`` — cancel the active exchange."""
        request_cancel(self)

    # ------------------------------------------------------------------
    # feat-019 — Esc-Esc discrimination
    # ------------------------------------------------------------------

    def _butler_turn_active(self) -> bool:
        """True when a butler exchange is in flight.

        Read of :attr:`active_cancel` — the exchange worker sets it
        when it enters ``_run_exchange`` and clears it in the finally
        block, so the presence of a cancel token = a butler turn is
        active.
        """
        return self.active_cancel is not None

    def _has_live_workflow(self) -> bool:
        """True when at least one workflow is ``running`` / ``cancelling``.

        Reads the ``WorkflowsImpl._workflow_registry`` directly (via
        ``getattr`` so headless fixtures without a full impl skip
        cleanly). Values are :class:`WorkflowRunRecord` instances;
        their ``phase`` field is the source of truth.
        """
        workflows_impl = self._active_workflows()
        if workflows_impl is None:
            return False
        registry = getattr(workflows_impl, "_workflow_registry", None)
        if registry is None:
            return False
        for record in registry.values():
            if getattr(record, "phase", None) in ("running", "cancelling"):
                return True
        return False

    def _find_oldest_running_workflow(self) -> Any:
        """Return the ``run_id`` of the oldest live workflow, or None.

        Uses ``started_at`` (with ``created_at`` fallback) as the age
        anchor — matches :func:`sort_workflow_cards`'s in-progress
        band ordering so the FIFO semantics are consistent across the
        UI. Skips terminal / cancelled records.
        """
        workflows_impl = self._active_workflows()
        if workflows_impl is None:
            return None
        registry = getattr(workflows_impl, "_workflow_registry", None)
        if registry is None:
            return None
        live = []
        for run_id, record in registry.items():
            phase = getattr(record, "phase", None)
            if phase not in ("running", "cancelling"):
                continue
            anchor = getattr(record, "started_at", None) or getattr(
                record, "created_at", None
            )
            if anchor is None:
                continue
            live.append((anchor, run_id))
        if not live:
            return None
        live.sort(key=lambda pair: pair[0])
        return live[0][1]

    def _active_workflows(self) -> Any:
        """Return the wired :class:`WorkflowsImpl` handle or ``None``."""
        registry = self._service_registry
        if registry is None:
            return None
        return getattr(registry, "workflows", None)

    def _cancel_butler_turn(self) -> None:
        """Cancel the in-flight butler turn via the shared helper."""
        request_cancel(self)

    async def on_key(self, event: events.Key) -> None:
        """feat-019 Esc-Esc discrimination.

        Non-escape keys fall through to Textual's default handling
        (bindings + widget dispatch). ``escape`` fires the FIFO state
        machine described in :func:`process/feat-019...` — see the
        spec for the full acceptance criteria.
        """
        if event.key != "escape":
            return
        # spec-014 approval takes precedence (existing behaviour).
        # Check composer's ``awaiting_approval`` flag as a proxy.
        with contextlib.suppress(Exception):
            composer = self.query_one(Composer)
            if getattr(composer, "awaiting_approval", False):
                return  # let default handling run

        # Second Esc inside the toast window — cancel the oldest workflow.
        if self._esc_toast_active:
            event.stop()
            self._esc_toast_active = False
            if self._esc_toast_timer is not None:
                with contextlib.suppress(Exception):
                    self._esc_toast_timer.cancel()
                self._esc_toast_timer = None
            oldest_id = self._find_oldest_running_workflow()
            if oldest_id is not None:
                workflows_impl = self._active_workflows()
                if workflows_impl is not None:
                    try:
                        await workflows_impl.cancel(oldest_id)
                    except Exception:
                        _LOGGER.exception(
                            "ChatApp.on_key: workflow cancel raised",
                        )
            return

        # First Esc — FIFO logic.
        cancelled_butler = False
        if self._butler_turn_active():
            self._cancel_butler_turn()
            cancelled_butler = True
        else:
            oldest_id = self._find_oldest_running_workflow()
            if oldest_id is not None:
                event.stop()
                workflows_impl = self._active_workflows()
                if workflows_impl is not None:
                    try:
                        await workflows_impl.cancel(oldest_id)
                    except Exception:
                        _LOGGER.exception(
                            "ChatApp.on_key: workflow cancel raised",
                        )
                return

        # Toast if cancelled butler AND workflow live.
        if cancelled_butler:
            event.stop()
            if self._has_live_workflow():
                self._esc_toast_active = True
                try:
                    loop = asyncio.get_running_loop()
                    self._esc_toast_timer = loop.call_later(
                        _ESC_TOAST_SECONDS,
                        self._dismiss_esc_toast,
                    )
                except RuntimeError:
                    # No running loop — headless fixtures. Toast is a
                    # UX affordance; skip.
                    pass
            return

        # No butler turn AND no live workflow — let default handling
        # run (Textual will fire the ``escape`` binding, which routes
        # to ``action_cancel_exchange`` — a safe no-op with no active
        # cancel token).

    def _dismiss_esc_toast(self) -> None:
        """Timer callback — clear the toast reactive."""
        self._esc_toast_active = False
        self._esc_toast_timer = None

    def watch__esc_toast_active(self, _old: bool, new: bool) -> None:  # noqa: FBT001 — Textual reactive-watcher signature
        """React to Esc-toast flag changes: push / clear the toast text.

        feat-019 AC #6 — the status bar renders the toast text while
        the reactive is True and clears it on transition to False.
        Widget-level rendering happens inside :class:`StatusBar` via
        its own reactive watchers.
        """
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
        except Exception:  # noqa: BLE001 — headless fixtures may lack #status-bar
            return
        if new:
            status_bar.set_toast(_ESC_TOAST_TEXT)
        else:
            status_bar.set_toast(None)

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
        except Exception:  # noqa: BLE001 — no composer mounted → nothing to guard, just exit
            self.exit(return_code=0)
            return
        if not composer.text:
            self.exit(return_code=0)


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
        args_str = (
            ", ".join(f"{k}={v!r}" for k, v in args.items())
            if isinstance(args, dict)
            else str(args)
        )
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
    user_indexes = [i for i, e in enumerate(events) if e.type == EventType.USER_MESSAGE]
    if len(user_indexes) <= n:
        return events, False
    cutoff = user_indexes[-n]
    return events[cutoff:], True


__all__ = ["ChatApp"]

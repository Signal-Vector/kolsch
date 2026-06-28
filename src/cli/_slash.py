"""Slash-command framework for ``kolsch chat`` — Phase 2.1 Step 24b.

Single source of truth for command metadata is :data:`COMMAND_REGISTRY`.
The registry drives:

- ``/help`` output (no parallel listing maintained).
- The :class:`SlashCompleter` popup that prompt_toolkit shows when
  the user types ``/``.
- The dynamic footer hint shown in the bottom toolbar while the
  user is typing a slash command.

Adding a future slash command means adding ONE entry to the
registry — the three consumers above pick it up automatically.

This module is a peer of ``src/cli/chat.py``. It's split out
because at ~250 lines (this file) + ~290 lines (chat.py from
24a) + the wiring 24b adds in chat.py, keeping the slash
framework here keeps each module under the readability ceiling
the build doc calls out.

Patch sections:

- §5 — commands table.
- §5.2 — ``/thinking``'s sub-form options.
- §5.3 — completer popup contract.
- §5.4 — footer-hint contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, TextIO

from prompt_toolkit.completion import Completer, Completion

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from engine import CancelToken
    from platform_runtime import PlatformRuntime
    from providers import AuthorizationProvider
    from providers.authz import Rule
    from schemas import ModelSpec, ResolvedAgentConfig, SessionId, ThreadId


# ---------------------------------------------------------------------------
# Control signals returned by slash handlers
# ---------------------------------------------------------------------------


class ControlSignal(StrEnum):
    """What the input loop should do after a slash command returns.

    The dispatch contract: handlers return one of these; the input
    loop branches accordingly. Engine integration (the
    ``user.message`` path) lands in 24c — that's why ``RELOAD``
    exists as a documented value here but isn't returned by any
    24b handler.
    """

    CONTINUE = "continue"
    """Return to the input prompt; keep the chat going."""

    EXIT = "exit"
    """Tear down the chat and exit cleanly with code 0."""

    NEW_SESSION = "new_session"
    """Reserved for ``/new`` (24c+) — close the current session,
    open a fresh one under the same agent + model."""

    RELOAD = "reload"
    """Reserved for ``/session <id>`` (24e+) — load a different
    session's history into the scrollback."""


# ---------------------------------------------------------------------------
# Handler context — what every handler gets at invocation time
# ---------------------------------------------------------------------------


@dataclass
class ChatSessionState:
    """Mutable per-chat-session state slash handlers may touch.

    Phase 2.1 / 24e introduces this so the lifecycle commands
    (``/new``, ``/model``, ``/session``, ``/thinking``) can effect
    a state change that subsequent exchanges and the input loop
    pick up. The 24c renderer + the 24d modal don't need mutable
    state; they read from ``SlashContext`` 's frozen fields.

    Boundaries on what handlers may write:

    - ``model_override`` + ``config`` — ``/model`` swaps the
      ModelSpec for *this session only*. No platform.yaml edit.
    - ``thinking_on`` / ``thinking_budget`` — ``/thinking``
      session-local flags. Persist for the lifetime of the chat
      process; reset when chat exits. The engine adapter today
      ignores them (graceful degradation per patch §5.2); the
      flags are still recorded so a future engine patch sees
      them without re-debating the surface.
    - ``pending_clear`` / ``pending_new_session`` /
      ``pending_session_id`` — flags the loop reads after
      dispatch to perform the actual session-management work.
      Handlers MUST NOT call ``store.create_session`` directly;
      they signal via these flags so the loop is the single
      site that owns session-lifecycle transitions.
    - ``active_cancel`` / ``last_cancel_reason`` — per-exchange
      cancellation handle + reason tag shared between the input
      loop, the modal, and the Esc background watcher per
      ``bug-chat-deny-stops-and-esc-interrupts.md`` Step 1. The
      input loop sets ``active_cancel`` right after constructing
      the ``CancelToken`` and clears both fields in a ``finally``
      around ``renderer.render``. The modal's deny path flips
      ``active_cancel.cancel()`` and stamps
      ``last_cancel_reason = "denied"`` before returning DENY so
      the framework's next iteration observes the cancellation.
      The Esc watcher does the equivalent with ``"interrupted"``.
      After the renderer drains, the loop prints a distinct
      post-render line based on the reason tag.

    All pending fields are reset by the loop after it handles
    them so a single ``/new`` doesn't keep firing.
    """

    runtime: PlatformRuntime
    config: ResolvedAgentConfig
    session_id: SessionId
    thread_id: ThreadId
    model_override: ModelSpec | None = None
    thinking_on: bool = False
    thinking_budget: int | None = None
    pending_clear: bool = False
    pending_new_session: bool = False
    pending_session_id: SessionId | None = None
    model_aliases: dict[str, str] = field(default_factory=dict)
    active_cancel: CancelToken | None = None
    last_cancel_reason: Literal["denied", "interrupted"] | None = None
    pending_force_skill: list[str] = field(default_factory=list)
    """Skill refs the next user message will force-invoke.

    Populated by ``/skill <ref>`` (one append per invocation; multiple
    ``/skill`` calls before the next submit accumulate). The chat input
    loop reads + clears this list when constructing the next
    ``ExecutionContext`` — present refs flip the trigger from
    ``user_message`` to ``user_message_with_force_skill``. Validation
    happens at the handler site (``_handler_skill``) so a typo'd ref
    fails with a friendly chat-line rather than the engine's
    pre-flight ``SkillForcedNotEnabled`` (which would also kill the
    just-typed user message)."""

    pending_workflow_run_id: str | None = None
    """The workflow_run currently bound to this chat session.

    Phase 3.9 — set by ``/workflow <ref>`` immediately after the run
    is minted; read by ``/fork <event_id>`` to find the parent run;
    read by the user_input chat-resume path to know which run to
    resume when the next non-slash message lands.

    At v1 there is exactly ONE pending workflow per chat session;
    a second ``/workflow`` silently overwrites this pointer (any
    paused first run stays in the store and remains resumable via
    ``kolsch workflows resume``). Persistence across chat-process
    restarts is deferred (the operator re-binds by reissuing
    ``/workflow``)."""

    expecting_user_input_response: bool = False
    """Set when the bound workflow_run has paused on a ``user_input``
    node and the next non-slash message should resume it.

    Phase 3.9 Step 2 owns the input-loop branch that consults this
    flag. ``True`` after the executor returns
    ``WorkflowRunStatus.WAITING_FOR_INPUT``; the next non-slash
    message routes to ``executor.resume(response={"response": text})``
    and clears the flag. Operator escape: any state-changing slash
    command (``/workflow``, ``/fork``, ``/new``, ``/model``) clears
    the flag while leaving the workflow paused (resume later via the
    CLI). Info-only commands (``/help``) do NOT clear the flag — see
    spec §15.2 operator-escape rule."""


@dataclass(frozen=True)
class SlashContext:
    """The collaborators a slash-command handler may touch.

    Kept narrow on purpose — handlers are chat-local operations
    per patch §5; they MUST NOT reach into runtime state or
    emit events except through the documented channels.

    24b ships the field set as just ``stdout``; 24c widens it for
    ``/tools`` (needs the agent config + an authz provider) and
    keeps the read-only-only invariant: every field added so far
    is a **lookup surface**, never a write seam. Specifically: no
    store, no event bus, no engine handle. A handler that wanted
    to mutate the session would need a store reference — and the
    context structurally doesn't carry one. That's what makes the
    chat patch §5.1 "slash commands run when input has the slash
    prefix" claim load-bearing rather than a convention.
    """

    stdout: TextIO
    """Where command output writes."""

    agent_config: ResolvedAgentConfig | None = None
    """The resolved config for the agent the chat is talking to.

    Optional so the simple commands (``/help``, ``/exit``,
    ``/clear``) work in tests that construct a ``SlashContext``
    by hand. ``/tools`` (24c) requires this field — its handler
    raises a clear error if it's None."""

    authz: AuthorizationProvider | None = None
    """The runtime's :class:`AuthorizationProvider`.

    ``/tools`` (24c) walks its rule list to surface which
    rule governs each of the agent's tools. The patch §5 row 7
    "+ the rule chain currently governing it" requirement maps
    onto :meth:`AuthorizationProvider.list_rules`."""

    agent_name: str | None = None
    """Pretty-printable agent name for command output (``/tools``
    headers, etc.). Defaults to the agent's ``name`` field;
    None signals "use a fallback" so tests stay terse."""

    state: ChatSessionState | None = None
    """Mutable session state for the lifecycle commands (24e+).

    None when SlashContext is built without an active chat
    (test surface for ``/help``, ``/exit``, ``/clear``). Handlers
    that need mutable state (``/new``, ``/model``, ``/thinking``,
    ``/session``) print a clear error when this is None so the
    "no chat runtime" case fails predictably rather than
    crashing the test harness."""


# ---------------------------------------------------------------------------
# Command metadata — the registry entry shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlashCommand:
    """One row of the registry.

    ``aliases`` lets ``/exit`` and ``/quit`` share a handler without
    two registry entries fighting for the same description in
    ``/help``. ``options`` populates the §5.3 sub-form completer
    popup (only ``/thinking`` uses it in MVP).
    """

    name: str
    """Canonical command name, with the leading slash
    (``"/help"``, ``"/exit"``)."""

    description: str
    """One-line description shown in ``/help`` AND in the completer
    popup's ``display_meta`` column."""

    usage: str
    """Footer-hint shown while the user is mid-typing this
    command. Patch §5.4."""

    handler: Callable[..., Awaitable[ControlSignal]] | None = None
    """Async callable ``(arg_text, ctx) -> ControlSignal``. ``None``
    means "registered but not implemented yet" — the dispatch
    layer turns ``None`` into a polite placeholder message that
    names the step where the handler lands."""

    options: tuple[tuple[str, str], ...] = ()
    """Sub-form options for the completer's ``/cmd <space>``
    popup. Each tuple is ``(option_value, description)``. Empty
    for commands with no discrete sub-options."""

    free_form_arg_hint: str | None = None
    """Placeholder line for the completer's free-form-arg popup
    (e.g., ``/model <type any model ref>``). Mutually exclusive
    with ``options``."""

    aliases: tuple[str, ...] = ()
    """Additional names that resolve to the same handler. Used
    only by ``/quit`` aliasing ``/exit``. Aliases do NOT show up
    in ``/help`` or the completer; they're invisible synonyms."""

    placeholder_step: str | None = None
    """When ``handler is None``, the step where the real handler
    lands (e.g., ``"24c"``, ``"24d"``). Surfaced in the
    placeholder message so the user knows when to expect it."""


# ---------------------------------------------------------------------------
# Handlers — only /help, /exit, /quit, /clear are real in 24b
# ---------------------------------------------------------------------------


async def _handler_help(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/help`` lists all commands; ``/help <command>`` describes one.

    Patch §5 row 1. Reads from :data:`COMMAND_REGISTRY` — no
    parallel listing; adding a future command surfaces in
    ``/help`` automatically.
    """
    arg = arg_text.strip()
    if arg:
        target = arg if arg.startswith("/") else f"/{arg}"
        command = _resolve_command_name(target)
        if command is None:
            ctx.stdout.write(
                f"unknown command: {target}. Type /help for the full list.\n"
            )
            return ControlSignal.CONTINUE
        ctx.stdout.write(f"{command.name:<10}  {command.description}\n")
        ctx.stdout.write(f"            {command.usage}\n")
        return ControlSignal.CONTINUE

    ctx.stdout.write("slash commands:\n")
    for command in _iter_visible_commands():
        ctx.stdout.write(f"  {command.name:<10}  {command.description}\n")
    ctx.stdout.write("\nShift+Enter (or Alt+Enter / Ctrl-J) inserts a newline.\n")
    return ControlSignal.CONTINUE


async def _handler_exit(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/exit`` / ``/quit`` — clean exit, code 0. Patch §5 row 2."""
    _ = arg_text, ctx
    return ControlSignal.EXIT


async def _handler_new(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/new`` — open a fresh session under the same agent + model.

    Patch §5 row 3. The old session is preserved on disk (the
    user can resume via ``kolsch chat <agent> --session
    <old_id>``); only the visible scrollback clears. The new
    session id appears in the reprinted title bar.

    Handler signals via ``ctx.state``; the input loop performs
    the actual ``store.create_session`` call so session-lifecycle
    transitions stay in one place.
    """
    _ = arg_text
    if ctx.state is None:
        ctx.stdout.write("/new requires a chat runtime; not available here.\n")
        return ControlSignal.CONTINUE
    ctx.state.pending_clear = True
    ctx.state.pending_new_session = True
    return ControlSignal.NEW_SESSION


async def _handler_model(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/model [<ref>]`` — show or switch the current model.

    Patch §5 row 5. Accepts the three Phase 1.1 ModelSpec input
    shapes: object (rare from a slash command), compact string
    (``opencode/gpt-oss-120b``), and alias (``opus`` →
    ``platform.yaml`` ``model_aliases.opus``).

    Without arg: prints the active model. With arg: validates
    via ``assets.assembler._normalize_model_value``, swaps
    ``state.config.model`` for THIS session, and prints a
    confirmation. Invalid refs print an error and the chat stays
    in the session — patch §5 row 5 + 24d builder note "does NOT
    crash". The model.overridden event mentioned in patch §5 row
    5 is deferred: Engine I1 (engine is sole writer of the log)
    forbids the chat from appending directly; the override lives
    in-memory and the next exchange's ``model.request`` reflects
    it (which is the spec's load-bearing behaviour).
    """
    from assets.assembler import _normalize_model_value  # noqa: PLC0415
    from schemas import ModelSpec  # noqa: PLC0415

    if ctx.state is None:
        ctx.stdout.write("/model requires a chat runtime; not available here.\n")
        return ControlSignal.CONTINUE
    arg = arg_text.strip()
    if not arg:
        current = ctx.state.config.model
        ctx.stdout.write(f"current model: {current.provider}/{current.model}\n")
        return ControlSignal.CONTINUE
    try:
        normalized = _normalize_model_value(arg, ctx.state.model_aliases)
        if not isinstance(normalized, dict):
            msg = f"unexpected normalization shape: {type(normalized).__name__}"
            raise TypeError(msg)  # noqa: TRY301
        new_spec = ModelSpec(**normalized)
    except Exception as exc:  # noqa: BLE001 — surface every validation flavour
        ctx.stdout.write(f"/model: invalid model ref {arg!r}: {exc}\n")
        return ControlSignal.CONTINUE
    ctx.state.model_override = new_spec
    ctx.state.config = ctx.state.config.model_copy(update={"model": new_spec})
    ctx.stdout.write(f"model set to {new_spec.provider}/{new_spec.model}\n")
    return ControlSignal.CONTINUE


_THINKING_FORMS = {"on", "off", "last"}
"""Discrete forms ``/thinking`` accepts. ``budget <n>`` is the
fourth, handled separately because it carries an integer arg."""


async def _handler_thinking(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/thinking [on|off|budget <n>|last]`` — extended-thinking control.

    Patch §5.2 — multi-form command shipped with graceful
    degradation: the engine doesn't emit reasoning events today
    (Part 10 item 2 of the chat spec deferred that to a later
    phase), so ``/thinking last`` always prints the documented
    "no thinking content" note; ``/thinking on`` accepts the
    flag for when reasoning events do start landing.
    """
    if ctx.state is None:
        ctx.stdout.write("/thinking requires a chat runtime; not available here.\n")
        return ControlSignal.CONTINUE
    arg = arg_text.strip()
    if not arg:
        _print_thinking_state(ctx)
    elif arg == "on":
        ctx.state.thinking_on = True
        ctx.stdout.write(
            "thinking display: on "
            "(will gate reasoning events when the engine emits them)\n",
        )
    elif arg == "off":
        ctx.state.thinking_on = False
        ctx.stdout.write("thinking display: off\n")
    elif arg.startswith("budget"):
        _apply_thinking_budget(arg, ctx)
    elif arg == "last":
        ctx.stdout.write(
            "No thinking content in this exchange — your current model "
            "may not emit reasoning events, or the engine doesn't yet "
            "expose them.\n",
        )
    else:
        ctx.stdout.write(
            f"/thinking: unknown form {arg!r}; expected one of "
            "on, off, budget <n>, last\n",
        )
    return ControlSignal.CONTINUE


def _print_thinking_state(ctx: SlashContext) -> None:
    """Bare ``/thinking`` — print the current state per patch §5.2 row 1."""
    state = ctx.state
    if state is None:
        return  # guard for type narrowing; caller checks
    on_label = "on" if state.thinking_on else "off"
    budget_label = (
        str(state.thinking_budget) if state.thinking_budget is not None else "(unset)"
    )
    ctx.stdout.write(
        f"thinking display: {on_label}\n"
        f"thinking budget: {budget_label}\n"
        "last-reply thinking: unavailable "
        "(engine doesn't emit reasoning events yet)\n",
    )


def _apply_thinking_budget(arg: str, ctx: SlashContext) -> None:
    """``/thinking budget <n>`` parser — sets the session-local budget."""
    state = ctx.state
    if state is None:
        return  # guard for type narrowing; caller checks
    tokens = arg.split(maxsplit=1)
    expected_token_count = 2
    if len(tokens) != expected_token_count:
        ctx.stdout.write("/thinking budget <n>   n must be an integer\n")
        return
    try:
        n = int(tokens[1])
    except ValueError:
        ctx.stdout.write(
            f"/thinking budget <n>   {tokens[1]!r} is not an integer\n",
        )
        return
    state.thinking_budget = n
    ctx.stdout.write(
        f"thinking budget set to {n}; this model may not honor it.\n",
    )


async def _handler_session(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/session [<id>]`` — show or switch the current session.

    Patch §5 row 8. Without arg: prints the current session id.
    With arg: validates the id via ``store.get_session`` and
    signals the loop to switch + replay via the renderer. The
    actual switch happens in the loop; the handler only signals
    so session-lifecycle transitions stay in one place.
    """
    if ctx.state is None:
        ctx.stdout.write("/session requires a chat runtime; not available here.\n")
        return ControlSignal.CONTINUE
    arg = arg_text.strip()
    if not arg:
        ctx.stdout.write(f"current session: {ctx.state.session_id}\n")
        return ControlSignal.CONTINUE
    # Strip surrounding quotes the user may paste.
    target_id = arg.strip("'\" ")
    try:
        from schemas import SessionId  # noqa: PLC0415

        session_id = SessionId(target_id)
        session = await ctx.state.runtime.store.get_session(session_id)
    except Exception as exc:  # noqa: BLE001 — surface anything
        ctx.stdout.write(f"/session: session {target_id!r} not loadable: {exc}\n")
        return ControlSignal.CONTINUE
    ctx.state.pending_session_id = session.session_id
    return ControlSignal.RELOAD


async def _handler_tools(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/tools`` — list the tools this agent can call + their governing rule.

    Patch §5 row 7. Reads the agent's tools from
    ``ctx.agent_config.subsystems["tools"].config`` (the same
    declaration the engine reads at pre-flight), then walks
    ``ctx.authz.list_rules()`` and surfaces the first rule whose
    ``(who, what)`` regexes match the tool's
    ``(principal, action)`` pair.

    Principal at tool dispatch time is ``agent.<agent_id>`` per
    agent-engine-contract §1.8; action is ``tool.<tool_name>``
    per the Tools subsystem's authz dispatch contract. Matching
    uses ``re.fullmatch`` to mirror the authz provider's
    ``A-I1`` invariant — so what ``/tools`` shows IS what the
    runtime would resolve at dispatch.

    Tools the agent does NOT have access to are NOT listed. The
    spec is emphatic about this (TC-CHAT-12 pins it as part of
    the acceptance criterion).
    """
    _ = arg_text
    config = ctx.agent_config
    if config is None:
        ctx.stdout.write(
            "/tools: no agent context available.\n"
            "(this is a chat-runtime invariant — please report.)\n",
        )
        return ControlSignal.CONTINUE

    tool_names = _tools_for_agent(config)
    if not tool_names:
        ctx.stdout.write(
            "no tools configured for this agent "
            "(subsystems.tools is empty or unset).\n",
        )
        return ControlSignal.CONTINUE

    rules: list[Rule] = []
    if ctx.authz is not None:
        rules = await ctx.authz.list_rules()
    principal = f"agent.{config.agent_id}"
    # qol-permission-system-human-readable.md Step 4 — the
    # rule-lookup helper handles both legacy regex AND new-shape
    # glob rules; the lookup callable bridges the action namespace
    # (`tool.bash`) and the qualified tool ref (`kolsch/bash`)
    # used by new-shape `what:` fields. Absent when the authz
    # variant doesn't expose the map.
    tool_ref_lookup = getattr(ctx.authz, "lookup_tool_ref", None)

    ctx.stdout.write("tools available to this agent:\n")
    for name in tool_names:
        rule = _governing_rule(
            rules,
            principal=principal,
            tool_name=name,
            tool_ref_lookup=tool_ref_lookup,
        )
        if rule is None:
            descriptor = "(no matching rule — default deny)"
        else:
            descriptor = f"{rule.id} ({rule.effect})"
        ctx.stdout.write(f"  {name:<20}  {descriptor}\n")
    return ControlSignal.CONTINUE


async def _handler_skill(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/skill <ref>`` — force-invoke a skill on the next exchange.

    Stashes the ref on :attr:`ChatSessionState.pending_force_skill`.
    The chat input loop reads + clears that list when building the
    next ``ExecutionContext``: a non-empty list flips the trigger
    shape from ``user_message`` to ``user_message_with_force_skill``,
    which the engine's pre-flight step 9 (``_process_force_invoke``)
    expands into a synthetic ``tool.read`` on the SKILL.md path.

    Validation at the slash site: a ref NOT in the agent's
    ``subsystems.skills.config.enabled`` prints a friendly error and
    leaves ``pending_force_skill`` untouched. The engine would
    otherwise raise :class:`errors.SkillForcedNotEnabled` at
    pre-flight, which is fatal to the exchange and would discard
    the user's just-typed message — surfacing the typo here keeps
    the next message intact.

    Multiple ``/skill`` calls before a submit accumulate; one
    exchange consumes them all. The trigger's ``force_skills`` list
    preserves the order they were typed.
    """
    if ctx.state is None:
        ctx.stdout.write("/skill requires a chat runtime; not available here.\n")
        return ControlSignal.CONTINUE
    ref = arg_text.strip()
    if not ref:
        # Bare ``/skill`` — print pending refs as a state surface.
        pending = list(ctx.state.pending_force_skill)
        if pending:
            ctx.stdout.write("pending force-invoke on next message:\n")
            for r in pending:
                ctx.stdout.write(f"  {r}\n")
        else:
            ctx.stdout.write(
                "/skill <ref>   queue a skill to force-load before the next message.\n"
            )
        return ControlSignal.CONTINUE
    enabled = _enabled_skill_refs(ctx.state.config)
    if ref not in enabled:
        ctx.stdout.write(
            f"/skill: {ref!r} is not in this agent's enabled skills.\n"
        )
        if enabled:
            ctx.stdout.write(f"enabled: {', '.join(enabled)}\n")
        else:
            ctx.stdout.write(
                "(this agent has no skills enabled in "
                "subsystems.skills.config.enabled.)\n"
            )
        return ControlSignal.CONTINUE
    ctx.state.pending_force_skill.append(ref)
    ctx.stdout.write(
        f"queued {ref} — force-load runs before your next message.\n"
    )
    return ControlSignal.CONTINUE


async def _handler_perm(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/perm <verb> [<tool> <pattern>]`` —
    qol-permission-system-human-readable.md Step 3 task 1.

    Five verbs, sharing one handler so ``/perm`` keeps a single
    completer + footer-hint entry:

    - ``/perm allow  <tool> <pattern>``
    - ``/perm deny   <tool> <pattern>``
    - ``/perm prompt <tool> <pattern>``
    - ``/perm remove <tool> <pattern>``
    - ``/perm list``

    ``<tool>`` must be the fully-qualified tool ref
    (``kolsch/bash``) or a system action namespace
    (``agent.execute.*``) or the catch-all ``*``. Bare names fail
    here with a friendly slash-line rather than the Fatal
    AuthzBareToolRef the provider would raise at next reload.
    ``<pattern>`` is a glob; the same ``_PATTERN_REGEX_CHARS``
    set is rejected as for YAML-loaded rules.

    Allow/deny/prompt verbs land the new rule ABOVE the catch-all
    AND ABOVE any conflict whose ``(what, pattern)`` would shadow
    the user's intent. The output line names the position so the
    user sees where it landed. Remove drops the rule by
    ``(what, pattern)`` match (legacy rules can still be removed
    by their rule id via ``kolsch permissions remove``).
    """
    if ctx.state is None:
        ctx.stdout.write("/perm requires a chat runtime; not available here.\n")
        return ControlSignal.CONTINUE
    parts = arg_text.split(maxsplit=1)
    if not parts:
        ctx.stdout.write(_PERM_USAGE)
        return ControlSignal.CONTINUE
    verb = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if verb == "list":
        await _perm_list(ctx)
        return ControlSignal.CONTINUE
    if verb not in {"allow", "deny", "prompt", "remove"}:
        ctx.stdout.write(
            f"/perm: unknown verb {verb!r}; "
            f"expected one of allow, deny, prompt, remove, list\n",
        )
        return ControlSignal.CONTINUE

    tokens = _split_perm_args(rest)
    if tokens is None:
        ctx.stdout.write(_PERM_USAGE)
        return ControlSignal.CONTINUE
    tool, pattern = tokens
    if not _validate_perm_tool(tool, ctx=ctx):
        return ControlSignal.CONTINUE
    if not _validate_perm_pattern(pattern, ctx=ctx):
        return ControlSignal.CONTINUE

    yaml_path = ctx.state.runtime.root / "platform.yaml"
    if verb == "remove":
        await _perm_remove(ctx, tool=tool, pattern=pattern, yaml_path=yaml_path)
    else:
        await _perm_upsert(
            ctx,
            verb=verb,
            tool=tool,
            pattern=pattern,
            yaml_path=yaml_path,
        )
    return ControlSignal.CONTINUE


_PERM_USAGE = (
    "/perm <verb> <tool> <pattern>   verbs: allow | deny | prompt | remove\n"
    "/perm list                       print every current rule\n"
    "  example: /perm allow kolsch/bash \"find *\"\n"
)


def _split_perm_args(text: str) -> tuple[str, str] | None:
    """Split a ``<tool> <pattern>`` arg string into two non-empty tokens.

    Supports double-quoted patterns so multi-word globs like
    ``"find * -name foo"`` survive: ``/perm allow kolsch/bash "find * -name foo"``.
    Returns ``None`` when the input doesn't parse as two tokens.
    """
    text = text.strip()
    if not text:
        return None
    head, _, tail = text.partition(" ")
    if not tail:
        return None
    tail = tail.strip()
    if tail.startswith('"'):
        if not tail.endswith('"') or len(tail) < 2:
            return None
        return head, tail[1:-1]
    return head, tail


def _validate_perm_tool(tool: str, *, ctx: SlashContext) -> bool:
    """``what`` validation mirroring
    :meth:`providers.RuleBasedAuthorizationProvider._validate_what`.

    - ``*`` → accept (catch-all).
    - Contains ``.`` (e.g., ``agent.execute.*``) → accept.
    - Contains ``/`` and the catalog has the qualified tool → accept.
    - Otherwise → friendly slash-line error (no exception).

    Returns ``True`` when valid; otherwise writes the error to
    ``ctx.stdout`` and returns ``False``."""
    if not tool:
        ctx.stdout.write("/perm: tool is required\n")
        return False
    if tool == "*" or "." in tool:
        return True
    if "/" not in tool:
        # Bare name — try to suggest the qualified form.
        candidates = _qualified_candidates_for_bare(tool, ctx=ctx)
        if candidates:
            ctx.stdout.write(
                f"/perm: {tool!r} is a bare tool name. "
                f"Use the qualified form: {', '.join(candidates)}\n",
            )
        else:
            ctx.stdout.write(
                f"/perm: {tool!r} is a bare tool name and not in the catalog. "
                "Use the qualified `<bundle>/<name>` form.\n",
            )
        return False
    # Qualified form — confirm against catalog if accessible.
    lookup = _action_to_tool_ref_map(ctx)
    if lookup is None:
        return True  # no catalog to check against; accept (best-effort)
    qualified_refs = set(lookup.values())
    if tool not in qualified_refs:
        ctx.stdout.write(
            f"/perm: tool ref {tool!r} not found in the catalog.\n",
        )
        return False
    return True


def _validate_perm_pattern(pattern: str, *, ctx: SlashContext) -> bool:
    """Reject regex characters in the glob pattern — same set the
    provider's ``_PATTERN_REGEX_CHARS`` rejects at YAML load.

    Surfaces the bad characters in the error so the user can fix
    in one keystroke."""
    from providers.authz import _PATTERN_REGEX_CHARS  # noqa: PLC0415

    if not pattern:
        ctx.stdout.write(
            "/perm: pattern is required (use `*` to match anything)\n",
        )
        return False
    bad = [c for c in _PATTERN_REGEX_CHARS if c in pattern]
    if bad:
        ctx.stdout.write(
            f"/perm: pattern {pattern!r} contains regex character(s) "
            f"{bad} — patterns are globs (use `*` and `?` only).\n",
        )
        return False
    return True


def _qualified_candidates_for_bare(
    bare: str,
    *,
    ctx: SlashContext,
) -> list[str]:
    """Look up qualified refs for ``bare`` from the runtime's authz
    provider. Returns ``[]`` if nothing in the catalog matches."""
    lookup = _action_to_tool_ref_map(ctx)
    if lookup is None:
        return []
    return sorted(
        ref for ref in lookup.values() if ref.rsplit("/", 1)[-1] == bare
    )


def _action_to_tool_ref_map(ctx: SlashContext) -> dict[str, str] | None:
    """Best-effort access to the authz provider's action→tool_ref map.

    Used by ``/perm`` validation. Returns ``None`` when the runtime
    isn't available or the authz provider's a variant without the
    accessor (custom variant in future)."""
    if ctx.state is None:
        return None
    authz = ctx.state.runtime.authz
    getter = getattr(authz, "_action_to_tool_ref", None)
    if not isinstance(getter, dict):
        return None
    return getter


_VERB_TO_EFFECT: dict[str, str] = {
    "allow": "allow",
    "deny": "deny",
    "prompt": "prompt",
}


async def _perm_list(ctx: SlashContext) -> None:
    """Render the current rule list."""
    from providers import RuleBasedAuthorizationProvider  # noqa: PLC0415

    assert ctx.state is not None  # guarded by caller
    authz = ctx.state.runtime.authz
    if not isinstance(authz, RuleBasedAuthorizationProvider):
        ctx.stdout.write("/perm: authz variant doesn't support listing rules.\n")
        return
    rules = await authz.list_rules()
    if not rules:
        ctx.stdout.write("(no rules)\n")
        return
    ctx.stdout.write("rules (first-match-wins, top to bottom):\n")
    for index, rule in enumerate(rules):
        if rule.who is None:
            ctx.stdout.write(
                f"  {index:>2}  what={rule.what!r:<22}  "
                f"pattern={rule.pattern!r:<14}  effect={rule.effect}\n",
            )
        else:
            ctx.stdout.write(
                f"  {index:>2}  what={rule.what!r:<22}  "
                f"[legacy regex — please convert]  effect={rule.effect}\n",
            )


async def _perm_upsert(
    ctx: SlashContext,
    *,
    verb: str,
    tool: str,
    pattern: str,
    yaml_path: Any,
) -> None:
    """Insert/update an allow/deny/prompt rule.

    Smart insertion: drops any prior new-shape rule with the same
    ``(what, pattern)`` (replacing intent) then inserts above the
    default-deny tail. The output names the final position so the
    user can immediately verify where it landed."""
    from cli.permissions import _write_back  # noqa: PLC0415
    from providers import RuleBasedAuthorizationProvider, Rule  # noqa: PLC0415

    assert ctx.state is not None  # guarded by caller
    authz = ctx.state.runtime.authz
    if not isinstance(authz, RuleBasedAuthorizationProvider):
        ctx.stdout.write("/perm: authz variant doesn't support editing rules.\n")
        return

    effect = _VERB_TO_EFFECT[verb]
    # Remove any existing new-shape rule with the same
    # (what, pattern) so the user's intent isn't shadowed.
    removed_index = await _remove_matching_new_rule(
        authz, what=tool, pattern=pattern,
    )
    new_rule = Rule(what=tool, pattern=pattern, effect=effect)
    inserted = await authz.add_rule(new_rule)
    _write_back(yaml_path, await authz.list_rules())
    position = await _position_of(authz, inserted.id)
    landed = "above the catch-all" if position is not None else "<unknown>"
    if removed_index is not None:
        ctx.stdout.write(
            f"/perm {verb}: replaced previous rule at position {removed_index}\n",
        )
    ctx.stdout.write(
        f"/perm {verb}: added rule  what={tool!r}  pattern={pattern!r}  "
        f"effect={effect}  ({landed}, position {position})\n",
    )


async def _perm_remove(
    ctx: SlashContext,
    *,
    tool: str,
    pattern: str,
    yaml_path: Any,
) -> None:
    """Drop the new-shape rule matching ``(tool, pattern)``.

    Legacy rules are intentionally not droppable here — they have a
    stable rule id the user can use via
    ``kolsch permissions remove <id>``. Refusing to drop them via
    ``/perm remove`` also prevents accidentally clobbering a
    hand-authored regex rule whose ``what`` happens to look like a
    qualified ref."""
    from cli.permissions import _write_back  # noqa: PLC0415
    from providers import RuleBasedAuthorizationProvider  # noqa: PLC0415

    assert ctx.state is not None
    authz = ctx.state.runtime.authz
    if not isinstance(authz, RuleBasedAuthorizationProvider):
        ctx.stdout.write("/perm: authz variant doesn't support editing rules.\n")
        return
    removed = await _remove_matching_new_rule(authz, what=tool, pattern=pattern)
    if removed is None:
        ctx.stdout.write(
            f"/perm remove: no rule with what={tool!r} pattern={pattern!r} "
            "(legacy rules: use `kolsch permissions remove <id>`)\n",
        )
        return
    _write_back(yaml_path, await authz.list_rules())
    ctx.stdout.write(
        f"/perm remove: dropped rule at position {removed}  "
        f"what={tool!r}  pattern={pattern!r}\n",
    )


async def _remove_matching_new_rule(
    authz: Any,
    *,
    what: str,
    pattern: str,
) -> int | None:
    """Drop the FIRST new-shape rule whose ``(what, pattern)`` matches.

    Returns the original index of the removed rule (so the caller
    can report it) or ``None`` when nothing matched. Walks the rule
    list once; matching is exact on the strings — globs aren't
    expanded since we're operating on the rule itself, not an
    invocation."""
    rules = await authz.list_rules()
    for index, rule in enumerate(rules):
        if rule.who is not None:
            continue  # never touch legacy rules from /perm
        if rule.what == what and rule.pattern == pattern:
            await authz.remove_rule(rule.id)
            return index
    return None


async def _position_of(authz: Any, rule_id: str) -> int | None:
    """Walk the in-memory rule list once to find a rule's index."""
    rules = await authz.list_rules()
    for index, rule in enumerate(rules):
        if rule.id == rule_id:
            return index
    return None


async def _handler_clear(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/clear`` — clear visible scrollback; session + event log untouched.

    Patch §5 row 4 + the §5 spec's load-bearing distinction from
    ``/new``: ``/clear`` is a display-only operation. Subsequent
    ``kolsch sessions events <id>`` against the same session still
    returns every event from before the ``/clear``.

    Implementation: write the ANSI sequence ``\\x1b[H\\x1b[2J\\x1b[3J``
    — cursor home + clear screen + clear scrollback buffer (xterm
    extension supported by every modern terminal we target).
    """
    _ = arg_text
    # Cursor home + erase entire screen + clear scrollback buffer.
    # xterm DEC private mode "3J" is what handles the scrollback;
    # without it some terminals leave the prior output reachable
    # via Page Up. Combined with the "H" home cursor + "2J" screen
    # erase, this produces the §5 "clear visible scrollback"
    # behaviour on macOS Terminal.app, iTerm2, kitty, Alacritty,
    # gnome-terminal.
    ctx.stdout.write("\x1b[H\x1b[2J\x1b[3J")
    ctx.stdout.flush()
    # ``/clear`` is a context-reset signal — any /skill refs the user
    # queued before clearing the scrollback would silently bleed into
    # the next message, force-loading a skill they may not even
    # remember queuing. Drop pending refs to keep state in line with
    # what the user can see.
    if ctx.state is not None:
        ctx.state.pending_force_skill.clear()
    return ControlSignal.CONTINUE


# ---------------------------------------------------------------------------
# COMMAND_REGISTRY — the single source of truth
# ---------------------------------------------------------------------------


COMMAND_REGISTRY: dict[str, SlashCommand] = {
    "/help": SlashCommand(
        name="/help",
        description="show available commands or describe one",
        usage="/help [<command>]   list all commands or print one's hint",
        handler=_handler_help,
    ),
    "/exit": SlashCommand(
        name="/exit",
        description="leave the chat",
        usage="/exit   exit chat (same as Ctrl-D)",
        handler=_handler_exit,
        aliases=("/quit",),
    ),
    "/new": SlashCommand(
        name="/new",
        description="start a fresh session, same agent",
        usage="/new   open a new session under this agent (preserves the old)",
        handler=_handler_new,
    ),
    "/clear": SlashCommand(
        name="/clear",
        description="clear visible scrollback (session unchanged)",
        usage="/clear   wipe the screen; session + event log untouched",
        handler=_handler_clear,
    ),
    "/model": SlashCommand(
        name="/model",
        description="switch model for this session",
        usage=("/model <ref>   any ModelSpec input shape (alias or provider/model)"),
        handler=_handler_model,
        free_form_arg_hint="<type any model ref>",
    ),
    "/thinking": SlashCommand(
        name="/thinking",
        description="control extended-thinking display and budget",
        usage=(
            "/thinking [on|off|budget <n>|last]   extended-thinking display + budget"
        ),
        handler=_handler_thinking,
        options=(
            ("on", "show thinking inline"),
            ("off", "hide thinking (default)"),
            ("budget <n>", "set max thinking tokens for next exchange"),
            ("last", "show thinking from the most recent reply"),
        ),
    ),
    "/tools": SlashCommand(
        name="/tools",
        description="list tools available to this agent",
        usage="/tools   list this agent's tools + the rules governing each",
        handler=_handler_tools,
    ),
    "/session": SlashCommand(
        name="/session",
        description="show or switch current session",
        usage="/session [<id>]   print the current session id, or switch",
        handler=_handler_session,
        free_form_arg_hint="<recent session id>",
    ),
    "/skill": SlashCommand(
        name="/skill",
        description="force-load a skill before the next exchange",
        usage=(
            "/skill <ref>   queue a SKILL.md read (force-invoke); "
            "the next message triggers it"
        ),
        handler=_handler_skill,
        free_form_arg_hint="<bundle>/skills/<name>",
    ),
    "/perm": SlashCommand(
        name="/perm",
        description="edit authorization rules (allow/deny/prompt/remove/list)",
        usage=(
            "/perm <verb> <tool> <pattern>   verbs: allow | deny | prompt | "
            "remove ; or /perm list"
        ),
        handler=_handler_perm,
        options=(
            ("allow", "permit a tool invocation matching <tool> <pattern>"),
            ("deny", "block a tool invocation matching <tool> <pattern>"),
            ("prompt", "ask before a tool invocation matching <tool> <pattern>"),
            ("remove", "drop the new-shape rule with this <tool> <pattern>"),
            ("list", "print every current rule in evaluation order"),
        ),
    ),
}
"""Ten visible commands; ``/exit`` carries ``/quit`` as an alias.
Patch §5 + ``/skill`` (skills-subsystem.md Draft 0.2 Step 3 follow-up) +
``/perm`` (qol-permission-system-human-readable.md Step 3)."""


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def is_slash_command(text: str) -> bool:
    """True iff ``text`` is a slash-command invocation.

    The input loop calls this before splitting between "send to
    engine" and "dispatch as slash command". Leading whitespace is
    stripped — a user typing ``  /help`` still gets the command,
    not a user message.
    """
    return text.lstrip().startswith("/")


async def dispatch_slash(text: str, ctx: SlashContext) -> ControlSignal:
    """Parse ``text``, look up the command, run its handler.

    Unknown commands print a single-line error and return
    ``CONTINUE``. Placeholder commands (``handler is None``) print
    a "not yet implemented (Step <NN>)" message and return
    ``CONTINUE``. Real handlers receive ``(arg_text, ctx)``.
    """
    stripped = text.lstrip()
    head, _, tail = stripped.partition(" ")
    arg_text = tail.strip()
    command = _resolve_command_name(head)
    if command is None:
        ctx.stdout.write(f"unknown command: {head}. Type /help for the full list.\n")
        return ControlSignal.CONTINUE
    if command.handler is None:
        step = command.placeholder_step or "a later step"
        ctx.stdout.write(
            f"{command.name} is not yet implemented (lands in Step {step}).\n"
        )
        return ControlSignal.CONTINUE
    return await command.handler(arg_text, ctx)


def _resolve_command_name(head: str) -> SlashCommand | None:
    """Look up a command by name OR alias.

    Walks ``COMMAND_REGISTRY`` once (8 entries — cheap). Returns
    the canonical entry when ``head`` matches its ``name`` or any
    of its ``aliases``.
    """
    for command in COMMAND_REGISTRY.values():
        if head == command.name or head in command.aliases:
            return command
    return None


def _iter_visible_commands() -> Iterable[SlashCommand]:
    """``COMMAND_REGISTRY`` in insertion order — aliases stay hidden.

    The registry is a plain dict; Python 3.7+ preserves insertion
    order, so ``/help`` shows commands in the order they were
    registered (patch §5 row order).
    """
    return COMMAND_REGISTRY.values()


# ---------------------------------------------------------------------------
# Completer
# ---------------------------------------------------------------------------


class SlashCompleter(Completer):
    """Drives the prompt_toolkit popup for slash-command completion.

    Patch §5.3:

    - Buffer starts with ``/`` and has no trailing space → narrow
      registry by prefix; popup lists matching commands with their
      one-line descriptions.
    - Buffer is ``/<command> `` (trailing space) and the command
      has discrete ``options`` → popup lists each option with its
      description.
    - Buffer is ``/<command> `` and the command has a
      ``free_form_arg_hint`` → popup shows the hint as a
      non-selectable placeholder line (best-effort: prompt_toolkit
      requires every Completion to be "insertable", so the hint
      is yielded as a no-op completion whose ``text`` is empty).
    - Anything else → no completions (popup hides).

    Phase 2.1 / 24e: ``/model `` (trailing space) ALSO yields
    aliases from ``platform.yaml``'s ``model_aliases`` section
    when ``model_alias_provider`` is supplied at construction.
    The aliases are queried fresh on every completion request so
    a future ``kolsch llm models alias add`` mid-session would
    surface immediately (today there's no such command — the
    provider is just a closure over the dict).
    """

    def __init__(
        self,
        *,
        model_alias_provider: Callable[[], dict[str, str]] | None = None,
        session_id_provider: Callable[[], list[tuple[str, str]]] | None = None,
        skill_ref_provider: Callable[[], list[tuple[str, str]]] | None = None,
    ) -> None:
        """Optional dynamic suggestion sources.

        ``model_alias_provider`` is queried on every keystroke when
        the buffer is ``/model `` (trailing space); returns
        ``{alias: canonical}``.

        ``session_id_provider`` is queried when the buffer is
        ``/session `` (trailing space); returns ``[(session_id,
        display_label)]``. The store's ``list_sessions`` is async;
        the closure here is sync — the chat snapshots recent ids
        at startup and re-snapshots when ``/new`` opens a fresh
        session. Stale ids that the user types still work (the
        ``/session`` handler validates via ``store.get_session``).

        ``skill_ref_provider`` is queried when the buffer is
        ``/skill `` (trailing space); returns ``[(skill_ref,
        display_label)]`` for the agent's enabled skills. Snapshotted
        from ``config.subsystems.skills.config.enabled`` at chat
        startup so the popup is fast.
        """
        self._model_alias_provider = model_alias_provider
        self._session_id_provider = session_id_provider
        self._skill_ref_provider = skill_ref_provider

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        _ = complete_event
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        head, sep, tail = text.partition(" ")
        if not sep:
            yield from self._complete_command_name(head)
            return
        # Trailing space (or characters after it) — sub-form
        # completions, if the command has any.
        resolved = _resolve_command_name(head)
        if resolved is None:
            return
        yield from self._complete_command_arg(resolved, tail)

    def _complete_command_name(self, head: str) -> Iterable[Completion]:
        """Yield matching commands when the buffer is just ``/<prefix>``."""
        for command in _iter_visible_commands():
            if command.name.startswith(head):
                yield Completion(
                    text=command.name[len(head) :],
                    display=command.name,
                    display_meta=command.description,
                )

    def _complete_command_arg(
        self, resolved: SlashCommand, partial: str
    ) -> Iterable[Completion]:
        """Yield arg-position completions for the resolved command."""
        if resolved.options:
            for option_value, option_desc in resolved.options:
                if option_value.startswith(partial):
                    yield Completion(
                        text=option_value[len(partial) :],
                        display=option_value,
                        display_meta=option_desc,
                    )
            return
        if resolved.name == "/model" and self._model_alias_provider is not None:
            yield from _prefix_matched_completions(
                self._model_alias_provider().items(),
                partial=partial,
            )
        if resolved.name == "/session" and self._session_id_provider is not None:
            yield from _prefix_matched_completions(
                self._session_id_provider(),
                partial=partial,
            )
        if resolved.name == "/skill" and self._skill_ref_provider is not None:
            yield from _prefix_matched_completions(
                self._skill_ref_provider(),
                partial=partial,
            )
        if resolved.free_form_arg_hint is not None and not partial:
            # A non-completing hint — yields with empty insertion
            # text so the popup just shows the placeholder line
            # for guidance.
            yield Completion(
                text="",
                display=resolved.free_form_arg_hint,
                display_meta="(any value)",
            )


def _prefix_matched_completions(
    pairs: Iterable[tuple[str, str]],
    *,
    partial: str,
) -> Iterable[Completion]:
    """Yield ``Completion`` objects for ``(value, label)`` pairs
    whose value starts with ``partial``.

    Shared between the ``/model`` and ``/session`` completion
    branches — both follow the same shape (prefix-match a list of
    pairs, render value + ``display_meta`` label).
    """
    for value, label in pairs:
        if value.startswith(partial):
            yield Completion(
                text=value[len(partial) :],
                display=value,
                display_meta=label,
            )


# ---------------------------------------------------------------------------
# Footer hint generator — patch §5.4
# ---------------------------------------------------------------------------


_DEFAULT_FOOTER_HINT = (
    "Shift+Enter (or Alt+Enter / Ctrl-J) for new line • /help • Ctrl-D to exit"
)
"""Idle hint — shown when the input is empty or doesn't start
with ``/``. Patch §5.4 + §2.1."""


# ---------------------------------------------------------------------------
# /tools helpers — kept module-private so tests can exercise them directly
# ---------------------------------------------------------------------------


def _tools_for_agent(config: ResolvedAgentConfig) -> list[str]:
    """Return the tool names a ``ResolvedAgentConfig`` enables.

    Reads from ``config.subsystems["tools"].config`` — the same
    structure the engine reads at pre-flight step 11
    (``_register_tools_with_subsystem``). Two flavours:

    - ``builtin: [name, ...]`` — Phase-1-era platform builtins.
      Used by older agent configs; no live shippable agent today
      relies on it, but the helper handles both shapes.
    - ``declared: [{ref: "<bundle>/tools/<name>"}, ...]`` — the
      file-IS-the-tool path the helper agent uses. The shipped
      starter bundle declares four refs this way.

    Returns names in the order the config lists them so the
    ``/tools`` output is deterministic.
    """
    sub = config.subsystems.get("tools")
    if sub is None:
        return []
    body = sub.config or {}
    names: list[str] = []
    for raw in body.get("builtin") or []:
        if isinstance(raw, str):
            names.append(raw)
    for raw in body.get("declared") or []:
        if isinstance(raw, dict):
            ref = str(raw.get("ref", ""))
            # Tool name is the ref's final segment ("kolsch/read_file"
            # → "read_file", or the legacy "kolsch/read_file"
            # → "read_file"). Matches the Tools subsystem's discovery rule.
            if ref:
                names.append(ref.rpartition("/")[2] or ref)
        elif isinstance(raw, str):
            names.append(raw.rpartition("/")[2] or raw)
    return names


def _enabled_skill_refs(config: ResolvedAgentConfig) -> list[str]:
    """Return the agent's enabled skill refs in declaration order.

    Reads ``config.subsystems["skills"].config["enabled"]`` — the
    same list :class:`SkillsSubsystem.get_enabled_skills` walks at
    runtime. Used by ``/skill`` for validation + completion. Returns
    ``[]`` when the subsystem is absent / unconfigured (the agent
    has no skills; ``/skill`` prints a clear message in that case).
    """
    sub = config.subsystems.get("skills")
    if sub is None:
        return []
    body = sub.config or {}
    out: list[str] = []
    for entry in body.get("enabled") or []:
        if isinstance(entry, dict):
            ref = entry.get("ref")
            if isinstance(ref, str) and ref:
                out.append(ref)
    return out


def _governing_rule(
    rules: list[Rule],
    *,
    principal: str,
    tool_name: str,
    tool_ref_lookup: Callable[[str], str | None] | None = None,
) -> Rule | None:
    """First-match-wins rule lookup mirroring the authz provider.

    Dispatches per-rule shape:

    - **Legacy** rules (``who`` set): mirror ``re.fullmatch`` on
      both ``who`` and ``what`` per A-I1.
    - **New-shape** rules (``who is None``):
      :func:`fnmatch.fnmatchcase` ``what`` against the action OR
      the tool ref, mirroring
      :meth:`providers.RuleBasedAuthorizationProvider._rule_matches`.

    ``tool_ref_lookup`` resolves an action string to its qualified
    tool ref (``tool.bash`` → ``kolsch/bash``). When None,
    new-shape rules only match against the bare action (rules
    written as ``what: kolsch/bash`` won't fire — same loss the
    provider has when no catalog is supplied; ``/tools`` shows
    "(no matching rule)" in that case).

    Returns ``None`` when no rule matches — the caller renders
    that as "(default deny)" because the authz provider's
    A-I3 invariant is default-deny when no rule matches.
    """
    import fnmatch  # noqa: PLC0415

    action = f"tool.{tool_name}"
    tool_ref = tool_ref_lookup(action) if tool_ref_lookup is not None else None
    for rule in rules:
        if rule.who is not None:
            # Legacy regex path.
            try:
                if re.fullmatch(rule.who, principal) and re.fullmatch(
                    rule.what,
                    action,
                ):
                    return rule
            except re.error:
                # A malformed regex would have failed at provider load
                # time; defensive only — never raise out of /tools.
                continue
            continue
        # New-shape glob path: `what` matches action OR tool_ref.
        what_pattern = rule.what
        action_match = fnmatch.fnmatchcase(action, what_pattern)
        tool_ref_match = tool_ref is not None and fnmatch.fnmatchcase(
            tool_ref, what_pattern,
        )
        if action_match or tool_ref_match:
            return rule
    return None


def footer_hint_for_buffer(buffer_text: str) -> str:
    """Pure function: buffer text → footer-hint string.

    - Empty buffer or non-slash text → :data:`_DEFAULT_FOOTER_HINT`.
    - ``/`` typed but no recognisable command yet → default hint.
    - ``/<command>`` (possibly with args) → that command's
      ``usage`` line from the registry.
    - ``/<unknown>`` → default hint (the user hasn't typed
      something the registry recognises).

    Patch §5.4 — also drives the bottom_toolbar in
    ``cli.chat._build_prompt_session``.
    """
    text = buffer_text.lstrip()
    if not text or not text.startswith("/"):
        return _DEFAULT_FOOTER_HINT
    head = text.partition(" ")[0]
    command = _resolve_command_name(head)
    if command is None:
        # Partial command name — find the best matching prefix and
        # show its usage so the hint tracks as the user types.
        for candidate in _iter_visible_commands():
            if candidate.name.startswith(head):
                return candidate.usage
        return _DEFAULT_FOOTER_HINT
    return command.usage

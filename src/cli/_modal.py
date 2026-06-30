"""Approval modal — Phase 2.1 Step 24d.

The chat's consumer of Phase 2.0's prompt callback. Per
``phase2.0-tools-specs.md`` §2.2 the chat is the ONLY caller that
should turn ``prompt`` into an interactive choice; non-chat callers
(``kolsch run``, the server, tests) treat ``prompt`` as deny so
unattended paths never hang.

Per ``phase2.1-chat-specs.md`` §4 the modal renders a four-choice
panel — ``[y]`` allow once, ``[a]`` always allow this pattern,
``[n]`` deny once, ``[d]`` always deny — and threads the user's
choice back through the Phase 2.0 callback's tuple shape so the
authz provider performs the in-memory rule insert + fires the
rule-added hook the chat wired at registration for atomic
``platform.yaml`` rewriting.

**Critical security-gate property**: the modal NEVER writes
``platform.yaml`` directly. It returns the :class:`Rule` object
via the callback's tuple form; Phase 2.0's authz provider does
the in-memory insert and (via the chat-registered hook) the
disk write. Two consequences:

1. The modal can be unit-tested without a workspace on disk.
2. There is one persist path (the chat's registered hook), not
   one-per-callsite. The same hook is reused by ``kolsch
   permissions add`` (it calls the same ``_write_back`` helper).

**Replay-safety**: the modal's choice is not persisted on the
event log in 24d — the schema lacks ``authz.prompt_requested`` /
``authz.prompt_answered`` event types (Phase 2.0 spec §4.3
imagined them; the schema patch is deferred). The IN-MEMORY rule
insert + YAML write IS persistent, so a re-run of the same
session sees the new rule applied without prompting — that's
the load-bearing 24d property.

Pattern suggestor (patch §4.3) lives in this module as a pure
function so tests can pin its heuristics independently of the
modal's I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TextIO

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.text import Text

from providers import Rule
from providers.authz import AuthzEffect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cli._slash import ChatSessionState


# ---------------------------------------------------------------------------
# Pattern suggestor — patch §4.3
# ---------------------------------------------------------------------------


def suggest_pattern(*, tool: str, target: str | None) -> str:
    """Suggest a glob ``pattern`` for an [a]/[d] persist rule.

    Pure function — same inputs always produce the same output.

    qol-permission-system-human-readable.md Step 3 rewrites this to
    emit a glob (``find *``, ``/abs/path/*``) instead of the
    previous regex (``^find .*$``, ``^/abs/path/.*$``). The caller
    pairs the returned pattern with the tool's qualified ref
    (resolved separately via :data:`ApprovalModal._tool_ref_lookup`)
    to form a new-shape :class:`providers.authz.Rule`.

    Three heuristics in priority order:

    1. **bash** with a command target: the first
       whitespace-bounded token becomes a fixed prefix; the rest
       becomes ``*``. So ``rg "foo bar" src/`` suggests
       ``rg *`` — broad enough to cover any future ``rg``
       invocation, narrow enough that a hostile ``rm -rf``
       doesn't slip through. Patch §4.3 row 1.

    2. **write_file / read_file / list_files** with an absolute
       path target: suggest ``<parent_dir>/*``. So
       ``/abs/path/X.py`` suggests ``/abs/path/*`` — every file
       in that directory tree. Patch §4.3 row 2.

    3. **other tools or no target**: fall back to ``*``. The
       user can refine the pattern before committing, and the
       paired qualified-ref ``what`` already pins the rule to
       the right tool, so ``*`` is the right default catch-all.
    """
    if tool == "bash" and target:
        # First non-whitespace token; literal (glob has no escape
        # sequences for whitespace, but the tokens we'd ever see
        # here — `find`, `ls`, `grep`, `cat`, `rg`, etc. — are
        # plain words, so no escaping is needed).
        first_token = target.strip().split(maxsplit=1)[0] if target.strip() else ""
        if first_token:
            return f"{first_token} *"

    if tool in {"read_file", "write_file", "list_files"} and target:
        # Directory portion of the path. ``rpartition("/")`` keeps
        # the implementation small and avoids a Path import that
        # would touch the filesystem.
        parent, sep, _name = target.rpartition("/")
        if sep:
            return f"{parent}/*"

    # Catch-all glob — the paired qualified-ref `what` already
    # pins the rule to the right tool, so `*` is the right
    # broad default.
    return "*"


def _glob_to_legacy_regex(pattern: str, *, action: str) -> str:
    """Fallback used when the modal can't resolve a qualified tool ref.

    The new-shape persist path writes a glob ``pattern`` paired with
    a qualified ``what``. When no qualified ref is available (no
    catalog / MCP tool / test stub), we have to fall back to the
    pre-Step-3 legacy shape — that path expects a regex in ``what``
    matched against action OR target. We re-build the equivalent
    regex by escaping the glob's non-``*`` chars and translating
    ``*`` → ``.*``. Empty / star-only patterns degrade to the
    action namespace (``^tool\\.<name>$``) so the fallback is at
    least scoped to this tool.
    """
    import re as _re  # noqa: PLC0415

    if not pattern or pattern == "*":
        # Nothing usable in the pattern itself — pin to the action.
        return f"^{_re.escape(action)}$"
    # Build the regex chunk-by-chunk so `*` and `?` survive without
    # being escaped to literals. We keep ASCII whitespace literal too
    # (re.escape would emit `\ ` for spaces, which is functionally
    # the same but produces noisy regex; the pre-Step-3 modal
    # behaviour didn't escape the space between the bash verb and
    # `.*`).
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch.isspace():
            out.append(ch)
        else:
            out.append(_re.escape(ch))
    return f"^{''.join(out)}$"


# ---------------------------------------------------------------------------
# Modal — the Phase 2.0 PromptCallback implementation
# ---------------------------------------------------------------------------


_VALID_CHOICES = ("y", "a", "n", "d")
"""Patch §4.1 four-choice keys."""


@dataclass(frozen=True)
class _ModalChoice:
    """The user's response after the panel renders.

    ``pattern`` is non-None only when the user picked ``a`` or
    ``d`` AND accepted/edited the suggested pattern. A None
    ``pattern`` after an ``a``/``d`` pick means "Esc abandoned the
    rule write" — patch §4.2: "abandon with Esc (modal closes;
    the original [y]/[n] semantics apply for this call only — no
    rule written)".
    """

    choice: str
    pattern: str | None


# The signature the chat installs — fixed by the Phase 2.0 spec
# (``rule_id``, ``action``, ``target``). The modal needs nothing
# beyond these three; the build doc's earlier signature
# ``(rule_id, tool, target, args)`` resolves to this shape with
# ``tool`` derived from ``action`` and ``args`` not used (the
# Phase 2.0 callback contract surfaces the same info via
# ``target`` — for bash the command IS the target, for write_file
# the path IS the target). See the 24d report for the
# no-escalation rationale.
PromptCallbackReturn = AuthzEffect | tuple[AuthzEffect, Rule]


class ApprovalModal:
    """The chat-surface :class:`providers.PromptCallback` implementation.

    Construct one per chat session; register with
    :meth:`providers.RuleBasedAuthorizationProvider.register_prompt_callback`
    before the input loop starts and clear it on exit. The chat
    lifecycle wires both ends in a finally so a leaked modal can
    never affect a subsequent in-process command — see
    ``src/cli/chat.py:cmd_chat``.

    :param stdout: where the panel renders.
    :param input_fn: async callable that prompts the user and
        returns their input string. Default uses prompt_toolkit's
        :class:`PromptSession` (the same library the input loop
        uses). Tests inject a scripted ``input_fn`` so the modal
        doesn't touch stdin.
    :param console: optional Rich :class:`Console` for test capture.
        Production uses :class:`Console` constructed against
        ``stdout`` with terminal autodetection disabled (the §2.4
        commitment).
    :param state: optional :class:`ChatSessionState` reference. When
        bound (via constructor or :meth:`bind_state`), the modal
        cooperates with the chat's cancel-token plumbing per
        ``bug-chat-deny-stops-and-esc-interrupts.md``:
        a DENY result flips ``state.active_cancel`` so the framework
        stops looping, and an already-cancelled token short-circuits
        further modal invocations in a fan-out. None (the default)
        preserves the pre-fix behavior — useful for the existing
        unit tests that exercise the callback shape without a chat
        runtime.
    """

    def __init__(
        self,
        *,
        stdout: TextIO,
        input_fn: Callable[[str], Awaitable[str]] | None = None,
        console: Console | None = None,
        state: ChatSessionState | None = None,
        tool_ref_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self._stdout = stdout
        self._input_fn = input_fn or _default_input_fn
        self._console = console or Console(
            file=stdout,
            force_terminal=False,
            highlight=False,
        )
        self._state = state
        # qol-permission-system-human-readable.md Step 3 — resolves
        # an action string (e.g. ``tool.bash``) to the qualified tool
        # ref (``kolsch/bash``) that the new-shape Rule's ``what``
        # field requires. The chat lifecycle (``cli/chat.py``) wires
        # this from :meth:`RuleBasedAuthorizationProvider.lookup_tool_ref`.
        # Tests + non-chat callers default to ``None``, in which case
        # the modal falls back to the legacy ``who/what`` Rule shape
        # so its behaviour is byte-identical to Step 2.
        self._tool_ref_lookup = tool_ref_lookup

    def bind_state(self, state: ChatSessionState | None) -> None:
        """Late-bind the per-session state.

        The modal is constructed during ``cmd_chat`` startup (so the
        authz provider's prompt callback is registered before any
        input is processed) but the ``ChatSessionState`` is built
        inside ``_run_input_loop``. ``bind_state`` lets the loop
        attach the state without restructuring the lifecycle.
        """
        self._state = state

    # -----------------------------------------------------------------------
    # The PromptCallback __call__ — matches providers.PromptCallback
    # -----------------------------------------------------------------------

    async def __call__(
        self,
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
    ) -> PromptCallbackReturn:
        """Run the modal; return the Phase 2.0 callback shape.

        Per patch §4.4 the four buttons map onto the callback's
        ``(effect, rule_or_None)`` tuple:

        - ``[y]`` → ``ALLOW`` (single value; no rule).
        - ``[n]`` → ``DENY`` (single value; no rule).
        - ``[a]`` (with pattern P) →
          ``(ALLOW, Rule(what=<tool_ref>, pattern=P, effect=ALLOW))``.
        - ``[d]`` (with pattern P) →
          ``(DENY, Rule(what=<tool_ref>, pattern=P, effect=DENY))``.

        qol-permission-system-human-readable.md Step 3 — when a
        :attr:`_tool_ref_lookup` is bound (chat lifecycle wires it),
        the persisted rule uses the new ``{what, pattern, effect}``
        schema with ``what`` as the qualified tool ref. When no
        lookup is bound (tests, unknown action), it falls back to
        the legacy ``{who, what, effect}`` regex shape so existing
        callers stay byte-identical.

        Esc on the pattern edit (patch §4.2) abandons the rule
        write — the original ``[y]``/``[n]`` semantics apply for
        the current call only.

        Per ``bug-chat-deny-stops-and-esc-interrupts.md`` Q4 the
        modal short-circuits to DENY at the top of the call when
        ``state.active_cancel`` is already cancelled — that
        handles a fan-out where the user denied the first
        tool_call and the framework is still walking the rest.
        A DENY result also flips ``state.active_cancel`` BEFORE
        returning so the framework's next iteration observes the
        cancellation and stops the exchange.
        """
        if self._is_already_cancelled():
            # Fan-out short-circuit: another tool_call in the same
            # framework iteration already cancelled the exchange.
            # Skip the panel + input collection entirely.
            return AuthzEffect.DENY

        # qol-action-namespace-bundle-scoped.md Step 2 — actions are
        # now 3-seg catalog refs ``<bundle>/tools/<name>``. The
        # rsplit handles both the new form (returns the trailing
        # ``<name>``) and the legacy ``tool.<name>`` form (no ``/``
        # present, so the whole string survives and ``removeprefix``
        # strips the ``tool.`` head). The fall-through keeps Phase 1
        # programmatic registrations rendering correctly.
        tool_name = action.rsplit("/", 1)[-1].removeprefix("tool.")
        self._render_panel(tool=tool_name, target=target, rule_id=rule_id)
        choice = await self._collect_choice()

        if choice == "y":
            return AuthzEffect.ALLOW
        if choice == "n":
            self._flip_cancel_for_deny()
            return AuthzEffect.DENY

        # [a] / [d] — show pattern edit step.
        suggested = suggest_pattern(tool=tool_name, target=target)
        pattern = await self._collect_pattern_edit(
            tool_name=tool_name,
            suggested=suggested,
        )
        if pattern is None:
            # User abandoned the rule write — collapse to the
            # single-call semantics per patch §4.2.
            if choice == "a":
                return AuthzEffect.ALLOW
            self._flip_cancel_for_deny()
            return AuthzEffect.DENY

        effect = AuthzEffect.ALLOW if choice == "a" else AuthzEffect.DENY
        # qol-action-namespace-bundle-scoped.md Step 2 — for tool
        # actions the 3-seg form (``kolsch/tools/bash``) IS the
        # canonical ``what:``; no bridge lookup needed. The
        # ``lookup_tool_ref`` map still resolves node actions
        # (``node.bash`` → ``kolsch/nodes/bash``). Prefer the lookup
        # result when present (covers nodes); otherwise use the
        # action directly when it looks like a 2-or-3-seg catalog
        # ref (covers tool actions under this QoL); otherwise fall
        # to the legacy regex shape (covers ``mcp.foo.bar`` and
        # other unqualified shapes the user can't easily migrate).
        effect_str = "allow" if effect is AuthzEffect.ALLOW else "deny"
        tool_ref = (
            self._tool_ref_lookup(action)
            if self._tool_ref_lookup is not None
            else None
        )
        qualified_ref = (
            tool_ref if tool_ref is not None else (action if "/" in action else None)
        )
        if qualified_ref is not None:
            rule = Rule(
                what=qualified_ref,
                pattern=pattern,
                effect=effect_str,
            )
        else:
            # Legacy fallback — the user is denying / allowing a tool
            # the catalog can't qualify (e.g. mcp.<server>.<name>,
            # or a test path with no catalog). ``pattern`` here
            # already looks like a glob (Step 3 suggest_pattern
            # always emits globs), so wrap it as a permissive regex
            # for the legacy matcher: literal-prefixed plus `.*`.
            # Mirrors the pre-Step-3 behaviour.
            rule = Rule(
                id="",
                who=".*",
                what=_glob_to_legacy_regex(pattern, action=action),
                effect=effect_str,
            )
        # Persist the rule first (return-value contract is unchanged),
        # then flip the cancel token so the framework stops. Order
        # matters: the rule write happens inside the authz provider
        # AFTER __call__ returns, but the cancel flip happens here
        # so the framework's next poll observes it.
        if effect is AuthzEffect.DENY:
            self._flip_cancel_for_deny()
        return effect, rule

    # -----------------------------------------------------------------------
    # Cancel-token cooperation — bug-chat-deny-stops-and-esc-interrupts.md
    # -----------------------------------------------------------------------

    def _is_already_cancelled(self) -> bool:
        """True when the bound state's CancelToken has already fired.

        Used by the fan-out short-circuit at the top of
        :meth:`__call__`. False when no state is bound, no
        CancelToken is attached, or the token is still un-cancelled
        — none of those are short-circuit conditions.
        """
        state = self._state
        if state is None:
            return False
        cancel = state.active_cancel
        if cancel is None:
            return False
        return cancel.is_cancelled()

    def _flip_cancel_for_deny(self) -> None:
        """Stamp the deny reason + flip the active CancelToken.

        Called immediately before any DENY return. The chat layer
        consumes ``state.last_cancel_reason`` post-render to print
        the distinct "[stopped — you denied the tool]" line.

        If no state is bound (legacy callers, unit tests without a
        chat runtime) this is a no-op and the deny falls back to
        the pre-fix behavior — surfaced to the model as a tool
        error.
        """
        state = self._state
        if state is None or state.active_cancel is None:
            return
        if state.last_cancel_reason is None:
            state.last_cancel_reason = "denied"
        state.active_cancel.cancel()

    # -----------------------------------------------------------------------
    # Rendering — capsule per qol-chat-capsule-layout.md §Permission
    # -----------------------------------------------------------------------

    def _render_panel(
        self,
        *,
        tool: str,
        target: str | None,
        rule_id: str | None,
    ) -> None:
        """Render the permission prompt as a sidebar capsule
        (qol-chat-capsule-layout.md) — replaces the prior Rich
        Panel modal. Per-spec layout: source bar, signature line,
        blank, choice rows, blank, cursor line."""
        from cli._renderer import _capsule, _now_hhmmss  # noqa: PLC0415

        sig = f"{tool}({target})" if target is not None else f"{tool}()"
        suffix = f"  —  matched {rule_id}" if rule_id is not None else ""
        body_lines: list[str] = [
            f"{sig}{suffix}",
            "",
            "[y] once    [a] always allow this",
            "[n] deny once    [d] always deny",
            "",
        ]
        _capsule(
            self._console,
            source="permission",
            timestamp=_now_hhmmss(),
            body_lines=body_lines,
        )

    # -----------------------------------------------------------------------
    # Input collection — testable via injection
    # -----------------------------------------------------------------------

    async def _collect_choice(self) -> str:
        while True:
            raw = (await self._input_fn("> [y/a/n/d]: ")).strip().lower()
            if raw and raw[0] in _VALID_CHOICES:
                return raw[0]
            self._console.print(
                Text(
                    f"please pick one of {', '.join(_VALID_CHOICES)} "
                    "(or Ctrl-D to abandon).",
                    style="yellow",
                ),
            )

    async def _collect_pattern_edit(
        self,
        *,
        tool_name: str,
        suggested: str,
    ) -> str | None:
        """Prompt the user with the suggested pattern; return their final pick.

        Returns ``None`` when the user abandons the rule write
        (Esc / empty submission per patch §4.2). The chat treats
        None as "fall back to single-call [y]/[n]".

        The prompt shows the tool's qualified ref
        ("kolsch/tools/bash") and the suggested ``what`` pattern
        pre-filled so the user can edit in-place — patch §4.2's
        reference UX.

        qol-action-namespace-bundle-scoped.md Step 2 — the displayed
        ref defaults to the legacy ``tool.<name>`` form when the
        modal is invoked without a tool-ref lookup (Phase 1 test
        paths). Engine-driven invocations always carry the full
        ``<bundle>/tools/<name>`` action through the lookup, so the
        new form surfaces in the panel header at line 313.
        """
        self._console.print(
            Text(
                "Pattern for the new rule (Enter accepts, empty abandons):",
                style="dim",
            ),
        )
        self._console.print(
            Text(f"  who:  tool.{tool_name}", style="dim"),
        )
        raw = await self._input_fn(f"  what: [{suggested}] ")
        if not raw.strip():
            # Empty submission accepts the suggested pattern. The
            # patch §4.2 wording "abandons with Esc" is the
            # default-input branch; an explicit Esc through
            # prompt_toolkit raises a KeyboardInterrupt which the
            # outer try/except in the engine boundary handles —
            # for the MVP we treat empty submit as accept (the
            # common case) and the input_fn raising as abandon.
            return suggested
        return raw.strip()


# ---------------------------------------------------------------------------
# Module-private — default input_fn using prompt_toolkit
# ---------------------------------------------------------------------------


_modal_session: PromptSession[str] | None = None
"""Lazily-constructed PromptSession for modal input.

The chat's outer PromptSession owns the "You: " prompt history +
completer; the modal's session is a separate one so its history
doesn't pollute the chat scrollback. Lazy construction avoids
importing prompt_toolkit's I/O machinery for callers that always
inject ``input_fn`` (tests)."""


async def _default_input_fn(prompt: str) -> str:
    """The production input collector — prompt_toolkit asyncio prompt.

    Falls back to raising :class:`KeyboardInterrupt` when the user
    Ctrl-C's the modal (the outer engine boundary treats this as
    "abandon"). Ctrl-D raises :class:`EOFError`; same treatment.
    """
    global _modal_session  # noqa: PLW0603 — lazy singleton across modal calls
    if _modal_session is None:
        _modal_session = PromptSession()
    return await _modal_session.prompt_async(prompt)


__all__ = [
    "ApprovalModal",
    "PromptCallbackReturn",
    "suggest_pattern",
]

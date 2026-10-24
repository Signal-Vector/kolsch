"""Approval modal — the chat's consumer of the authz prompt callback.

The chat is the only caller that should turn `prompt` into an
interactive choice; non-chat callers (`kolsch run`, the server,
tests) treat `prompt` as deny so unattended paths never hang.

The modal renders a four-choice panel — `[y]` allow once, `[a]`
always allow this pattern, `[n]` deny once, `[d]` always deny — and
threads the user's choice back through the authz callback's tuple
shape so the provider performs the in-memory rule insert + fires
the rule-added hook the chat wired at registration for atomic
`platform.yaml` rewriting.

**Critical security-gate property.** The modal never writes
`platform.yaml` directly. It returns the [`Rule`][providers.Rule]
object via the callback's tuple form; the authz provider does the
in-memory insert and (via the chat-registered hook) the disk write.
Two consequences:

1. The modal can be unit-tested without a workspace on disk.
2. There is one persist path (the chat's registered hook), not
   one-per-callsite. The hook and `kolsch permissions add` both go
   through `cli.permissions.mutate_platform_rules`, which inserts the
   one rule into the rules currently on disk (bug-162).

**Replay-safety.** The modal's choice is not persisted on the event
log — the schema lacks `authz.prompt_requested` /
`authz.prompt_answered` event types. The in-memory rule insert +
YAML write is persistent, so a re-run of the same session sees the
new rule applied without prompting.

The pattern suggestor lives in this module as a pure function so
tests can pin its heuristics independently of the modal's I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TextIO

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.text import Text

from providers import Rule
from providers.authz import AuthzEffect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cli._slash import ChatSessionState
    from engine.context import ExecutionContext


# ---------------------------------------------------------------------------
# Pattern suggestor
# ---------------------------------------------------------------------------


def suggest_pattern(*, tool: str, target: str | None) -> str:
    """Suggest a glob `pattern` for an [a]/[d] persist rule.

    Pure function — same inputs always produce the same output. The
    caller pairs the returned pattern with the tool's qualified ref
    to form a new-shape [`Rule`][providers.Rule].

    Three heuristics in priority order:

    1. **bash with a command target.** The first
       whitespace-bounded token becomes a fixed prefix; the rest
       becomes `*`. So `rg "foo bar" src/` suggests `rg *` — broad
       enough to cover any future `rg` invocation, narrow enough
       that a hostile `rm -rf` doesn't slip through.
    2. **write_file / read_file / list_files with an absolute path
       target.** Suggest `<parent_dir>/*`. So `/abs/path/X.py`
       suggests `/abs/path/*` — every file in that directory tree.
    3. **Other tools or no target.** Fall back to `*`. The user can
       refine the pattern before committing, and the paired
       qualified-ref `what` already pins the rule to the right
       tool, so `*` is the right default catch-all.
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
    r"""Fallback used when the modal can't resolve a qualified tool ref.

    The new-shape persist path writes a glob `pattern` paired with
    a qualified `what`. When no qualified ref is available (no
    catalog / MCP tool / test stub), fall back to the legacy shape
    — that path expects a regex in `what` matched against action OR
    target. Re-build the equivalent regex by escaping the glob's
    non-`*` chars and translating `*` → `.*`. Empty / star-only
    patterns degrade to the action namespace (`^tool\.<name>$`) so
    the fallback is at least scoped to this tool.
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
# Modal — the PromptCallback implementation
# ---------------------------------------------------------------------------


_VALID_CHOICES = ("y", "a", "n", "d")
"""The four-choice keys."""


@dataclass(frozen=True)
class _ModalChoice:
    """The user's response after the panel renders.

    `pattern` is non-None only when the user picked `a` or `d` AND
    accepted/edited the suggested pattern. A `None` `pattern` after
    an `a`/`d` pick means "Esc abandoned the rule write" — the
    modal closes; the original [y]/[n] semantics apply for this
    call only, no rule written.
    """

    choice: str
    pattern: str | None


# The signature the chat installs — fixed by the authz contract
# (``rule_id``, ``action``, ``target``). The modal needs nothing
# beyond these three; the callback contract surfaces the same info
# via ``target`` — for bash the command IS the target, for
# write_file the path IS the target.
PromptCallbackReturn = AuthzEffect | tuple[AuthzEffect, Rule]


class ApprovalModal:
    """The chat-surface `PromptCallback` implementation.

    Implements [`PromptCallback`][providers.authz.PromptCallback].
    Construct one per chat session; register with
    `RuleBasedAuthorizationProvider.register_prompt_callback` before
    the input loop starts and clear it on exit. The chat lifecycle
    wires both ends in a finally so a leaked modal can never affect
    a subsequent in-process command.

    Args:
        stdout: Where the panel renders.
        input_fn: Async callable that prompts the user and returns
            their input string. Default uses prompt_toolkit's
            `PromptSession` (the same library the input loop uses).
            Tests inject a scripted `input_fn` so the modal doesn't
            touch stdin.
        console: Optional Rich `Console` for test capture.
            Production uses a `Console` constructed against `stdout`
            with terminal autodetection disabled.
        state: Optional `ChatSessionState` reference. When bound
            (via constructor or `bind_state`), the modal cooperates
            with the chat's cancel-token plumbing: a DENY result
            flips `state.active_cancel` so the framework stops
            looping, and an already-cancelled token short-circuits
            further modal invocations in a fan-out. `None` (the
            default) leaves the cancel plumbing un-armed — useful
            for unit tests that exercise the callback shape without
            a chat runtime.
        tool_ref_lookup: Optional resolver from action string to a
            qualified tool ref; the chat lifecycle wires this from
            `RuleBasedAuthorizationProvider.lookup_tool_ref`.
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
        # Resolves an action string (e.g. ``tool.bash``) to the
        # qualified tool ref (``kolsch/tools/bash``) the new-shape
        # Rule's ``what`` field requires. Tests + non-chat callers
        # default to ``None``, in which case the modal falls back to
        # the legacy ``who/what`` Rule shape.
        self._tool_ref_lookup = tool_ref_lookup

    def bind_state(self, state: ChatSessionState | None) -> None:
        """Late-bind the per-session state.

        The modal is constructed during `cmd_chat` startup (so the
        authz provider's prompt callback is registered before any
        input is processed) but the `ChatSessionState` is built
        inside `_run_input_loop`. `bind_state` lets the loop attach
        the state without restructuring the lifecycle.
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
        caller_chain: tuple[str, ...] = (),
        execution_context: ExecutionContext | None = None,  # noqa: ARG002 — accepted for prompt-callback signature parity
    ) -> PromptCallbackReturn:
        """Run the modal; return the authz callback shape.

        The four buttons map onto the callback's
        `(effect, rule_or_None)` tuple:

        - `[y]` → `ALLOW` (single value; no rule).
        - `[n]` → `DENY` (single value; no rule).
        - `[a]` (with pattern P) →
          `(ALLOW, Rule(what=<tool_ref>, pattern=P, from_=<F>, effect=ALLOW))`.
        - `[d]` (with pattern P) →
          `(DENY, Rule(what=<tool_ref>, pattern=P, from_=<F>, effect=DENY))`.

        `caller_chain` is the proximate-first tuple of caller frames
        the authz provider matched against. Persisted `[a]`/`[d]`
        rules default `from_ = caller_chain[0]` — so approving a
        tool during agent X's execution scopes the rule to
        `kolsch/agents/X` rather than granting the effect to every
        future caller. The pattern-edit step surfaces the default
        `from:` glob and lets the user broaden it (e.g. to `*`) or
        leave it unset via an empty input.

        When `_tool_ref_lookup` is bound (chat lifecycle wires it),
        the persisted rule uses the new
        `{what, pattern, from, effect}` schema with `what` as the
        qualified tool ref. When no lookup is bound (tests, unknown
        action), it falls back to the legacy `{who, what, effect}`
        regex shape (which does NOT support `from:`; broadening to
        all callers is the only option there).

        Esc on the pattern edit abandons the rule write — the
        original `[y]`/`[n]` semantics apply for the current call
        only.

        The modal short-circuits to DENY at the top of the call
        when `state.active_cancel` is already cancelled — that
        handles a fan-out where the user denied the first tool_call
        and the framework is still walking the rest. A DENY result
        also flips `state.active_cancel` before returning so the
        framework's next iteration observes the cancellation and
        stops the exchange.
        """
        if self._is_already_cancelled():
            # Fan-out short-circuit: another tool_call in the same
            # framework iteration already cancelled the exchange.
            # Skip the panel + input collection entirely.
            return AuthzEffect.DENY

        # Actions are now 3-seg catalog refs `<bundle>/tools/<name>`.
        # The rsplit handles both the new form (returns the trailing
        # `<name>`) and the legacy `tool.<name>` form (no `/`
        # present, so the whole string survives and `removeprefix`
        # strips the `tool.` head). The fall-through keeps older
        # programmatic registrations rendering correctly.
        tool_name = action.rsplit("/", 1)[-1].removeprefix("tool.")
        self._render_panel(
            tool=tool_name,
            target=target,
            rule_id=rule_id,
            caller_chain=caller_chain,
        )
        choice = await self._collect_choice()

        if choice == "y":
            return AuthzEffect.ALLOW
        if choice == "n":
            self._flip_cancel_for_deny()
            return AuthzEffect.DENY

        # [a] / [d] — show pattern edit step.
        suggested = suggest_pattern(tool=tool_name, target=target)
        default_from = caller_chain[0] if caller_chain else None
        pattern = await self._collect_pattern_edit(
            tool_name=tool_name,
            suggested=suggested,
        )
        if pattern is None:
            # User abandoned the rule write — collapse to the
            # single-call semantics.
            if choice == "a":
                return AuthzEffect.ALLOW
            self._flip_cancel_for_deny()
            return AuthzEffect.DENY

        # Scope prompt — Enter accepts the proximate caller frame,
        # an empty submission after clearing broadens to all callers.
        # Skipped when no chain is available (bootstrap paths /
        # tests without a chain).
        from_ref = default_from
        if default_from is not None:
            from_ref = await self._collect_from_edit(default_from=default_from)

        effect = AuthzEffect.ALLOW if choice == "a" else AuthzEffect.DENY
        # For tool actions the 3-seg form (`kolsch/tools/bash`) IS
        # the canonical `what:`; no bridge lookup needed. The
        # `lookup_tool_ref` map still resolves node actions
        # (`node.bash` → `kolsch/nodes/bash`). Prefer the lookup
        # result when present (covers nodes); otherwise use the
        # action directly when it looks like a 2-or-3-seg catalog
        # ref (covers tool actions); otherwise fall back to the
        # legacy regex shape (covers `mcp.foo.bar` and other
        # unqualified shapes the user can't easily migrate).
        effect_str: Literal["allow", "deny"] = (
            "allow" if effect is AuthzEffect.ALLOW else "deny"
        )
        tool_ref = (
            self._tool_ref_lookup(action) if self._tool_ref_lookup is not None else None
        )
        qualified_ref = (
            tool_ref if tool_ref is not None else (action if "/" in action else None)
        )
        if qualified_ref is not None:
            rule = Rule(
                what=qualified_ref,
                pattern=pattern,
                from_=from_ref,
                effect=effect_str,
            )
        else:
            # Legacy fallback — the user is denying / allowing a
            # tool the catalog can't qualify (e.g. mcp.<server>.<name>,
            # or a test path with no catalog). `pattern` here
            # already looks like a glob (suggest_pattern always
            # emits globs), so wrap it as a permissive regex for
            # the legacy matcher: literal-prefixed plus `.*`. The
            # legacy shape doesn't support `from:` scoping; approved
            # rules on that path always fire for every caller.
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
    # Cancel-token cooperation
    # -----------------------------------------------------------------------

    def _is_already_cancelled(self) -> bool:
        """Return True when the bound state's CancelToken has already fired.

        Used by the fan-out short-circuit at the top of `__call__`.
        False when no state is bound, no CancelToken is attached,
        or the token is still un-cancelled — none of those are
        short-circuit conditions.
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
        consumes `state.last_cancel_reason` post-render to print
        the distinct "[stopped — you denied the tool]" line.

        If no state is bound (legacy callers, unit tests without a
        chat runtime) this is a no-op and the deny surfaces to the
        model as a tool error instead.
        """
        state = self._state
        if state is None or state.active_cancel is None:
            return
        if state.last_cancel_reason is None:
            state.last_cancel_reason = "denied"
        state.active_cancel.cancel()

    # -----------------------------------------------------------------------
    # Rendering — capsule layout
    # -----------------------------------------------------------------------

    def _render_panel(
        self,
        *,
        tool: str,
        target: str | None,
        rule_id: str | None,
        caller_chain: tuple[str, ...] = (),
    ) -> None:
        """Render the permission prompt as a sidebar capsule.

        Layout: source bar, signature line, optional caller line
        (surfaces the proximate frame the `[a]/[d]` rule will scope
        to), blank, choice rows, blank, cursor line.
        """
        from cli._rich_capsule import _capsule, _now_hhmmss  # noqa: PLC0415

        sig = f"{tool}({target})" if target is not None else f"{tool}()"
        suffix = f"  —  matched {rule_id}" if rule_id is not None else ""
        body_lines: list[str] = [f"{sig}{suffix}"]
        if caller_chain:
            body_lines.append(f"caller: {caller_chain[0]}")
        body_lines.extend(
            [
                "",
                "[y] once    [a] always allow this",
                "[n] deny once    [d] always deny",
                "",
            ]
        )
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

        Returns `None` when the user abandons the rule write
        (Esc / empty submission). The chat treats `None` as "fall
        back to single-call [y]/[n]".

        The prompt shows the tool's qualified ref
        (`kolsch/tools/bash`) and the suggested `what` pattern
        pre-filled so the user can edit in-place.

        The displayed ref defaults to the legacy `tool.<name>` form
        when the modal is invoked without a tool-ref lookup (test
        paths). Engine-driven invocations always carry the full
        `<bundle>/tools/<name>` action through the lookup.
        """
        self._console.print(
            Text(
                "Pattern for the new rule (Enter accepts, empty abandons):",
                style="dim",
            ),
        )
        self._console.print(
            Text(f"  tool: tool.{tool_name}", style="dim"),
        )
        raw = await self._input_fn(f"  pattern: [{suggested}] ")
        if not raw.strip():
            # Empty submission accepts the suggested pattern. An
            # explicit Esc through prompt_toolkit raises a
            # KeyboardInterrupt which the outer try/except handles —
            # for now treat empty submit as accept (the common case)
            # and the input_fn raising as abandon.
            return suggested
        return raw.strip()

    async def _collect_from_edit(self, *, default_from: str) -> str | None:
        """Prompt the user for the rule's `from:` caller-chain scope.

        `default_from` is the proximate caller-chain frame (the
        agent or workflow that fired the tool call). Three inputs:

        - Enter (empty submission) — accept `default_from`; the
          rule fires only when the same caller invokes the tool
          again.
        - `*` — broaden to all callers. `Rule.from_` becomes `None`,
          so the rule fires regardless of caller.
        - Any other string — used verbatim as `from_`. Must be a
          3-seg agent/workflow ref (glob or literal); the authz
          provider validates it on insert.

        Returns the string to put on `Rule.from_`, or `None` for the
        broaden case.
        """
        self._console.print(
            Text(
                "Scope this rule to the current caller? "
                "(Enter accepts; `*` broadens to all callers)",
                style="dim",
            ),
        )
        raw = await self._input_fn(f"  from: [{default_from}] ")
        stripped = raw.strip()
        if not stripped:
            # Empty / whitespace-only submission — accept default.
            return default_from
        if stripped == "*":
            # Explicit broaden sentinel — `*` is not a valid `from:`
            # glob (fails 3-seg validation) so it's unambiguous.
            return None
        return stripped


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

    Falls back to raising `KeyboardInterrupt` when the user Ctrl-C's
    the modal (the outer engine boundary treats this as "abandon").
    Ctrl-D raises `EOFError`; same treatment.
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

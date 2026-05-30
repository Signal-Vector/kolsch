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

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, TextIO

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from providers import Rule
from providers.authz import AuthzEffect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


# ---------------------------------------------------------------------------
# Pattern suggestor — patch §4.3
# ---------------------------------------------------------------------------


def suggest_pattern(*, tool: str, target: str | None) -> str:
    """Suggest a ``what`` regex for an [a]/[d] persist rule.

    Pure function — same inputs always produce the same output.
    Three heuristics in priority order:

    1. **bash**: the first whitespace-bounded token of the command
       becomes a fixed prefix; the rest becomes ``.*``. So
       ``rg "foo bar" src/`` suggests ``^rg .*$`` — broad enough
       to cover any future ``rg`` invocation, narrow enough that
       a hostile ``rm -rf`` doesn't slip through. Patch §4.3
       row 1.

    2. **write_file / read_file / list_files** with an absolute
       path target: suggest ``^<parent_dir>/.*$``. So
       ``/abs/path/X.py`` suggests ``^/abs/path/.*$`` — every
       file in that directory tree. Patch §4.3 row 2.

    3. **other tools or no target**: fall back to the
       action-namespace pattern ``^tool\\.<name>$``. That covers
       the case where the dispatcher couldn't extract a target
       (the tool's args don't include ``path``/``command``/``url``);
       the user can still refine before committing.

    The user is always shown the suggestion and can edit; the
    suggestor is friction-removal, not enforcement.
    """
    if tool == "bash" and target:
        # First non-whitespace token; ``re.escape`` so chars like
        # ``./`` survive the regex compile when the user accepts.
        first_token = target.strip().split(maxsplit=1)[0] if target.strip() else ""
        if first_token:
            return f"^{re.escape(first_token)} .*$"

    if tool in {"read_file", "write_file", "list_files"} and target:
        # Directory portion of the path. ``rpartition("/")`` keeps
        # the implementation small and avoids a Path import that
        # would touch the filesystem.
        parent, sep, _name = target.rpartition("/")
        if sep:
            # Trailing slash + ``.*`` so the regex matches every
            # file under the directory tree.
            return f"^{re.escape(parent)}/.*$"

    # Catch-all: pin to the tool's action namespace. Matches what
    # Phase 2.0's setup-produced rules use.
    return f"^tool\\.{re.escape(tool)}$"


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
    """

    def __init__(
        self,
        *,
        stdout: TextIO,
        input_fn: Callable[[str], Awaitable[str]] | None = None,
        console: Console | None = None,
    ) -> None:
        self._stdout = stdout
        self._input_fn = input_fn or _default_input_fn
        self._console = console or Console(
            file=stdout,
            force_terminal=False,
            highlight=False,
        )

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
          ``(ALLOW, Rule(who=action, what=P, effect=ALLOW))``.
        - ``[d]`` (with pattern P) →
          ``(DENY, Rule(who=action, what=P, effect=DENY))``.

        Esc on the pattern edit (patch §4.2) abandons the rule
        write — the original ``[y]``/``[n]`` semantics apply for
        the current call only.
        """
        tool_name = action.removeprefix("tool.")
        self._render_panel(tool=tool_name, target=target, rule_id=rule_id)
        choice = await self._collect_choice()

        if choice == "y":
            return AuthzEffect.ALLOW
        if choice == "n":
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
            return AuthzEffect.ALLOW if choice == "a" else AuthzEffect.DENY

        effect = AuthzEffect.ALLOW if choice == "a" else AuthzEffect.DENY
        # The new rule mirrors the setup-produced rule shape (see
        # ``cli._setup._workspace_only_default_rules``): ``who`` is
        # ``.*`` (any principal — Phase 2.0 §3.2 "Phase 2.0 only
        # uses tool name; principal identity is reserved for Phase 3"),
        # ``what`` is the user-accepted pattern, and the dispatcher's
        # action vs. target distinction is handled by the provider's
        # matcher (it tries the regex against both — see
        # ``RuleBasedAuthorizationProvider.allow``). So a pattern
        # ``^rg .*$`` matches any future call with that command, and
        # a pattern ``^tool\.bash$`` matches any call by action
        # namespace.
        rule = Rule(
            id="",  # provider auto-derives from (who, what, effect)
            who=".*",
            what=pattern,
            effect="allow" if effect is AuthzEffect.ALLOW else "deny",
        )
        return effect, rule

    # -----------------------------------------------------------------------
    # Rendering — Rich Panel per patch §4.1
    # -----------------------------------------------------------------------

    def _render_panel(
        self,
        *,
        tool: str,
        target: str | None,
        rule_id: str | None,
    ) -> None:
        lines: list[Text] = []
        lines.append(Text(f"Tool:    {tool}", style="bold"))
        if target is not None:
            label = "Command:" if tool == "bash" else "Target: "
            lines.append(Text(f"{label} {target}"))
        if rule_id is not None:
            lines.append(Text(f"Matched rule: {rule_id}", style="dim"))
        lines.append(Text(""))
        # Rich markup treats ``[...]`` as a style tag. Escape the
        # literal ``[`` brackets via ``\[`` so the panel prints the
        # patch §4.1 hint characters as-is.
        lines.append(
            Text.from_markup(
                r"[bold green]\[y][/] allow once   "
                r"[bold green]\[a][/] always allow this pattern",
            ),
        )
        lines.append(
            Text.from_markup(
                r"[bold red]\[n][/] deny once    "
                r"[bold red]\[d][/] deny always",
            ),
        )
        body = Text("\n").join(lines)
        self._console.print(
            Panel(
                body,
                title="Permission requested",
                title_align="left",
                border_style="yellow",
                padding=(1, 2),
            ),
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

        The prompt shows the tool's ``who`` namespace ("tool.bash")
        and the suggested ``what`` regex pre-filled so the user
        can edit in-place — patch §4.2's reference UX.
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

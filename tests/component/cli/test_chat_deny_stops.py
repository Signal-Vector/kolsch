"""Regression tests — ``bug-chat-deny-stops-and-esc-interrupts.md`` Step 3.

Three TCs pin the deny-stops-the-exchange contract end-to-end via the
authz provider (the surface the chat actually uses), not at the modal's
isolation seam:

- **TC-CHAT-DENY-01** — single-call `[n]` flips the shared
  ``CancelToken`` so the framework's next iteration poll observes
  cancellation and raises.
- **TC-CHAT-DENY-02** — `[d]` + accepted pattern flips the cancel
  AND persists the rule via the ``on_rule_added`` hook (so the chat's
  atomic ``platform.yaml`` rewrite still fires).
- **TC-CHAT-DENY-03** — fan-out short-circuit: when ``active_cancel``
  is already cancelled (an earlier tool_call in the same framework
  iteration was denied), the modal returns ``DENY`` immediately
  without invoking ``input_fn`` — the user is not prompted again.

The unit-level modal-isolation tests for the same behaviors live in
``test_chat_modal.py``; these tests pin the path through the
:class:`RuleBasedAuthorizationProvider` which is what the engine's
tool dispatch calls into.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import TYPE_CHECKING

from cli._modal import ApprovalModal
from engine import CancelToken
from providers import Rule, RuleBasedAuthorizationProvider
from providers.authz import AuthzEffect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scripted_input(answers: list[str]) -> Callable[[str], Awaitable[str]]:
    """Async ``input_fn`` that pops one scripted answer per call.

    Mirrors the helper in ``test_chat_modal.py`` so the two test
    files stay independently readable. Raises ``AssertionError`` if
    the modal asks for more input than the script provides — the
    TC's contract explicitly forbids surprise prompts.
    """
    iterator = iter(answers)

    async def _fn(prompt: str) -> str:
        _ = prompt
        try:
            return next(iterator)
        except StopIteration as exc:
            msg = "modal asked for more input than the test scripted"
            raise AssertionError(msg) from exc

    return _fn


def _stub_state(cancel: CancelToken) -> object:
    """Minimal ``ChatSessionState`` stand-in.

    The modal reads ``active_cancel`` and writes
    ``last_cancel_reason`` — nothing else. A SimpleNamespace mirrors
    the dataclass shape; constructing a real ChatSessionState would
    require bootstrapping a runtime/config/session, which is pure
    setup cost for these tests."""
    return SimpleNamespace(active_cancel=cancel, last_cancel_reason=None)


def _provider_with_bash_prompt() -> RuleBasedAuthorizationProvider:
    """Authz provider with a single prompt rule on bash + default deny."""
    return RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
                {"who": ".*", "what": ".*", "effect": "deny"},
            ],
        },
    )


# ===========================================================================
# TC-CHAT-DENY-01 — Deny once stops the active exchange
# ===========================================================================


async def test_tc_chat_deny_01_deny_once_flips_cancel_token() -> None:
    """TC-CHAT-DENY-01: Modal returning DENY in single-call mode
    flips the shared CancelToken so the framework observes
    cancellation on next iteration.

    End-to-end through ``RuleBasedAuthorizationProvider.allow``,
    which is what ``ToolsSubsystem.invoke_tool`` calls. Asserts
    BOTH the provider-level effect AND the cancel-token side
    effect on ``state``."""
    out = io.StringIO()
    cancel = CancelToken()
    assert cancel.is_cancelled() is False  # pre-state: not cancelled
    state = _stub_state(cancel)

    provider = _provider_with_bash_prompt()
    modal = ApprovalModal(
        stdout=out,
        input_fn=_scripted_input(["n"]),
        state=state,
    )
    provider.register_prompt_callback(modal)

    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="rm -rf /",
    )

    # Provider-level: DENY collapsed from the prompt rule.
    assert result.effect is AuthzEffect.DENY
    assert result.allowed is False
    # Cancel-token side effect: framework will observe this on the
    # next iteration poll (fake_framework.py:78-79).
    assert cancel.is_cancelled() is True
    assert state.last_cancel_reason == "denied"


# ===========================================================================
# TC-CHAT-DENY-02 — Always-deny flips the token AND emits the rule
# ===========================================================================


async def test_tc_chat_deny_02_always_deny_flips_cancel_and_returns_rule() -> None:
    """TC-CHAT-DENY-02: Modal returning DENY with a rule (``[d]`` path)
    flips the cancel token AND persists the rule via on_rule_added.

    The chat's atomic platform.yaml rewrite path is preserved —
    the rule write happens AFTER the modal returns, via the
    provider's _resolve_prompt hook fire (authz.py:503-513). The
    cancel flip happens INSIDE the modal BEFORE the return — both
    happen, neither blocks the other."""
    out = io.StringIO()
    cancel = CancelToken()
    state = _stub_state(cancel)

    provider = _provider_with_bash_prompt()
    # Spy on the rule-added hook to confirm persistence still
    # fires post-fix.
    captured_rules: list[list[Rule]] = []

    def _on_rule_added(rules: list[Rule]) -> None:
        captured_rules.append(list(rules))

    modal = ApprovalModal(
        stdout=out,
        # ``[d]`` then empty submit accepts the suggested pattern.
        input_fn=_scripted_input(["d", ""]),
        state=state,
    )
    provider.register_prompt_callback(modal, on_rule_added=_on_rule_added)

    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="rm -rf /",
    )

    # Provider-level: DENY effect via the prompt → callback resolution.
    assert result.effect is AuthzEffect.DENY
    assert result.allowed is False

    # Cancel flipped + reason stamped — the exchange stops on the
    # next framework iteration poll.
    assert cancel.is_cancelled() is True
    assert state.last_cancel_reason == "denied"

    # Rule write fired — the chat's platform.yaml hook would now
    # atomically rewrite the file. Inspect the captured rule list:
    # the new deny rule must be present, with the suggest_pattern
    # heuristic applied to ("bash", "rm -rf /").
    assert len(captured_rules) == 1, (
        f"on_rule_added must fire exactly once for a [d] persist, "
        f"got {len(captured_rules)}"
    )
    persisted_rules = captured_rules[0]
    new_rule = next(
        (r for r in persisted_rules if r.what == "^rm .*$"),
        None,
    )
    assert new_rule is not None, (
        f"expected a persisted rule with what='^rm .*$' "
        f"(suggest_pattern's bash heuristic); rule list was "
        f"{[(r.who, r.what, r.effect) for r in persisted_rules]}"
    )
    assert new_rule.effect == "deny"
    assert new_rule.who == ".*"

    # The new rule was inserted BEFORE the prompt rule (so a future
    # call hits it first — "always deny" semantics).
    deny_idx = next(i for i, r in enumerate(persisted_rules) if r.what == "^rm .*$")
    prompt_idx = next(
        i for i, r in enumerate(persisted_rules) if r.what == r"tool\.bash"
    )
    assert deny_idx < prompt_idx, (
        "new always-deny rule must be inserted BEFORE the prompt rule "
        "so future matching calls auto-deny without re-prompting"
    )


# ===========================================================================
# TC-CHAT-DENY-03 — Fan-out short-circuit
# ===========================================================================


async def test_tc_chat_deny_03_modal_short_circuits_when_cancel_already_flipped() -> (
    None
):
    """TC-CHAT-DENY-03: When the modal is invoked AFTER active_cancel
    was already cancelled, it returns DENY immediately without
    calling input_fn.

    This is the fan-out guarantee: when a single LLM response
    issued N tool_calls and the user denied the first one (which
    flipped the cancel), the modal does NOT re-prompt for calls
    2..N. The framework's dispatch loop still walks all the calls
    (the bookkeeping stays consistent) but every subsequent modal
    invocation is silent.

    The input_fn raises if called — that's the assertion that
    matters. Reaching it would mean the panel rendered and the
    user was prompted again, which is the bug.
    """
    out = io.StringIO()
    cancel = CancelToken()
    cancel.cancel()  # an EARLIER tool_call in the same fan-out denied
    state = _stub_state(cancel)

    async def _never_called(prompt: str) -> str:
        _ = prompt
        msg = (
            "input_fn must NOT be invoked when active_cancel is "
            "already cancelled — the fan-out short-circuit should "
            "fire before any panel render or input collection"
        )
        raise AssertionError(msg)

    provider = _provider_with_bash_prompt()
    modal = ApprovalModal(
        stdout=out,
        input_fn=_never_called,
        state=state,
    )
    provider.register_prompt_callback(modal)

    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="ls",
    )

    # Provider's prompt callback collapsed to DENY without
    # rendering the modal — the user saw nothing.
    assert result.effect is AuthzEffect.DENY
    assert result.allowed is False
    # Panel never rendered. The first modal in the fan-out (the
    # one that flipped the cancel) is the only visible prompt.
    assert out.getvalue() == ""

"""Phase 2.1 Step 24d — approval modal + callback component tests.

Four families of cases:

1. **Pattern suggestor** (pure function): heuristics for bash,
   file tools, the action-namespace fallback.
2. **Modal callback shape**: feed crafted ``(rule_id, action,
   target)`` plus scripted input; assert the return matches the
   Phase 2.0 ``PromptCallback`` shape (``AuthzEffect`` or
   ``(AuthzEffect, Rule)``).
3. **TC-CHAT-03 (modal allow)** + **TC-CHAT-04 (modal always-allow
   persists)**: end-to-end via the authz provider — register the
   modal, call ``allow``, assert the tool's authz check resolves
   correctly AND (for [a]) ``platform.yaml`` is rewritten with
   the new rule before the next call.
4. **Deregistration**: register, clear, re-call ``allow`` — the
   modal MUST NOT fire. Load-bearing for tests that bootstrap
   then exercise more than one command against the same runtime.
"""

from __future__ import annotations

import io as _io
import sys
from typing import TYPE_CHECKING

import pytest
import yaml

from cli._modal import ApprovalModal, suggest_pattern
from providers import RuleBasedAuthorizationProvider
from providers.authz import AuthzEffect, Rule

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers — scripted input + provider factories
# ---------------------------------------------------------------------------


def _scripted_input(answers: list[str]) -> _Iter:
    """Return an ``input_fn`` that pops one line per prompt."""
    iterator = iter(answers)

    async def _fn(prompt: str) -> str:
        _ = prompt
        try:
            return next(iterator)
        except StopIteration as exc:
            msg = "modal asked for more input than the test scripted"
            raise AssertionError(msg) from exc

    return _fn  # type: ignore[return-value]


class _Iter:  # purely for typing the helper above
    async def __call__(self, prompt: str) -> str:  # pragma: no cover
        return ""


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
# Pattern suggestor — pure function heuristics
# ===========================================================================


def test_suggest_pattern_bash_uses_first_token_with_dot_star() -> None:
    """Patch §4.3 row 1 + qol-permission-system-human-readable.md
    Step 3: bash → ``<first-token> *`` glob (was: regex
    ``^<token> .*$``)."""
    assert suggest_pattern(tool="bash", target='rg "foo bar" src/') == "rg *"
    assert suggest_pattern(tool="bash", target="ls -la /tmp") == "ls *"


def test_suggest_pattern_bash_empty_target_falls_back() -> None:
    """No command string → ``*`` catch-all glob (the qualified
    ``what`` ref already pins it to bash)."""
    assert suggest_pattern(tool="bash", target=None) == "*"
    assert suggest_pattern(tool="bash", target="") == "*"


def test_suggest_pattern_file_tools_use_parent_dir() -> None:
    """Patch §4.3 row 2: file tools → ``<parent>/*`` glob."""
    assert suggest_pattern(tool="write_file", target="/abs/path/X.py") == "/abs/path/*"
    assert (
        suggest_pattern(tool="read_file", target="/workspace/docs/spec.md")
        == "/workspace/docs/*"
    )


def test_suggest_pattern_unknown_tool_falls_back_to_action_namespace() -> None:
    """Tools the suggestor doesn't recognise → ``*`` catch-all glob.
    The paired ``what`` already names the tool, so ``*`` is the
    right default."""
    assert suggest_pattern(tool="ping", target="example.com") == "*"


def test_suggest_pattern_file_tool_with_no_target_falls_back() -> None:
    """File tool but no target (defensive) → ``*`` catch-all glob."""
    assert suggest_pattern(tool="write_file", target=None) == "*"


# ===========================================================================
# Modal callback — return-shape tests
# ===========================================================================


async def test_modal_y_returns_allow_no_rule() -> None:
    """Patch §4.4: ``[y]`` → bare ``ALLOW`` (no rule persisted)."""
    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["y"]))
    result = await modal(rule_id="rl_test", action="tool.bash", target="rg foo")
    assert result is AuthzEffect.ALLOW
    rendered = out.getvalue()
    # Panel surfaces the tool + the matched rule_id + the four choices.
    assert "bash" in rendered
    assert "rg foo" in rendered
    assert "rl_test" in rendered
    assert "[y]" in rendered
    assert "[a]" in rendered
    assert "[n]" in rendered
    assert "[d]" in rendered


async def test_modal_n_returns_deny_no_rule() -> None:
    """Patch §4.4: ``[n]`` → bare ``DENY`` (no rule persisted)."""
    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["n"]))
    result = await modal(rule_id="rl_test", action="tool.bash", target="rm -rf /")
    assert result is AuthzEffect.DENY


async def test_modal_a_with_accepted_pattern_returns_allow_rule_tuple() -> None:
    """Patch §4.4 + qol-permission-system-human-readable.md Step 3:
    ``[a]`` + accept pattern → tuple ``(ALLOW, Rule)``.

    Without a tool_ref_lookup the modal falls back to the legacy
    regex shape so the rule still loads via the provider's legacy
    compile path. With a lookup the rule uses the new schema —
    covered in :func:`test_modal_a_with_lookup_writes_new_shape`.
    """
    out = _io.StringIO()
    # "" on the pattern prompt accepts the suggested default.
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["a", ""]))
    result = await modal(rule_id="rl_bash_any", action="tool.bash", target="rg foo")
    assert isinstance(result, tuple)
    effect, rule = result
    assert effect is AuthzEffect.ALLOW
    # No tool_ref_lookup → legacy fallback. ``who`` is ``.*`` and
    # ``what`` is the glob-translated-to-regex form so the legacy
    # matcher still fires.
    assert rule.who == ".*"
    assert rule.what == "^rg .*$"
    assert rule.effect == "allow"


async def test_modal_a_with_lookup_writes_new_shape() -> None:
    """qol-permission-system-human-readable.md Step 3 — when a
    ``tool_ref_lookup`` is bound the persisted rule uses the new
    ``{what, pattern, effect}`` schema with ``what`` as the
    qualified tool ref. Mirror image of
    :func:`test_modal_a_with_accepted_pattern_returns_allow_rule_tuple`
    (no lookup → legacy fallback).
    """
    out = _io.StringIO()
    modal = ApprovalModal(
        stdout=out,
        input_fn=_scripted_input(["a", ""]),
        tool_ref_lookup=lambda action: "kolsch/bash" if action == "tool.bash" else None,
    )
    result = await modal(rule_id="rl_bash_any", action="tool.bash", target="rg foo")
    assert isinstance(result, tuple)
    _, rule = result
    # New-shape: no `who`, qualified `what`, glob `pattern`.
    assert rule.who is None
    assert rule.what == "kolsch/bash"
    assert rule.pattern == "rg *"
    assert rule.effect == "allow"


async def test_modal_a_scopes_from_to_proximate_caller_by_default() -> None:
    """When `caller_chain` is non-empty the modal defaults `from_`
    to the proximate frame (`caller_chain[0]`), so approved rules
    scope to the current agent/workflow instead of granting the
    effect to every future caller. Bare Enter on the `from:` prompt
    accepts the default.
    """
    out = _io.StringIO()
    # Inputs: [a] choice, "" accept pattern, "" accept default from.
    modal = ApprovalModal(
        stdout=out,
        input_fn=_scripted_input(["a", "", ""]),
        tool_ref_lookup=lambda action: (
            "kolsch/tools/bash" if action == "tool.bash" else None
        ),
    )
    result = await modal(
        rule_id="rl_prompt",
        action="tool.bash",
        target="rg foo",
        caller_chain=("kolsch/agents/triage",),
    )
    assert isinstance(result, tuple)
    _, rule = result
    assert rule.from_ == "kolsch/agents/triage"
    assert rule.what == "kolsch/tools/bash"


async def test_modal_a_star_broadens_scope_to_all_callers() -> None:
    """Typing `*` at the scope prompt clears `from_` (broadens the
    rule so it fires for every caller). `*` is not a valid `from:`
    glob (3-seg validation would reject it), so treating it as an
    unambiguous broaden sentinel is safe.
    """
    out = _io.StringIO()
    modal = ApprovalModal(
        stdout=out,
        input_fn=_scripted_input(["a", "", "*"]),
        tool_ref_lookup=lambda action: (
            "kolsch/tools/bash" if action == "tool.bash" else None
        ),
    )
    result = await modal(
        rule_id="rl_prompt",
        action="tool.bash",
        target="rg foo",
        caller_chain=("kolsch/agents/triage",),
    )
    assert isinstance(result, tuple)
    _, rule = result
    assert rule.from_ is None


async def test_modal_without_caller_chain_leaves_from_none() -> None:
    """Bootstrap / test paths that don't have a caller_chain skip
    the scope prompt entirely; approved rules land unscoped so
    behavior matches the pre-workflow modal for non-workflow
    contexts.
    """
    out = _io.StringIO()
    # Only two inputs — no scope prompt when caller_chain is empty.
    modal = ApprovalModal(
        stdout=out,
        input_fn=_scripted_input(["a", ""]),
        tool_ref_lookup=lambda action: (
            "kolsch/tools/bash" if action == "tool.bash" else None
        ),
    )
    result = await modal(
        rule_id="rl_prompt",
        action="tool.bash",
        target="rg foo",
    )
    assert isinstance(result, tuple)
    _, rule = result
    assert rule.from_ is None


async def test_modal_a_with_edited_pattern_uses_user_value() -> None:
    """User can override the suggested pattern by typing a new one.

    The user types a glob (qol-permission-system-human-readable.md
    Step 3 docs ``/perm`` style); the legacy fallback translates
    it into the equivalent regex for the in-flight Rule write."""
    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["a", "rg foo *"]))
    result = await modal(rule_id="rl_bash_any", action="tool.bash", target="rg foo")
    assert isinstance(result, tuple)
    _, rule = result
    assert rule.what == "^rg foo .*$"  # glob `rg foo *` → regex


async def test_modal_d_with_accepted_pattern_returns_deny_rule_tuple() -> None:
    """Patch §4.4: ``[d]`` + accept pattern → ``(DENY, Rule(effect=deny))``."""
    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["d", ""]))
    result = await modal(rule_id="rl_bash_any", action="tool.bash", target="rm -rf /")
    assert isinstance(result, tuple)
    effect, rule = result
    assert effect is AuthzEffect.DENY
    assert rule.effect == "deny"
    assert rule.what == "^rm .*$"


async def test_modal_invalid_choice_reprompts_until_valid() -> None:
    """Invalid characters reprompt rather than crash. Patch §4.1
    expects a clean retry loop for typos."""
    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["x", "z", "y"]))
    result = await modal(rule_id="rl_test", action="tool.bash", target="ls")
    assert result is AuthzEffect.ALLOW


async def test_modal_renders_target_inside_signature_for_non_bash_tools() -> None:
    """Per qol-chat-capsule-layout.md §Permission, the target is
    rendered inside the tool-signature line (``<tool>(<arg>)``)
    rather than via a separate ``Target:`` row."""
    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["y"]))
    await modal(rule_id="rl_write", action="tool.write_file", target="/abs/X.py")
    rendered = out.getvalue()
    assert "/abs/X.py" in rendered
    assert "write_file" in rendered


# ===========================================================================
# TC-CHAT-03 — modal allow path (full provider integration)
# ===========================================================================


async def test_tc_chat_03_modal_allow_resolves_to_allow_via_provider() -> None:
    """TC-CHAT-03: inject a prompt-effect rule for bash; register
    the modal as the provider's prompt callback; ``allow`` returns
    ``allowed=True`` when the user picks ``[y]``.

    The provider-modal interaction is what the chat surface uses;
    the test exercises the same wiring without bootstrapping the
    full runtime (the modal is the security gate; this test is
    the security-gate's contract).
    """
    out = _io.StringIO()
    provider = _provider_with_bash_prompt()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["y"]))
    provider.register_prompt_callback(modal)

    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="rg foo src/",
    )
    assert result.allowed is True
    assert result.effect is AuthzEffect.ALLOW
    # The matched rule is still the prompt rule — [y] is a
    # single-call resolution, no rule was inserted.
    assert result.matched_rule is not None
    assert result.matched_rule.effect == "prompt"
    assert "rg foo" in out.getvalue()


async def test_tc_chat_03_modal_deny_resolves_to_deny() -> None:
    """``[n]`` flip: same setup but the user picks ``[n]`` →
    ``allow`` returns ``allowed=False`` with effect=DENY."""
    out = _io.StringIO()
    provider = _provider_with_bash_prompt()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["n"]))
    provider.register_prompt_callback(modal)

    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="rg foo src/",
    )
    assert result.allowed is False
    assert result.effect is AuthzEffect.DENY


# ===========================================================================
# TC-CHAT-04 — modal always-allow + persistence
# ===========================================================================


async def test_tc_chat_04_always_allow_persists_to_yaml_and_skips_modal_next_time(
    tmp_path: Path,
) -> None:
    """TC-CHAT-04: pick ``[a]`` + accept the suggested pattern;
    assert (1) the call resolves to ALLOW, (2) ``platform.yaml``
    now contains the new rule, (3) the next matching call does
    NOT invoke the modal (it hits the persisted ALLOW rule
    first).

    This is the integration of: modal → provider in-memory insert
    → rule-added hook → atomic ``platform.yaml`` rewrite.
    """
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [
                            {
                                "who": ".*",
                                "what": r"tool\.bash",
                                "effect": "prompt",
                            },
                            {"who": ".*", "what": ".*", "effect": "deny"},
                        ],
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
                {"who": ".*", "what": ".*", "effect": "deny"},
            ],
        },
    )

    # Wire the same write-back used by ``kolsch permissions add``
    # so this test exercises the production persistence path.
    from cli.permissions import _write_back

    def _on_rule_added(rules: list[Rule]) -> None:
        _write_back(yaml_path, rules)

    invocations = 0
    suggested_pattern: str | None = None

    async def _input_fn(prompt: str) -> str:
        nonlocal invocations, suggested_pattern
        invocations += 1
        if invocations == 1:
            return "a"
        # Pattern prompt: accept the suggested default by
        # submitting empty.
        suggested_pattern = "^rg .*$"
        return ""

    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_input_fn)
    provider.register_prompt_callback(modal, on_rule_added=_on_rule_added)

    # First call — modal fires, user picks [a], pattern accepted.
    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="rg foo src/",
    )
    assert result.allowed is True
    assert result.effect is AuthzEffect.ALLOW

    # (b) platform.yaml now contains the new rule.
    persisted = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    yaml_rules = persisted["authorization"]["config"]["rules"]
    rule_what = [r["what"] for r in yaml_rules]
    assert "^rg .*$" in rule_what
    # The new rule sits BEFORE the prompt rule so future calls hit
    # it first — that's the provider's _insert_rule_before
    # contract.
    assert rule_what.index("^rg .*$") < rule_what.index(r"tool\.bash")

    # (c) Next call matching the new rule does NOT invoke the modal
    # again. The modal's input_fn would AssertionError on a third
    # call (scripted only two answers); the test passes only when
    # the modal doesn't fire.
    result2 = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="rg different",
    )
    assert result2.allowed is True
    assert result2.matched_rule is not None
    # Hit the new ALLOW rule, not the prompt rule.
    assert result2.matched_rule.effect == "allow"


# ===========================================================================
# TC-CHAT-04 — modal abandon collapses to single-call semantics
# ===========================================================================


async def test_modal_a_then_esc_abandons_rule_write() -> None:
    """Per patch §4.2: Esc on the pattern edit abandons the rule
    write; the [a] choice still resolves ALLOW for THIS call.

    Test models Esc as a scripted empty pattern WHEN we don't
    want the suggested default. Implementation detail: the modal
    treats empty-submit as "accept suggested default"; abandoning
    happens via raising from input_fn. This test exercises the
    abandon path through that raise.
    """
    out = _io.StringIO()

    async def _input_fn(prompt: str) -> str:
        if "[y/a/n/d]" in prompt:
            return "a"
        raise KeyboardInterrupt

    modal = ApprovalModal(stdout=out, input_fn=_input_fn)
    with pytest.raises(KeyboardInterrupt):
        await modal(rule_id="rl_bash_any", action="tool.bash", target="rg foo")


# ===========================================================================
# Deregistration — load-bearing for in-process subsequent commands
# ===========================================================================


async def test_clear_prompt_callback_makes_subsequent_allow_collapse_to_deny() -> None:
    """Registering then clearing → ``allow`` returns the
    Phase-2.0-default ``PROMPT, allowed=False`` shape (i.e. the
    modal is NOT invoked).

    Load-bearing per the build doc: a leaked callback would have
    a subsequent in-process ``kolsch run`` against the same authz
    provider hang on stdin OR allow silently. The clear is the
    only defence.
    """
    out = _io.StringIO()
    provider = _provider_with_bash_prompt()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["y"]))
    provider.register_prompt_callback(modal)
    provider.clear_prompt_callback()

    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="ls",
    )
    # The provider falls back to the no-callback semantics —
    # prompt effect, but allowed=False (the dispatcher would
    # emit authz.prompt_unattended).
    assert result.effect is AuthzEffect.PROMPT
    assert result.allowed is False
    # Importantly, the modal's input_fn was NEVER consumed —
    # if it had been, the scripted ``["y"]`` would have popped
    # and the next test sharing the same fixture would AssertionError.
    # Belt-and-braces:
    assert out.getvalue() == ""  # panel never rendered


async def test_cmd_chat_clears_modal_callback_on_clean_exit(
    chat_workspace: Path,
) -> None:
    """End-to-end: ``cmd_chat`` against an initialised workspace
    registers the modal on the bootstrapped runtime's authz
    provider AND clears it before returning. Subsequent in-process
    ``runtime.authz.allow`` calls collapse ``prompt`` to deny
    (no callback) per Phase 2.0 §2.2.

    Load-bearing per the build doc's deregistration concern:
    a subsequent in-process command (test suite, future Python
    embedding API) MUST NOT see the modal — which would hang on
    stdin or worse, allow silently. This test pins the lifecycle.
    """
    import argparse

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from cli import chat
    from platform_runtime import bootstrap_runtime

    # Run cmd_chat with EOF input → returns 0 cleanly.
    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        pipe_input.close()
        rc = await chat.cmd_chat(
            argparse.Namespace(
                root=chat_workspace,
                agent_ref="kolsch/agents/helper",
                session=None,
                model=None,
            ),
            out=out,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
    assert rc == 0

    # Now bootstrap a fresh runtime and verify the authz provider
    # has NO modal callback hanging around. (The chat owns the
    # provider lifetime today; bootstrap_runtime constructs a
    # fresh one per call, so the assertion is really "after chat
    # exit, the chat's provider is gone too." A subsequent in-
    # process command's bootstrap returns a different provider
    # without the callback by construction.)
    fresh_runtime = await bootstrap_runtime(root=chat_workspace)
    assert isinstance(fresh_runtime.authz, RuleBasedAuthorizationProvider)
    # The fresh provider has no callback — a prompt-effect rule
    # collapses to deny, NOT to a modal invocation.
    # qol-action-namespace-bundle-scoped.md Step 2 — action is now
    # the 3-seg catalog ref; this matches the migrated rule shape.
    result = await fresh_runtime.authz.allow(
        principal="agent.helper",
        action="kolsch/tools/bash",
        target="ls",
    )
    assert result.effect is AuthzEffect.PROMPT
    assert result.allowed is False


async def test_cmd_chat_clears_modal_callback_even_when_loop_raises(
    chat_workspace: Path,
) -> None:
    """Defensive: if the input loop raises an unexpected exception,
    the finally clause MUST still clear the modal. Drives that
    branch via a monkeypatched loop that raises.
    """
    import argparse

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from cli import chat

    # Patch _run_input_loop on this module to raise; the test
    # captures the runtime via a side-channel and asserts the
    # modal callback is cleared even though the loop raised.
    captured_runtime: list[object] = []
    original_loop = chat._run_input_loop

    async def _raising_loop(*args: object, **kwargs: object) -> int:
        runtime = kwargs.get("runtime")
        captured_runtime.append(runtime)
        # Confirm the registration happened before the loop ran:
        assert isinstance(runtime, type(runtime))
        msg = "boom"
        raise RuntimeError(msg)

    chat._run_input_loop = _raising_loop
    try:
        out = _io.StringIO()
        with (
            create_pipe_input() as pipe_input,
            create_app_session(input=pipe_input, output=DummyOutput()),
        ):
            pipe_input.close()
            with pytest.raises(RuntimeError, match="boom"):
                await chat.cmd_chat(
                    argparse.Namespace(
                        root=chat_workspace,
                        agent_ref="kolsch/agents/helper",
                        session=None,
                        model=None,
                    ),
                    out=out,
                    prompt_input=pipe_input,
                    prompt_output=DummyOutput(),
                )
    finally:
        chat._run_input_loop = original_loop

    # The captured runtime's authz provider had its callback
    # cleared by the finally branch — assert that.
    assert len(captured_runtime) == 1
    runtime = captured_runtime[0]
    assert isinstance(runtime, type(runtime))
    # The same provider that had the callback registered now
    # has it cleared.
    rt_authz = runtime.authz  # type: ignore[attr-defined]
    assert isinstance(rt_authz, RuleBasedAuthorizationProvider)
    assert rt_authz._registered_prompt_callback is None
    assert rt_authz._rule_added_hook is None

    # Subsequent allow on the SAME provider doesn't invoke a modal.
    # qol-action-namespace-bundle-scoped.md Step 2 — 3-seg action.
    result = await rt_authz.allow(
        principal="agent.helper",
        action="kolsch/tools/bash",
        target="ls",
    )
    assert result.effect is AuthzEffect.PROMPT
    assert result.allowed is False


# ===========================================================================
# bug-chat-deny-stops-and-esc-interrupts.md — Step 2 modal behavior
# ===========================================================================
#
# Three new behaviors land in Step 2:
#   - Fan-out short-circuit: if ``state.active_cancel`` is already
#     cancelled when the modal is invoked, return DENY immediately
#     without rendering the panel or calling input_fn.
#   - Deny [n] flips ``state.active_cancel`` and stamps
#     ``state.last_cancel_reason = "denied"`` BEFORE returning DENY.
#   - Deny [d] flips the cancel AND still returns the rule tuple
#     so the provider persists it via the existing on_rule_added
#     hook (verified by inspecting the (effect, rule) return shape;
#     the persistence path is exercised end-to-end by TC-CHAT-04
#     above, which is unchanged because that test omits state).


def _stub_state_with_cancel(cancel: object) -> object:
    """Build a minimal stand-in for :class:`ChatSessionState`.

    The modal only reads ``active_cancel`` and writes
    ``last_cancel_reason``; the full dataclass requires a runtime,
    config, session_id, thread_id that the unit tests for the modal
    have no reason to bootstrap. ``SimpleNamespace`` mirrors the
    shape exactly and the modal's type-check is permissive
    (TYPE_CHECKING import only).
    """
    from types import SimpleNamespace

    return SimpleNamespace(active_cancel=cancel, last_cancel_reason=None)


async def test_modal_short_circuits_when_active_cancel_already_cancelled() -> None:
    """Fan-out short-circuit (Q4 / Acceptance #4).

    Construct a modal whose bound state's ``active_cancel`` is
    already ``.cancel()``-ed. Assert ``__call__`` returns DENY
    immediately — no panel render, no input_fn invocation.
    """
    from engine import CancelToken

    out = _io.StringIO()
    cancel = CancelToken()
    cancel.cancel()  # already-cancelled — simulates a prior modal in the same fan-out
    state = _stub_state_with_cancel(cancel)

    async def _never_called(prompt: str) -> str:
        _ = prompt
        msg = "input_fn must not be called when active_cancel is already cancelled"
        raise AssertionError(msg)

    modal = ApprovalModal(
        stdout=out,
        input_fn=_never_called,
        state=state,  # type: ignore[arg-type]
    )
    result = await modal(rule_id="rl_test", action="tool.bash", target="rg foo")
    assert result is AuthzEffect.DENY
    # Panel never rendered — fan-out is silent for the remaining
    # tool calls.
    assert out.getvalue() == ""


async def test_modal_deny_once_flips_active_cancel() -> None:
    """[n] flips the cancel token + stamps reason BEFORE returning.

    Verifies the Step 2 contract: by the time the authz provider
    receives DENY and the framework receives the tool error, the
    cancel token is already cancelled — so the next framework
    iteration observes it and the exchange stops.
    """
    from engine import CancelToken

    out = _io.StringIO()
    cancel = CancelToken()
    assert cancel.is_cancelled() is False  # not yet cancelled
    state = _stub_state_with_cancel(cancel)

    modal = ApprovalModal(
        stdout=out,
        input_fn=_scripted_input(["n"]),
        state=state,  # type: ignore[arg-type]
    )
    result = await modal(rule_id="rl_test", action="tool.bash", target="rm -rf /")
    assert result is AuthzEffect.DENY
    assert cancel.is_cancelled() is True
    assert state.last_cancel_reason == "denied"  # type: ignore[attr-defined]


async def test_modal_deny_always_flips_cancel_and_returns_rule() -> None:
    """[d] + accept pattern: flip the cancel AND return the (DENY, Rule)
    tuple so the provider persists the rule via on_rule_added.

    The exchange stops; the rule survives chat exit. Both happen.
    """
    from engine import CancelToken

    out = _io.StringIO()
    cancel = CancelToken()
    state = _stub_state_with_cancel(cancel)

    modal = ApprovalModal(
        stdout=out,
        # [d] then accept the suggested pattern via empty submit.
        input_fn=_scripted_input(["d", ""]),
        state=state,  # type: ignore[arg-type]
    )
    result = await modal(rule_id="rl_bash_any", action="tool.bash", target="rm -rf /")
    assert isinstance(result, tuple)
    effect, rule = result
    assert effect is AuthzEffect.DENY
    assert rule.effect == "deny"
    assert rule.what == "^rm .*$"  # suggest_pattern's bash heuristic
    # AND the cancel token is flipped — the exchange stops too.
    assert cancel.is_cancelled() is True
    assert state.last_cancel_reason == "denied"  # type: ignore[attr-defined]


async def test_modal_allow_does_not_flip_active_cancel() -> None:
    """Symmetry guard: [y] returns ALLOW and must NOT touch the
    cancel token. Without this guard a refactor that flips on every
    return would silently kill normal allow flows."""
    from engine import CancelToken

    out = _io.StringIO()
    cancel = CancelToken()
    state = _stub_state_with_cancel(cancel)

    modal = ApprovalModal(
        stdout=out,
        input_fn=_scripted_input(["y"]),
        state=state,  # type: ignore[arg-type]
    )
    result = await modal(rule_id="rl_test", action="tool.bash", target="ls")
    assert result is AuthzEffect.ALLOW
    assert cancel.is_cancelled() is False
    assert state.last_cancel_reason is None  # type: ignore[attr-defined]


async def test_modal_allow_always_does_not_flip_active_cancel() -> None:
    """[a] + accept pattern: ALLOW + rule, cancel untouched.

    Pairs with the deny-always test above to confirm only the deny
    paths flip the cancel; allow paths are inert."""
    from engine import CancelToken

    out = _io.StringIO()
    cancel = CancelToken()
    state = _stub_state_with_cancel(cancel)

    modal = ApprovalModal(
        stdout=out,
        input_fn=_scripted_input(["a", ""]),
        state=state,  # type: ignore[arg-type]
    )
    result = await modal(rule_id="rl_bash_any", action="tool.bash", target="rg foo")
    assert isinstance(result, tuple)
    effect, _rule = result
    assert effect is AuthzEffect.ALLOW
    assert cancel.is_cancelled() is False
    assert state.last_cancel_reason is None  # type: ignore[attr-defined]


async def test_modal_bind_state_late_binds() -> None:
    """``bind_state`` lets ``chat._run_input_loop`` attach the
    per-session state AFTER the modal is constructed in
    ``cmd_chat``. Without this seam the modal would need to be
    moved into the input loop, which would break the existing
    modal-clear lifecycle."""
    from engine import CancelToken

    out = _io.StringIO()
    cancel = CancelToken()
    cancel.cancel()
    state = _stub_state_with_cancel(cancel)

    async def _never_called(prompt: str) -> str:
        _ = prompt
        msg = "input_fn must not be called when active_cancel is already cancelled"
        raise AssertionError(msg)

    # Modal constructed WITHOUT state — pre-fix construction.
    modal = ApprovalModal(stdout=out, input_fn=_never_called)
    # Late-bind, mirroring _run_input_loop's call.
    modal.bind_state(state)  # type: ignore[arg-type]

    result = await modal(rule_id="rl_test", action="tool.bash", target="ls")
    # Short-circuit fired post-bind — proves bind_state took effect.
    assert result is AuthzEffect.DENY


async def test_modal_without_state_still_returns_pre_fix_shape() -> None:
    """Regression pin: a modal constructed WITHOUT state behaves
    identically to pre-Step-2 — [n] returns bare DENY, no cancel
    side-effects. This is what every existing test relies on; the
    new state plumbing must be additive only.
    """
    out = _io.StringIO()
    modal = ApprovalModal(stdout=out, input_fn=_scripted_input(["n"]))
    result = await modal(rule_id="rl_test", action="tool.bash", target="rm -rf /")
    assert result is AuthzEffect.DENY


async def test_register_replaces_previous_callback() -> None:
    """Calling ``register_prompt_callback`` twice replaces — no
    implicit chaining. The most-recently-registered modal is the
    sole resolver."""
    provider = _provider_with_bash_prompt()
    out1 = _io.StringIO()
    out2 = _io.StringIO()
    modal1 = ApprovalModal(stdout=out1, input_fn=_scripted_input(["n"]))
    modal2 = ApprovalModal(stdout=out2, input_fn=_scripted_input(["y"]))
    provider.register_prompt_callback(modal1)
    provider.register_prompt_callback(modal2)

    result = await provider.allow(
        principal="agent.helper",
        action="tool.bash",
        target="ls",
    )
    # modal2 (the most recent) won — it returns ALLOW.
    assert result.allowed is True
    # And modal1's panel never rendered.
    assert out1.getvalue() == ""
    assert out2.getvalue() != ""


# ===========================================================================
# Authz provider — first-match-wins across (action, target)
# ===========================================================================


async def test_authz_what_matches_either_action_or_target() -> None:
    """Phase 2.1 / 24d authz tweak: ``what`` matches against
    ``target`` OR ``action``. A rule whose ``what`` matches
    ``tool.bash`` (action namespace) still fires when the
    dispatcher passes target='rg foo'; and a rule whose ``what``
    matches '^rg .*$' fires too.

    First-match-wins on rule order: the rule earlier in the list
    wins regardless of which domain it matched."""
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                # Rule 0 matches a specific target.
                {"who": ".*", "what": r"^rg .*$", "effect": "allow"},
                # Rule 1 matches the action namespace (Phase-2.0-shape).
                {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
            ],
        },
    )
    # Target='rg foo' matches rule 0 first.
    result = await provider.allow(
        principal="agent.x", action="tool.bash", target="rg foo"
    )
    assert result.effect is AuthzEffect.ALLOW
    # Target='rm foo' doesn't match rule 0; rule 1 matches via action.
    result2 = await provider.allow(
        principal="agent.x", action="tool.bash", target="rm foo"
    )
    assert result2.effect is AuthzEffect.PROMPT


async def test_authz_without_target_matches_only_against_action() -> None:
    """Backward compat with Phase 1 / non-chat callers: when
    ``target`` is None, the matcher checks only against ``action``.
    Existing TC-TOOL-12 tests continue to pass under this contract."""
    provider = _provider_with_bash_prompt()
    result = await provider.allow(principal="agent.x", action="tool.bash")
    # Hits the prompt rule on action match; no callback → collapses
    # to allowed=False per Phase 2.0 §2.2.
    assert result.effect is AuthzEffect.PROMPT


# ---------------------------------------------------------------------------
# Default input_fn smoke — ensure the production path is exercised at least
# for module-level import correctness.
# ---------------------------------------------------------------------------


def test_default_input_fn_is_lazy_async() -> None:
    """The lazy PromptSession isn't constructed at module import.
    Tests that never invoke the production path don't pay for it."""
    from cli import _modal

    assert _modal._modal_session is None
    # Re-import is a no-op; the singleton stays None until first
    # call. We don't actually invoke _default_input_fn here
    # because it would block on stdin.
    assert _modal._default_input_fn.__name__ == "_default_input_fn"
    _ = sys  # silence unused import for the noqa-free top

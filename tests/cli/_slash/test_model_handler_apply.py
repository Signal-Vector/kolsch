"""spec-132 Step 1 — baseline tests for `/model <ref>`'s argument-form path.

Before this spec, no test module exercised `_handler_model`'s
success-case argument branch or the `ChatSessionState.model_override`
/ `ChatSessionState.config` mutation it performs — confirmed by a
repo-wide search (`tests/component/test_models_command.py` covers the
unrelated `kolsch models` CLI subcommand; `tests/unit/cli/
test_run_model_override.py` covers a different function,
`apply_run_model_override`, used only by `kolsch run --model`).

This file establishes that baseline and doubles as the regression
floor for spec-132's extraction of the apply-model logic into the
shared `_apply_model` helper (Step 1) — `_handler_model`'s
argument-form branch must remain byte-identical in behavior.

Covers:
- a valid `provider/model` compact string
- a valid alias (`model_aliases`)
- an invalid ref (error path unchanged — friendly one-line message,
  `ControlSignal.CONTINUE`, no state mutation)
- a direct assertion that `ChatSessionState.model_override` /
  `ctx.state.config` reflect the new model after a successful call

spec-132 Steps 5/6 briefly changed the no-arg branch to open
`ModelPickerScreen` itself; a post-ship fix reverted that —
`_handler_model` synchronously invoked from `ChatApp._handle_slash`
can't safely open a picker (Textual's `push_screen_wait` needs worker
context, and awaiting that worker from the calling message handler
deadlocks — see `_handler_model`'s current docstring in `cli._slash`
for the full write-up). The no-arg branch is back to printing the
current model as plain text (byte-identical to the original,
pre-spec-132 behavior) — this is a FALLBACK for direct callers only;
in the real `kolsch chat` UI, `ChatApp._handle_slash` intercepts
`/model` with no argument before it ever reaches this function and
routes to `ChatApp._handle_model_picker` instead, covered by
`tests/cli/chat/test_model_picker_slash_dispatch.py`.
"""

from __future__ import annotations

import io
from typing import Any, cast
from uuid import uuid4

import pytest

from cli._slash import ChatSessionState, ControlSignal, SlashContext, _handler_model
from schemas import AgentId, IOSpec, ModelSpec, ResolvedAgentConfig, SessionId, ThreadId


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="openrouter", model="anthropic/claude-3"),
        prompt="You are an agent.",
        io=IOSpec(mode="conversational"),
    )


def _state(*, model_aliases: dict[str, str] | None = None) -> ChatSessionState:
    return ChatSessionState(
        runtime=cast("Any", None),  # unused by /model
        config=_config(),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        model_aliases=model_aliases or {},
    )


def _ctx(state: ChatSessionState) -> SlashContext:
    return SlashContext(stdout=io.StringIO(), state=state)


@pytest.mark.asyncio
async def test_model_arg_compact_string_applies_and_confirms() -> None:
    """`/model provider/model` applies the spec and prints the exact
    pre-existing confirmation line."""
    state = _state()
    ctx = _ctx(state)

    signal = await _handler_model("opencode/gpt-oss-120b", ctx)

    assert signal is ControlSignal.CONTINUE
    assert ctx.stdout.getvalue() == "model set to opencode/gpt-oss-120b\n"


@pytest.mark.asyncio
async def test_model_arg_compact_string_updates_state() -> None:
    """Direct assertion: `model_override` and `config.model` both
    reflect the new model after a successful call."""
    state = _state()
    ctx = _ctx(state)

    await _handler_model("opencode/gpt-oss-120b", ctx)

    expected = ModelSpec(provider="opencode", model="gpt-oss-120b")
    assert state.model_override == expected
    assert state.config.model == expected


@pytest.mark.asyncio
async def test_model_arg_alias_resolves_via_model_aliases() -> None:
    """`/model <alias>` resolves through `ChatSessionState.model_aliases`."""
    state = _state(model_aliases={"smart": "openrouter/big-model"})
    ctx = _ctx(state)

    signal = await _handler_model("smart", ctx)

    assert signal is ControlSignal.CONTINUE
    expected = ModelSpec(provider="openrouter", model="big-model")
    assert state.model_override == expected
    assert state.config.model == expected
    assert ctx.stdout.getvalue() == "model set to openrouter/big-model\n"


@pytest.mark.asyncio
async def test_model_arg_invalid_ref_prints_error_and_leaves_state_unchanged() -> None:
    """An unresolvable ref (not `provider/model`, not a known alias)
    prints the pre-existing friendly error, returns CONTINUE, and
    leaves session state untouched."""
    state = _state(model_aliases={})
    ctx = _ctx(state)

    signal = await _handler_model("not-a-known-alias", ctx)

    assert signal is ControlSignal.CONTINUE
    output = ctx.stdout.getvalue()
    assert output.startswith("/model: invalid model ref 'not-a-known-alias':")
    # No mutation on the error path.
    assert state.model_override is None
    assert state.config.model == ModelSpec(
        provider="openrouter", model="anthropic/claude-3"
    )


@pytest.mark.asyncio
async def test_model_arg_malformed_compact_string_prints_error() -> None:
    """A `provider/` or `/model`-shaped string with an empty segment
    hits the malformed-compact-model error path, unchanged."""
    state = _state()
    ctx = _ctx(state)

    signal = await _handler_model("provider-with-no-model/", ctx)

    assert signal is ControlSignal.CONTINUE
    output = ctx.stdout.getvalue()
    assert output.startswith("/model: invalid model ref 'provider-with-no-model/':")
    assert state.model_override is None


@pytest.mark.asyncio
async def test_model_no_arg_prints_current_model_as_fallback() -> None:
    """Bare `/model`, called directly (not via `ChatApp._handle_slash`),
    prints the current `provider/model` as text and mutates nothing —
    the safe, no-`App`-required fallback behaviour. In real `kolsch
    chat` usage this branch is unreachable (`ChatApp._handle_slash`
    intercepts first); see `tests/cli/chat/test_model_picker_slash_dispatch.py`
    for the real no-arg picker-opening coverage."""
    state = _state()
    ctx = _ctx(state)

    signal = await _handler_model("", ctx)

    assert signal is ControlSignal.CONTINUE
    assert ctx.stdout.getvalue() == "current model: openrouter/anthropic/claude-3\n"
    assert state.model_override is None

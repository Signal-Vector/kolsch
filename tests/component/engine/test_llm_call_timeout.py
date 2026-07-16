"""spec-069 Part A — bounded LLM/network read via ``asyncio.wait_for``.
bug-086 extends the resolver so the default is configurable via
``platform.yaml:workflows.llm_call_timeout_seconds`` (env still
overrides, hardcoded fallback bumped from 120s to 210s).

Covers Acceptance Criterion 4 of spec-069 and bug-086 AC1/AC3/AC4:

- **AC4 (spec-069)** — the blocking LLM call (CrewAI ``kickoff`` /
  provider read inside ``asyncio.to_thread``) is bounded by a
  timeout so a stalled provider read fails fast rather than
  pinning a worker indefinitely.
- **AC1 (bug-086)** — the bootstrap-installed default is honoured
  when the env var is unset; env still overrides.
- **AC3 (bug-086)** — with the new 210s hardcoded fallback, a
  ~150s call is NOT cancelled by the bare-install shape (the old
  120s constant would have killed it).
- **AC4 (bug-086)** — env override semantics preserved (blank/0/
  negative disables the wrap; malformed falls through to the
  configured default).

Two seams are wrapped in Part A — this file exercises the
``_crewai_adapter.make_llm_callable`` seam because it is the
narrower, sync-boundary path the engine calls per-turn. The other
seam (``crewai_framework.run_exchange``'s ``kickoff_async``) shares
the same wait_for wrap and the same
[`ModelTimeout`][errors.ModelTimeout] semantic error, so pinning
one seam covers both.

The test does NOT import the real ``crewai`` package (crewai is an
optional install, per ``engine/__init__.py``); a stand-in for
``crewai.LLM`` blocks on a ``threading.Event`` that the test
never sets, so the underlying ``asyncio.to_thread`` future never
resolves and the wait_for MUST fire to unwind the awaiting task.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest

from engine import _crewai_adapter
from engine._crewai_adapter import make_llm_callable
from engine.framework import LLMRequest, Message
from errors import ModelTimeout

if TYPE_CHECKING:
    from crewai import LLM


@pytest.fixture(autouse=True)
def _reset_configured_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guarantee test isolation for the module-level configured default.

    ``set_llm_call_timeout_default`` writes process-global state.
    If a bootstrap-integration test runs earlier in the session, the
    write persists and pollutes the assertions here. Reset to the
    sentinel via monkeypatch so each test sees the hardcoded
    fallback semantics until it explicitly installs a default.
    """
    monkeypatch.setattr(
        _crewai_adapter,
        "_configured_default_timeout",
        _crewai_adapter._UNSET_CONFIGURED_DEFAULT,
    )


class _WedgedCrewaiLLM:
    """Stand-in for ``crewai.LLM`` whose ``.call`` blocks until released.

    Blocks on a ``threading.Event`` the test can set via
    ``release()`` for teardown. Without a release, the underlying
    ``asyncio.to_thread`` worker (a non-daemon default-pool thread)
    would keep running after the test returns and block pytest
    interpreter shutdown — spec-069 Part B (owned executor) fixes
    that at the process level; until then the test is polite and
    releases the worker in a ``finally:`` arm.

    This models the spec-069 defect-2 shape: a provider read that
    never returns without operator intervention. The wait_for wrap
    MUST unwind the awaiting task inside the configured bound; the
    worker thread itself keeps running (Python cannot cancel
    arbitrary threads) but no longer blocks the caller.
    """

    model: str = "wedged-mock"

    def __init__(self) -> None:
        self._release_gate = threading.Event()
        self.entered = threading.Event()
        self.released = threading.Event()

    def call(
        self,
        messages: Any,
        tools: Any = None,
        **kwargs: Any,
    ) -> Any:
        _ = (messages, tools, kwargs)
        self.entered.set()
        # Wait until the test releases us (teardown) or forever
        # (which the wait_for wrap must unwind). If the wrap regressed,
        # this blocks forever and the outer wait_for kicks in.
        self._release_gate.wait()
        self.released.set()
        return "unreachable"

    def release(self) -> None:
        """Signal the worker thread to exit so pytest can tear down."""
        self._release_gate.set()


@pytest.mark.asyncio
async def test_wedged_llm_call_raises_model_timeout_within_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged ``crewai.LLM.call`` fails fast via ``asyncio.wait_for``.

    Sets ``KOLSCH_LLM_CALL_TIMEOUT_SECONDS=0.5`` so the test is fast;
    invokes ``make_llm_callable`` with a stub that blocks its worker
    thread forever; asserts the caller sees ``ModelTimeout`` (the
    semantic domain error the fallback ladder + observer render as
    ``model.timeout``) within a bounded wall time.

    Fail modes this test catches:

    - The wrap is missing → the ``await`` hangs indefinitely and
      the outer ``asyncio.wait_for(..., timeout=5.0)`` fires with
      a bare ``TimeoutError`` (NOT ``ModelTimeout``); the assertion
      about the exception type fails.
    - The wrap is present but on the wrong error → the caller sees
      a bare ``TimeoutError`` / ``asyncio.CancelledError`` and the
      engine's fallback ladder can't route it as a provider timeout.
    - The wrap fires too late → wall time exceeds the budget by
      more than a small margin (0.5s + 2s slack).
    """
    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "0.5")

    wedged = _WedgedCrewaiLLM()
    fn = make_llm_callable(cast("LLM", wedged))
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="wedged-mock",
        parameters={},
    )

    try:
        started = time.perf_counter()
        # A generous outer wait_for so the test suite never actually
        # hangs even if the inner wrap regresses; the wrap itself is
        # what we're checking, and it should fire well under this.
        with pytest.raises(ModelTimeout) as excinfo:
            await asyncio.wait_for(fn(request), timeout=5.0)
        elapsed = time.perf_counter() - started

        # AC4 — the timeout fires within its budget (0.5s + slack for
        # thread scheduling + wait_for granularity).
        assert elapsed < 3.0, (
            f"LLM wait_for wrap fired too late — elapsed {elapsed:.3f}s "
            "(expected sub-3s with a 0.5s bound)"
        )
        # The wrap must produce the SEMANTIC error, not a bare
        # TimeoutError, so the engine's fallback / observer treats
        # it uniformly with a provider-side ``model.timeout``.
        assert excinfo.value.detail["provider"] == "crewai"
        assert excinfo.value.detail["timeout_seconds"] == pytest.approx(0.5)
        assert excinfo.value.detail["model"] == "wedged-mock"

        # Sanity — the worker thread actually got scheduled (proves the
        # stub was invoked and we're timing out mid-call, not before).
        assert wedged.entered.is_set(), (
            "the wedged crewai.LLM.call was never entered — the test is "
            "not exercising the wait_for wrap"
        )
        # The worker thread is still alive (Python can't cancel it) —
        # spec-069 Part B (owned executor) makes it daemonic so it
        # can't block interpreter shutdown. Confirm it hasn't (falsely)
        # released as if the timeout somehow unblocked the blocking
        # call — that would indicate a bug in the wrap.
        assert not wedged.released.is_set(), (
            "the worker thread reported 'released' — the wait_for wrap "
            "should NOT have unblocked the underlying threading.Event"
        )
    finally:
        # Teardown — release the worker thread so it exits instead of
        # blocking pytest interpreter shutdown. Part B (owned executor)
        # makes this unnecessary at the process level (daemon threads
        # don't block Py_Finalize), but until Part B lands we're
        # polite and hand-release.
        wedged.release()


@pytest.mark.asyncio
async def test_env_opt_out_disables_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting ``KOLSCH_LLM_CALL_TIMEOUT_SECONDS`` to an empty string
    disables the wrap entirely — the caller sees the pre-spec-069
    unbounded behaviour. Guards tests that need the raw shape.
    """
    from engine._crewai_adapter import _read_llm_call_timeout_seconds

    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "")
    assert _read_llm_call_timeout_seconds() is None

    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "0")
    assert _read_llm_call_timeout_seconds() is None

    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "-1")
    assert _read_llm_call_timeout_seconds() is None

    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "not a float")
    # Malformed values fall back to the bootstrap-configured default
    # if any, else the hardcoded module-level fallback (defensive —
    # better a bounded call than an unbounded one when the operator
    # typo'd the env var). bug-086 bumped this fallback from 120s to
    # 210s (= opencode's tuned 180s + 30s margin) so a bare-install
    # no longer undercuts the provider timeout.
    assert _read_llm_call_timeout_seconds() == 210.0

    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "42.5")
    assert _read_llm_call_timeout_seconds() == pytest.approx(42.5)


@pytest.mark.asyncio
async def test_configured_default_used_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bug-086 AC1 — ``set_llm_call_timeout_default`` installs the fallback.

    With no env override, ``_read_llm_call_timeout_seconds`` returns
    the value bootstrap installed via ``set_llm_call_timeout_default``,
    not the hardcoded 210s. This is the path
    ``platform.yaml:workflows.llm_call_timeout_seconds`` travels.
    """
    from engine._crewai_adapter import (
        _read_llm_call_timeout_seconds,
        set_llm_call_timeout_default,
    )

    monkeypatch.delenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", raising=False)

    set_llm_call_timeout_default(60.0)
    assert _read_llm_call_timeout_seconds() == pytest.approx(60.0)

    # `None` = disable the wrap (explicit `null` in platform.yaml).
    set_llm_call_timeout_default(None)
    assert _read_llm_call_timeout_seconds() is None


@pytest.mark.asyncio
async def test_env_overrides_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bug-086 AC1 — env stays highest priority above the platform.yaml default.

    Operators can still pin a per-process value via the env var
    without editing the workspace's platform.yaml.
    """
    from engine._crewai_adapter import (
        _read_llm_call_timeout_seconds,
        set_llm_call_timeout_default,
    )

    set_llm_call_timeout_default(60.0)
    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "300")
    assert _read_llm_call_timeout_seconds() == pytest.approx(300.0)

    # Env disable (empty) beats the configured default too — the
    # env var's opt-out semantics are preserved on top of the new
    # configured-default layer.
    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "")
    assert _read_llm_call_timeout_seconds() is None


@pytest.mark.asyncio
async def test_malformed_env_falls_through_to_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bug-086 — malformed env falls through to the platform.yaml default.

    When the operator typos the env var, the resolver defends the
    caller with the configured default rather than degrading to the
    hardcoded fallback (which is the correct behaviour only when
    NO bootstrap default has been installed).
    """
    from engine._crewai_adapter import (
        _read_llm_call_timeout_seconds,
        set_llm_call_timeout_default,
    )

    set_llm_call_timeout_default(60.0)
    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "not a float")
    assert _read_llm_call_timeout_seconds() == pytest.approx(60.0)

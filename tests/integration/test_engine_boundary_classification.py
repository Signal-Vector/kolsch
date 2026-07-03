"""TC-ENG-CLASSIFY-* — bug-platform-internal-error-boundary-swallow.md 0.3 Step 3.

End-to-end tests for the IN-LOOP cause-chain classifier (0.3's
primary fix) and the outer boundary safety net (0.2's wiring).

Pre-fix history:

- 0.1 / pre-fix: non-PlatformError from the framework wrapped at
  the outer boundary as ``PlatformInternalError`` (FATAL). Retry
  never ran. ``runtime.max_retries`` was dead policy.
- 0.2 patch: added a classifier at the OUTER boundary
  (``engine.py:364-385``). Translation worked
  (``model.upstream_error`` instead of ``platform.internal_error``)
  BUT the boundary sits outside the retry loop, so retry STILL
  didn't run — single-attempt failure with the better surface but
  unchanged retry behavior. This is the regression the 0.3 spec
  flags: "0.2's TC-01 was satisfied by a single-attempt
  classify-and-emit path; that's how the bad placement got past CI."
- 0.3 fix: classifier also runs INSIDE the retry loop
  (``engine.py:1119-1167``). Non-PlatformError with a Recoverable
  cause-chain match → append to ``attempts_data`` + continue.
  Fatal match → re-raise classified (outer ``except PlatformError``
  catches as typed surface). No match → bare ``raise`` (outer
  boundary safety net handles).

Post-fix behaviour pinned here:

- TC-CLASSIFY-01 (Recoverable cause-chain → 3 attempts → exhausted).
  REGRESSION GUARD: asserts ``detail.attempts`` length == 3 AND
  framework was called 3 times. The 0.2 patch produced a
  single-attempt path; the attempts assertion is what would have
  caught it.
- TC-CLASSIFY-02 (Fatal cause-chain → 1 attempt → typed surface).
  An ``openai.AuthenticationError`` in the cause chain → the
  in-loop classifier sees Fatal → re-raises as ``AgentUnauthorized``;
  outer ``except PlatformError`` emits ``agent.unauthorized`` —
  NOT ``model.retry_exhausted`` (auth won't fix by retrying) and
  NOT ``platform.internal_error`` (the in-loop classifier typed
  it properly).
- TC-CLASSIFY-03 (real engine bug → ``platform.internal_error``).
  An ``AttributeError`` with no provider-typed cause chain → the
  in-loop classifier returns None → bare ``raise`` → outer boundary
  classifier also returns None → falls back to
  ``PlatformInternalError``. The classification table does NOT
  swallow real engine bugs.
- TC-CLASSIFY-04 (transient → eventual success). Framework raises
  on attempt 1, returns on attempt 2. ``agent.message`` lands;
  no ``thread.exchange_failed`` is emitted. Framework called
  exactly 2 times (regression guard against 0.2's single-attempt
  bug).
- TC-CLASSIFY-05 (adapter wrap in isolation). Defense-in-depth
  adapter translation in ``crewai_framework.CrewAIFramework`` —
  currently dead code in practice (CrewAI wraps before
  ``kickoff_async``'s outer except sees it) but kept against
  upstream changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from engine import MinimalFramework
from errors import ModelUpstreamError
from schemas import EventType
from tests.fixtures.engine_builder import build_engine, new_session_context
from tests.fixtures.fake_llm import FakeLLM, TextResponse

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Test fixture frameworks — minimal Framework implementations that
# emulate specific failure shapes.
# ---------------------------------------------------------------------------


def _synth_openai_typed_exc(name: str, msg: str) -> Exception:
    """Build an Exception whose ``type.__module__`` is ``"openai"`` and
    ``__name__`` matches ``name``. The classifier matches by qualified
    string (``f"{module}.{name}"``) so a synthesised class with the
    right module + name is indistinguishable from the real
    ``openai.<name>`` for its purposes — and keeps the integration
    test free of any ``openai`` SDK import."""
    cls = type(name, (Exception,), {})
    cls.__module__ = "openai"
    return cls(msg)


class _CauseChainRecoverableFramework:
    """Raises a non-PlatformError whose ``__cause__`` carries a typed
    openai.APITimeoutError shape on EVERY call.

    This is the post-0.3 retry-path test surface. The framework
    raises a non-PlatformError, so the retry loop's existing
    ``except PlatformError`` branch doesn't catch it — only the
    NEW in-loop classifier (the 0.3 fix) does. Pre-0.3 the
    exception would have escaped the loop entirely after a single
    attempt; post-0.3 the loop runs ``max_retries+1`` times.

    Tracks ``call_count`` so tests can assert the loop actually
    iterated (the regression guard against 0.2's single-attempt
    classify-at-boundary path).
    """

    def __init__(self) -> None:
        self.call_count = 0

    async def run_exchange(self, **_kwargs: Any) -> str:
        self.call_count += 1
        typed = _synth_openai_typed_exc(
            "APITimeoutError",
            "Request timed out.",
        )
        try:
            raise typed
        except Exception as cause:
            # Generic wrapper with `from cause` preserves the chain
            # — mirrors what CrewAI's adapter does
            # (raise ConnectionError(...) from APIConnectionError).
            raise RuntimeError("opaque framework wrapper") from cause


class _CauseChainFatalFramework:
    """Raises a non-PlatformError whose ``__cause__`` carries a typed
    openai.AuthenticationError shape on every call.

    Tests the in-loop classifier's FATAL branch: the classifier
    returns ``AgentUnauthorized`` (Fatal), the loop re-raises it,
    the outer ``except PlatformError`` emits the typed surface
    (``agent.unauthorized``) WITHOUT retrying — auth won't fix on
    attempt 2.

    Tracks ``call_count`` to prove the loop fires exactly once on
    Fatal classification.
    """

    def __init__(self) -> None:
        self.call_count = 0

    async def run_exchange(self, **_kwargs: Any) -> str:
        self.call_count += 1
        typed = _synth_openai_typed_exc(
            "AuthenticationError",
            "Invalid API key",
        )
        try:
            raise typed
        except Exception as cause:
            raise RuntimeError("opaque framework wrapper") from cause


class _TransientCauseChainFramework:
    """Raises a non-PlatformError with cause-chain Recoverable on the
    FIRST call; returns clean text on the second.

    Tests the happy-path payoff of 0.3: the in-loop classifier
    absorbs the transient, the second attempt succeeds, the user
    gets ``agent.message`` and no ``thread.exchange_failed``
    fires. ``call_count == 2`` is the regression guard against
    0.2's single-attempt path.
    """

    def __init__(self) -> None:
        self.call_count = 0

    async def run_exchange(self, **_kwargs: Any) -> str:
        self.call_count += 1
        if self.call_count == 1:
            typed = _synth_openai_typed_exc(
                "APITimeoutError",
                "transient timeout",
            )
            try:
                raise typed
            except Exception as cause:
                raise RuntimeError("opaque framework wrapper") from cause
        return "Recovered after retry."


# ---------------------------------------------------------------------------
# Helpers — config + event filtering.
# ---------------------------------------------------------------------------


async def _drive_one_exchange(
    tmp_path: Path,
    framework: object,
    *,
    max_retries: int = 2,
) -> list[Any]:
    """Build an engine on tmp_path, drive one exchange, return events."""
    fake_llm = FakeLLM(script=[TextResponse(text="unused")])
    engine, store, _am = await build_engine(
        tmp_path,
        fake_llm,
        framework=framework,  # type: ignore[arg-type] — protocol duck-types
    )
    ctx = await new_session_context(store)
    # Bump max_retries on the config Phase 2.2 envelope uses.
    from schemas import RuntimeSpec

    # _drive_one_exchange uses build_engine's stock _config(); patch
    # runtime.max_retries by passing a modified config to execute. The
    # helper here re-uses the engine's default-config builder, so we
    # need to call execute_stream with an explicit config. Inline that:
    from tests.component.test_engine import _config

    cfg = _config().model_copy(update={"runtime": RuntimeSpec(max_retries=max_retries)})
    events: list[Any] = []
    async for evt in engine.execute_stream(cfg, ctx):
        events.append(evt)
    return events


def _failed(events: list[Any]) -> Any | None:
    for evt in events:
        if evt.type == EventType.THREAD_EXCHANGE_FAILED:
            return evt
    return None


def _completed(events: list[Any]) -> Any | None:
    for evt in events:
        if evt.type == EventType.THREAD_EXCHANGE_COMPLETED:
            return evt
    return None


def _agent_message(events: list[Any]) -> Any | None:
    for evt in events:
        if evt.type == EventType.AGENT_MESSAGE:
            return evt
    return None


# ---------------------------------------------------------------------------
# TC-CLASSIFY-01 — adapter translation + retry + exhaust.
# ---------------------------------------------------------------------------


async def test_tc_eng_classify_01_cause_chain_recoverable_retries_three_times_then_exhausts(
    tmp_path: Path,
) -> None:
    """The trigger case end-to-end via the IN-LOOP classifier.

    Framework raises a NON-PlatformError on every call. The
    exception's ``__cause__`` carries a typed
    ``openai.APITimeoutError`` (synthesised — no SDK import). The
    retry loop's existing ``except PlatformError`` doesn't catch
    (the exception isn't a PlatformError), but the 0.3 in-loop
    classifier DOES — it walks the cause chain, finds the typed
    Recoverable, returns ``ModelUpstreamError``, appends to
    ``attempts_data``, continues.

    ``runtime.max_retries=2`` → 3 attempts; exhaustion raises
    ``ModelRetryExhausted`` with the per-attempt array populated.

    Pre-0.3 (0.2 patch): the classifier sat at the OUTER boundary.
    A non-PlatformError fell straight through the retry loop's
    handlers, escaped ``_run_framework``, and the outer boundary
    re-classified it as ``model.upstream_error`` — but emitted
    that as the final event, no retries. Single attempt.

    Post-0.3: 3 attempts, all model.upstream_error, then
    model.retry_exhausted.

    REGRESSION GUARD: the ``framework.call_count == 3`` assertion +
    the ``len(attempts) == 3`` assertion together pin the LOOP
    actually iterating. A single-attempt classify-and-emit path
    (the 0.2 bug) would produce ``call_count == 1`` and the failed
    event would carry ``model.upstream_error`` directly with no
    ``attempts`` field. This is the assertion 0.2's tests lacked.
    """
    framework = _CauseChainRecoverableFramework()
    events = await _drive_one_exchange(tmp_path, framework, max_retries=2)

    failed = _failed(events)
    assert failed is not None, "exchange should have failed after retry exhaustion"
    assert failed.data["error"]["type"] == "model.retry_exhausted", (
        f"expected model.retry_exhausted from in-loop retry; got "
        f"{failed.data['error']['type']!r} — likely the in-loop classifier "
        "isn't wired (0.2 placement bug)"
    )

    attempts = failed.data["error"]["detail"]["attempts"]
    assert len(attempts) == 3, (
        f"expected 3 attempts with max_retries=2, got {len(attempts)}"
    )
    for attempt in attempts:
        assert attempt["type"] == "model.upstream_error", (
            f"each retry attempt should be classified as model.upstream_error; "
            f"got {attempt['type']!r}"
        )

    assert framework.call_count == 3, (
        f"framework should have been called 3 times (max_retries+1); "
        f"got {framework.call_count} — the in-loop classifier isn't "
        "running. This is the load-bearing regression guard against "
        "0.2's single-attempt placement bug."
    )


# ---------------------------------------------------------------------------
# TC-CLASSIFY-02 — cause-chain typed exception re-classified.
# ---------------------------------------------------------------------------


async def test_tc_eng_classify_02_fatal_cause_chain_typed_surface_no_retry(
    tmp_path: Path,
) -> None:
    """A non-PlatformError with a known FATAL typed exception in
    ``__cause__`` (``openai.AuthenticationError``) MUST NOT retry —
    auth won't fix on attempt 2 — and MUST surface as the right
    typed PlatformError (``agent.unauthorized``), not
    ``platform.internal_error``.

    The in-loop classifier's Fatal branch: ``raise classified from
    exc``. The outer ``except PlatformError`` at ``run()`` picks
    up the typed exception and emits the failed event with the
    right error type. The boundary's cause-chain classifier (0.2
    wiring) is never consulted on this path because the outer
    except matches PlatformError first.

    REGRESSION GUARD: framework called exactly once. Retrying an
    auth failure would burn the user's credentials budget for no
    gain and surface a misleading ``model.retry_exhausted`` instead
    of ``agent.unauthorized``.
    """
    framework = _CauseChainFatalFramework()
    events = await _drive_one_exchange(tmp_path, framework, max_retries=2)

    failed = _failed(events)
    assert failed is not None
    assert failed.data["error"]["type"] == "agent.unauthorized", (
        f"Fatal cause-chain should surface the typed PlatformError; got "
        f"{failed.data['error']['type']!r}"
    )
    # Hard guard against the wrong retry behavior.
    assert failed.data["error"]["type"] != "model.retry_exhausted"
    assert framework.call_count == 1, (
        f"auth failures must NOT retry; framework called {framework.call_count} times"
    )


# ---------------------------------------------------------------------------
# TC-CLASSIFY-03 — real engine bug still platform.internal_error.
# ---------------------------------------------------------------------------


async def test_tc_eng_classify_03_real_engine_bug_still_internal_error(
    tmp_path: Path,
) -> None:
    """A genuine engine-side ``AttributeError`` with no provider-typed
    cause chain STILL surfaces as ``platform.internal_error``.

    Sim: patch the assembly procedure (called from _run_framework
    BEFORE the framework dispatch) to raise AttributeError. The
    cause-chain inspection finds no known typed exception, so the
    boundary falls back to PlatformInternalError — the right
    classification for real engine bugs.
    """
    fake_llm = FakeLLM(script=[TextResponse(text="unused")])
    engine, store, _am = await build_engine(
        tmp_path,
        fake_llm,
        framework=MinimalFramework(),
    )
    ctx = await new_session_context(store)

    # Patch assemble_context to raise AttributeError mid-_run_framework.
    # The boundary's classifier should NOT match (no provider type in
    # the cause chain) and fall back to PlatformInternalError.
    def _broken_assemble(*_args: Any, **_kwargs: Any) -> Any:
        raise AttributeError("'NoneType' object has no attribute 'foo'")

    from tests.component.test_engine import _config

    cfg = _config()
    with patch("engine.engine.assemble_context", side_effect=_broken_assemble):
        events = [evt async for evt in engine.execute_stream(cfg, ctx)]

    failed = _failed(events)
    assert failed is not None
    assert failed.data["error"]["type"] == "platform.internal_error", (
        "real engine bugs (no typed provider exception in cause chain) "
        "must still surface as platform.internal_error — the classifier "
        "MUST NOT swallow them"
    )
    # The original exception type is preserved in detail.
    assert "AttributeError" in failed.data["error"]["detail"]["original_type"]


# ---------------------------------------------------------------------------
# TC-CLASSIFY-04 — transient failure absorbed by retry.
# ---------------------------------------------------------------------------


async def test_tc_eng_classify_04_transient_cause_chain_recovers_on_attempt_two(
    tmp_path: Path,
) -> None:
    """The happy-path payoff for the whole bug fix.

    Framework raises a NON-PlatformError with a cause-chain
    Recoverable on attempt 1; returns clean text on attempt 2. The
    in-loop classifier absorbs the transient, the retry succeeds,
    the user gets a normal ``agent.message`` and no
    ``thread.exchange_failed`` fires.

    REGRESSION GUARD: ``call_count == 2``. A single-attempt path
    (the 0.2 bug) would never reach attempt 2; the user would see
    a failed exchange on the first transient. This is the most
    user-visible behavior the fix unlocks.
    """
    framework = _TransientCauseChainFramework()
    events = await _drive_one_exchange(tmp_path, framework, max_retries=2)

    assert _failed(events) is None, (
        "transient failure should have been absorbed by retry; "
        f"got failed event: {_failed(events)}"
    )
    completed = _completed(events)
    assert completed is not None, "exchange should have completed cleanly"
    msg = _agent_message(events)
    assert msg is not None
    assert msg.data["content"]["text"] == "Recovered after retry."

    assert framework.call_count == 2, (
        f"framework should have been called exactly twice (transient + "
        f"successful retry); got {framework.call_count}. A single-attempt "
        "path would mean the in-loop classifier didn't fire."
    )


# ---------------------------------------------------------------------------
# TC-CLASSIFY-05 — the real CrewAIFramework wrap catches ConnectionError.
# ---------------------------------------------------------------------------


async def test_tc_eng_classify_05_crewai_framework_wrap_translates_connection_error(
    tmp_path: Path,
) -> None:
    """Direct test of the Step 2 (A) translation wrap inside
    ``crewai_framework.CrewAIFramework.run_exchange``. Patches the
    inner ``crew.kickoff_async`` to raise a bare ConnectionError;
    verifies the wrap surfaces ``ModelUpstreamError`` (NOT a bare
    ConnectionError, NOT a PlatformInternalError).

    Isolated component-level test of the adapter wrap, complementing
    TC-CLASSIFY-01's e2e integration of the same path through the
    retry pipeline.
    """
    _ = tmp_path  # unused — this test exercises the wrap directly
    from engine.crewai_framework import CrewAIFramework

    framework = CrewAIFramework()

    # Build the minimum kwargs run_exchange needs. The wrap fires
    # before kickoff_async returns, so the llm/tools handles don't
    # need to do anything beyond being present.
    async def _llm_handle(_request: Any) -> Any:  # pragma: no cover — never called
        msg = "unreachable in this test"
        raise RuntimeError(msg)

    # Patch the Crew.kickoff_async to raise the bare ConnectionError
    # CrewAI's OpenAI adapter would produce.
    class _BadCrew:
        async def kickoff_async(self) -> Any:
            raise ConnectionError("Failed to connect to OpenAI API: Request timed out.")

    with (
        patch("engine.crewai_framework.Crew", return_value=_BadCrew()),
        pytest.raises(ModelUpstreamError) as exc_info,
    ):
        await framework.run_exchange(
            system_prompt="hi",
            user_message="hi",
            history=(),
            llm=_llm_handle,
            tools=(),
        )

    assert exc_info.value.level.name == "RECOVERABLE"
    assert "Failed to connect" in exc_info.value.detail["response"]
    # The cause chain is preserved (Step 2 reviewer check 3).
    assert isinstance(exc_info.value.__cause__, ConnectionError)

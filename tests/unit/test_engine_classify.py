"""Unit tests for ``engine._classify`` — bug-platform-internal-error-boundary-swallow.md Step 2.

Pure-function tests for the cause-chain helper. Integration coverage
(driving an exchange end-to-end) lives in
``tests/integration/test_engine_boundary_classification.py`` (Step 3).

The helper matches on ``f"{module}.{name}"`` strings so the engine
does not hard-import any provider SDK at module load. These tests
build fake exception classes with the right ``__module__`` /
``__qualname__`` so the matcher fires without needing ``openai``
installed in the unit-test path.
"""

from __future__ import annotations

import pytest

from engine._classify import (
    _MAX_DEPTH,
    classify_from_cause_chain,
)
from errors import AgentUnauthorized, ModelUpstreamError


def _exception_with_module(module: str, name: str, msg: str) -> Exception:
    """Build an Exception whose ``type.__module__`` + ``__name__``
    match ``module`` + ``name``. The classifier matches by qualified
    string so a synthesised class with the right __module__ is
    indistinguishable from the real openai.APITimeoutError for its
    purposes — and lets the unit test stay free of provider SDK
    imports."""
    cls = type(name, (Exception,), {})
    cls.__module__ = module
    instance: Exception = cls(msg)
    return instance


# ---------------------------------------------------------------------------
# Happy path — known typed exception classified.
# ---------------------------------------------------------------------------


def test_classify_returns_model_upstream_error_for_openai_timeout() -> None:
    """The trigger case: ``openai.APITimeoutError`` (subclass of
    ``APIConnectionError``) appearing anywhere in __cause__ is
    classified as ``ModelUpstreamError`` (Recoverable). The engine's
    retry loop will then absorb it instead of fatally aborting."""
    typed = _exception_with_module("openai", "APITimeoutError", "Request timed out.")
    wrapped = ConnectionError("Failed to connect to OpenAI API: Request timed out.")
    wrapped.__cause__ = typed

    classified = classify_from_cause_chain(wrapped)
    assert isinstance(classified, ModelUpstreamError)
    assert classified.level.name == "RECOVERABLE"
    assert classified.detail["provider"] == "openai"
    assert "Request timed out" in classified.detail["response"]


def test_classify_recognises_rate_limit_error() -> None:
    typed = _exception_with_module("openai", "RateLimitError", "429 Too Many Requests")
    classified = classify_from_cause_chain(typed)
    assert isinstance(classified, ModelUpstreamError)
    assert classified.level.name == "RECOVERABLE"


def test_classify_recognises_authentication_error_as_fatal() -> None:
    """Auth errors don't retry — bad credentials won't fix themselves
    on attempt 2. Classified as AgentUnauthorized (Fatal) so the
    exchange ends and the user fixes their API key."""
    typed = _exception_with_module("openai", "AuthenticationError", "Invalid API key")
    classified = classify_from_cause_chain(typed, agent_id="ag_test")
    assert isinstance(classified, AgentUnauthorized)
    assert classified.level.name == "FATAL"
    assert classified.detail["agent_id"] == "ag_test"
    assert classified.detail["action"] == "model.call"


def test_classify_walks_chain_to_find_typed_deep_in_chain() -> None:
    """The known type can be three layers deep — provider → adapter →
    intermediate wrapper. The helper walks via ``__cause__`` until it
    finds a match (or exhausts the chain)."""
    typed = _exception_with_module("openai", "APITimeoutError", "timeout")
    middle = RuntimeError("middle-layer wrap")
    middle.__cause__ = typed
    top = ConnectionError("top-layer flatten")
    top.__cause__ = middle

    classified = classify_from_cause_chain(top)
    assert isinstance(classified, ModelUpstreamError)


# ---------------------------------------------------------------------------
# Negative — None for unknown / no chain.
# ---------------------------------------------------------------------------


def test_classify_returns_none_when_no_known_type_in_chain() -> None:
    """A plain ``AttributeError`` from inside engine code (no provider
    typed exception in the chain) returns None — the boundary's
    fallback to ``PlatformInternalError`` is the right surface for
    real engine bugs."""
    exc = AttributeError("'NoneType' object has no attribute 'foo'")
    assert classify_from_cause_chain(exc) is None


def test_classify_returns_none_when_chain_is_empty() -> None:
    """A naked exception with no __cause__ returns None unless the
    naked exception itself is a known type. Plain ConnectionError
    (no chain, no openai subclass) is NOT classified — generic
    ConnectionErrors could come from anywhere; the adapter-side (A)
    translation is the right layer to catch the CrewAI-specific
    shape."""
    exc = ConnectionError("generic, no chain")
    assert classify_from_cause_chain(exc) is None


# ---------------------------------------------------------------------------
# Robustness — cycle + max-depth.
# ---------------------------------------------------------------------------


def test_classify_terminates_on_cycle() -> None:
    """If user code constructs a __cause__ loop (legal Python — the
    chain isn't validated), the walker must terminate. Without the
    seen-set guard this would hang the engine boundary."""
    a = ValueError("a")
    b = ValueError("b")
    a.__cause__ = b
    b.__cause__ = a  # cycle

    # Must NOT hang. Returns None because neither node is a known
    # typed exception.
    classified = classify_from_cause_chain(a)
    assert classified is None


def test_classify_stops_at_max_depth() -> None:
    """A pathological chain longer than ``_MAX_DEPTH`` does not
    cause unbounded work. The cap is generous (real chains rarely
    exceed 3 hops); this test just proves the limit is enforced."""
    # Build a chain longer than _MAX_DEPTH, with the known type at
    # the very bottom (past the cap).
    typed = _exception_with_module("openai", "APITimeoutError", "deep")
    current: BaseException = typed
    for i in range(_MAX_DEPTH + 5):
        wrapper: BaseException = RuntimeError(f"layer-{i}")
        wrapper.__cause__ = current
        current = wrapper

    # The typed exception is deeper than the cap; walker stops before
    # reaching it. Returns None.
    classified = classify_from_cause_chain(current)
    assert classified is None


# ---------------------------------------------------------------------------
# Engine doesn't import openai at module load.
# ---------------------------------------------------------------------------


def test_engine_classify_module_does_not_import_openai_sdk() -> None:
    """The whole point of string matching (vs. isinstance) is that
    the engine module stays loadable without provider SDKs on the
    PYTHONPATH. Verify by inspecting the module's source."""
    import inspect

    import engine._classify as classify_module

    source = inspect.getsource(classify_module)
    # No `import openai` line. The matching is by ``module.name``
    # string — see _RECOVERABLE_UPSTREAM literal set.
    assert "import openai" not in source
    assert "from openai" not in source


# ---------------------------------------------------------------------------
# Helper coverage — pytest parametrize over the recognized types.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("openai", "APITimeoutError"),
        ("openai", "APIConnectionError"),
        ("openai", "RateLimitError"),
    ],
)
def test_classify_recognises_all_recoverable_upstream_types(
    module: str, name: str
) -> None:
    """Every entry in ``_RECOVERABLE_UPSTREAM`` actually fires the
    ModelUpstreamError branch. Adding a row to the literal set
    without a corresponding match path would silently regress."""
    typed = _exception_with_module(module, name, "test")
    classified = classify_from_cause_chain(typed)
    assert isinstance(classified, ModelUpstreamError)


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("openai", "AuthenticationError"),
        ("openai", "PermissionDeniedError"),
    ],
)
def test_classify_recognises_all_fatal_auth_types(module: str, name: str) -> None:
    typed = _exception_with_module(module, name, "test")
    classified = classify_from_cause_chain(typed)
    assert isinstance(classified, AgentUnauthorized)

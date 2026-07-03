"""Fake LLM — scripted model client for deterministic, zero-token tests.

Per handbook §8.3:
- implements the model-client interface;
- configured per-test with a scripted response list (text, tool-call,
  error);
- records the context it was asked to process so tests can assert on
  what the model *would* have seen (the Materialization Rule check).

All data types here are Pydantic v2 models (handbook §2.1). The model
client is async-only because I/O is async-first per the handbook; a
sync entry point would let engine code drift away from the real
async client contract.

The model-client Protocol itself lives in the engine package (not yet
shipped). When it lands, this Fake conforms to it without changing
test call sites.
"""

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from errors import PlatformError


class TextResponse(BaseModel):
    """A scripted plain-text response from the model."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["text"] = "text"
    text: str


class ToolCallResponse(BaseModel):
    """A scripted tool-call response. The engine should dispatch it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["tool_call"] = "tool_call"
    tool: str
    args: dict[str, Any]
    tool_call_id: str | None = None


class ErrorResponse(BaseModel):
    """A scripted error: the model client raises an exception of this kind."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["error"] = "error"
    error_type: str
    message: str


# A tagged union — Pydantic uses the `kind` Literal to discriminate. The
# discriminator makes validation explicit and gives mypy the right
# narrowing inside `isinstance` checks below.
FakeLLMResponse = Annotated[
    TextResponse | ToolCallResponse | ErrorResponse,
    Field(discriminator="kind"),
]


class FakeLLMScriptExhaustedError(RuntimeError):
    """Raised when a test asks the Fake LLM for more responses than scripted.

    Surfaces as a test failure rather than silent fallback; an exhausted
    script almost always means the test setup is missing a response.
    """


class FakeLLMScriptedError(RuntimeError):
    """Raised by the Fake LLM when the script directs an error response.

    The `error_type` attribute carries the catalog-style code so a test
    can assert the failure path resolves to the right registered code
    once the error catalog ships.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class RecordedCall(BaseModel):
    """One recorded interaction with the Fake LLM.

    Captures whatever the engine passed as the context for the call so
    tests can assert on the Materialization Rule: what the model was
    asked to process equals what the events captured (handbook §8.6's
    `assert_materialization` helper consumes these).
    """

    model_config = ConfigDict(frozen=True)

    context: dict[str, Any]
    response: FakeLLMResponse


class FakeLLM(BaseModel):
    """Deterministic, zero-token model stub.

    Usage::

        fake = FakeLLM(script=[
            ToolCallResponse(tool="read", args={"path": "data/x.md"}),
            TextResponse(text="Based on the file, the answer is 42."),
        ])
        ...
        assert fake.calls[0].context["system_prompt"] == ...

    `script` is consumed left-to-right; `calls` accumulates one
    `RecordedCall` per `agenerate` invocation regardless of whether the
    response was text, tool-call, or error.
    """

    model_config = ConfigDict(arbitrary_types_allowed=False)

    script: list[FakeLLMResponse] = Field(default_factory=list)
    calls: list[RecordedCall] = Field(default_factory=list)

    async def agenerate(self, context: dict[str, Any]) -> FakeLLMResponse:
        """Return the next scripted response and record the call.

        `context` is whatever the engine assembled and would have sent
        to a real model: system prompt, conversational messages, tool
        definitions. The shape stays loose until the engine publishes a
        typed model-client Protocol.

        Raises:
            FakeLLMScriptExhaustedError: the script has no more responses.
            FakeLLMScriptedError: the next scripted response is an
                `ErrorResponse`; the recorded call is still appended so
                tests can introspect what the model was asked.
        """
        if not self.script:
            msg = (
                "FakeLLM script exhausted: the engine requested another "
                "response but the test did not script one."
            )
            raise FakeLLMScriptExhaustedError(msg)
        response = self.script.pop(0)
        self.calls.append(RecordedCall(context=context, response=response))
        if isinstance(response, ErrorResponse):
            raise FakeLLMScriptedError(response.error_type, response.message)
        return response

    def reset_calls(self) -> None:
        """Drop recorded calls and start over. Script is preserved."""
        self.calls.clear()

    def inject_error(self, error_type: str, *, message: str = "") -> None:
        """Phase 1.3 (Step 20) helper — schedule an :class:`ErrorResponse`
        as the next scripted output.

        Equivalent to ``script.insert(0, ErrorResponse(...))`` but
        gives tests a more readable call site for
        deterministic error injection:

            fake.inject_error("model.rate_limited")

        forces the next ``agenerate`` call to raise. The fallback
        loop's classification layer (per-connector mapping) is
        responsible for turning the raised
        :class:`FakeLLMScriptedError` into the typed PlatformError
        the engine ultimately catches; for tests that exercise the
        engine directly, the engine's test adapter does this mapping
        explicitly (see :mod:`tests.fixtures.fake_llm.platform_error_for_scripted`).
        """
        self.script.insert(
            0,
            ErrorResponse(
                error_type=error_type,
                message=message or f"injected {error_type}",
            ),
        )


def platform_error_for_scripted(
    exc: FakeLLMScriptedError, *, provider: str = "fake", model: str = "fake-model"
) -> "PlatformError":
    """Map a ``FakeLLMScriptedError`` into the catalogued PlatformError.

    Centralises the Fake → catalog mapping so test adapters share
    the table. New ``model.*`` codes added later flow through this
    function; if a test scripts an error_type that has no mapping,
    the function raises ``ValueError`` (test setup bug).
    """
    from errors import (
        ModelAuthenticationFailed,
        ModelBadRequest,
        ModelContextOverflow,
        ModelRateLimited,
        ModelServerError,
        ModelTimeout,
        ModelUpstreamError,
    )

    code = exc.error_type
    msg = str(exc)
    if code == "model.rate_limited":
        return ModelRateLimited(provider=provider, model=model)
    if code == "model.server_error":
        return ModelServerError(provider=provider, model=model, status_code=503)
    if code == "model.timeout":
        return ModelTimeout(provider=provider, model=model, timeout_seconds=30.0)
    if code == "model.context_overflow":
        return ModelContextOverflow(
            provider=provider, model=model, requested_tokens=200_000
        )
    if code == "model.authentication_failed":
        return ModelAuthenticationFailed(
            provider=provider, model=model, status_code=401
        )
    if code == "model.bad_request":
        return ModelBadRequest(
            provider=provider,
            model=model,
            status_code=400,
            reason=msg or "malformed",
        )
    if code == "model.upstream_error":
        # Phase 1.1 generic — kept for back-compat with existing tests.
        return ModelUpstreamError(provider=provider, http_status=500, response=msg)
    msg_err = (
        f"FakeLLMScriptedError {code!r} has no mapping in "
        f"platform_error_for_scripted; add it before testing"
    )
    raise ValueError(msg_err)

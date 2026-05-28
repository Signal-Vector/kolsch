"""Fake LLM — scripted model client for deterministic, zero-token tests.

Per handbook §8.3:
- implements the model-client interface;
- configured per-test with a scripted response list (text, tool-call,
  error);
- records the context it was asked to process so tests can assert on
  what the model *would* have seen (the Materialization Rule check).

The model-client contract is not yet declared in code (engine module
lands later). This module ships the scripted-response data classes and
the recorder; the engine-facing `generate(...)` method here uses
`dict[str, object]` for the context until the typed model-client
Protocol is published. When the Protocol lands, this stub conforms
without changing test call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict


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
    args: dict[str, object]
    tool_call_id: str | None = None


class ErrorResponse(BaseModel):
    """A scripted error: the model client raises an exception of this kind."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["error"] = "error"
    error_type: str
    message: str


FakeLLMResponse = TextResponse | ToolCallResponse | ErrorResponse


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


@dataclass
class RecordedCall:
    """One recorded interaction with the Fake LLM.

    Captures whatever the engine passed as the context for the call so
    tests can assert on the Materialization Rule: what the model was
    asked to process equals what the events captured (handbook §8.6's
    `assert_materialization` helper consumes these).
    """

    context: dict[str, object]
    response: FakeLLMResponse


@dataclass
class FakeLLM:
    """Deterministic, zero-token model stub.

    Usage:
        fake = FakeLLM(script=[
            ToolCallResponse(tool="read", args={"path": "data/x.md"}),
            TextResponse(text="Based on the file, the answer is 42."),
        ])
        ...
        assert fake.calls[0].context["system_prompt"] == ...
    """

    script: list[FakeLLMResponse] = field(default_factory=list)
    calls: list[RecordedCall] = field(default_factory=list)

    def generate(self, context: dict[str, object]) -> FakeLLMResponse:
        """Return the next scripted response and record the call.

        `context` is whatever the engine assembled and would have sent to
        a real model: system prompt, conversational messages, tool
        definitions. The shape stays loose until the engine publishes a
        typed model-client Protocol.
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

    async def agenerate(self, context: dict[str, object]) -> FakeLLMResponse:
        """Async variant — same scripted behaviour.

        The model-client interface is async by handbook §2.1 (I/O is
        async). This method exists so engine code that awaits the client
        works against the Fake without monkey-patching.
        """
        return self.generate(context)

    def reset(self) -> None:
        """Drop recorded calls and start over. Script is preserved."""
        self.calls.clear()

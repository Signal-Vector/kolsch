"""``jev_decide`` — typed decisions from Jev (TypeSafe AI "System One").

This file IS the tool. See ``read_file.py`` for the file-IS-the-tool
rationale (TOOL-R10 hard rule).

Jev is not a chat LLM. The caller sends a ``state`` (the content to
judge) plus named, typed ``questions`` (``choice`` / ``score`` /
``noul``) and gets back typed answers with calibrated probabilities.
Many questions in ONE call is the intended batching mechanism.

Transport (v1): OpenRouter ``POST <base_url>/v1/systemone``, which is
wire-identical to TypeSafe's native endpoint. The ``_PROFILES`` seam
holds both; only ``openrouter`` is selectable in v1, so switching to
direct TypeSafe (or a later gateway) is a data change, not a code
change. Credentials reuse the ``OPENROUTER_API_KEY`` that
``kolsch connect openrouter`` writes to ``.env``.

Kolsch-only question fields (``fail_below``) are stripped before the
request is sent. After a successful response, any question whose
metric (``confidence`` for choice, ``score`` for score, ``noul`` for
noul) is strictly below its ``fail_below`` raises
``JevBelowThreshold`` (``tool.jev_below_threshold``).

Returns ``{model, answers, usage}``; ``usage.cost`` is present only
when the upstream sent it.

Spec: process/spec-136-jev-decision-tool-and-node.md (M1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import PydanticCustomError

from errors import JevBelowThreshold, SecretNotFound, ToolExecutionFailed, ToolTimeout
from errors.engine import ExchangeCancelled
from errors.recovery import recovery_hint
from tools.decorator import ToolContext, tool

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import httpx as httpx_mod

_TOOL_NAME: Final = "jev_decide"
_DEFAULT_MODEL: Final = "typesafe/jev-1.14"
_OPENROUTER_SECRET: Final = "OPENROUTER_API_KEY"  # noqa: S105 — a secret NAME

# Per-request budget. Jev answers in ~100-500 ms; 30 s leaves ample
# headroom for a cold upstream without hanging a chat turn.
_REQUEST_TIMEOUT_SECONDS: float = 30.0
# Retry policy for 429 / 529 (rate limited / overloaded).
_RETRY_STATUSES: Final = frozenset({429, 529})
_MAX_ATTEMPTS: Final = 3
_BACKOFF_BASE_SECONDS: float = 1.0
_MAX_RETRY_WAIT_SECONDS: float = 10.0
# Whole-call ceiling: every attempt at its full request budget plus
# every retry wait at its cap.
_TOOL_BUDGET_SECONDS: Final = (
    _REQUEST_TIMEOUT_SECONDS * _MAX_ATTEMPTS
    + _MAX_RETRY_WAIT_SECONDS * (_MAX_ATTEMPTS - 1)
)
# Cap on upstream error text forwarded to the calling model.
_MAX_UPSTREAM_MESSAGE_CHARS: Final = 2000
_MAX_UPSTREAM_VALIDATION_ITEMS: Final = 20


# ---------------------------------------------------------------------------
# Transport profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Profile:
    """One Jev transport: where to POST, which secret, which default model.

    ``selectable`` gates whether the tool may use the profile. v1
    ships ``openrouter`` as the only selectable profile; ``typesafe``
    is defined as data so enabling it later (with config-driven
    secret selection) is a data change.
    """

    name: str
    base_url: str
    secret_name: str
    default_model: str
    auth_recovery_hint: str
    selectable: bool


_PROFILES: dict[str, _Profile] = {
    "openrouter": _Profile(
        name="openrouter",
        base_url="https://openrouter.ai/api",
        secret_name=_OPENROUTER_SECRET,
        default_model=_DEFAULT_MODEL,
        auth_recovery_hint="kolsch connect openrouter",
        selectable=True,
    ),
    "typesafe": _Profile(
        name="typesafe",
        base_url="https://api.typesafe.ai",
        secret_name="TYPESAFE_API_KEY",  # noqa: S106 — a secret NAME
        default_model="jev-1.14.0",
        auth_recovery_hint="set TYPESAFE_API_KEY in the workspace .env",
        selectable=False,
    ),
}
_ACTIVE_PROFILE: Final = "openrouter"


# ---------------------------------------------------------------------------
# Argument schema
# ---------------------------------------------------------------------------

# Upstream accepts a string, object, or array wherever free-form text
# goes (state, instructions, criteria descriptions). Array items are
# typed (string or object) rather than ``Any``: OpenAI-backed callers
# reject a tool schema whose array ``items`` has no ``type`` key.
_FreeForm = str | dict[str, Any] | list[str | dict[str, Any]]


def _count_between[T: (dict[str, Any], list[Any])](
    low: int, high: int | None, what: str
) -> Callable[[T], T]:
    """Build a validator bounding a dict's / list's size to ``[low, high]``.

    ``high=None`` means no upper bound.

    Size bounds are enforced here rather than as ``min_length`` /
    ``max_length`` field constraints because those emit JSON Schema
    ``minProperties`` / ``maxProperties``, which OpenAI-backed callers
    reject in a tool's parameter schema. The bounds are stated in each
    field's description instead.

    Raises ``PydanticCustomError`` (not ``ValueError``) so the
    validation error's ``ctx`` stays JSON-serialisable: a ``ValueError``
    lands in ``ctx["error"]`` as an exception object, which the
    ``tool.invalid_arguments`` event payload cannot serialise.
    """

    def _check(value: T) -> T:
        if len(value) < low or (high is not None and len(value) > high):
            bounds = f"at least {low}" if high is None else f"{low}-{high}"
            error_type = "jev_count_out_of_range"
            template = "expected {bounds} {what}, got {count}"
            raise PydanticCustomError(
                error_type,
                template,
                {"bounds": bounds, "what": what, "count": len(value)},
            )
        return value

    return _check


class _QuestionBase(BaseModel):
    """Fields shared by every question type."""

    model_config = ConfigDict(extra="forbid")

    instructions: _FreeForm = Field(
        description=(
            "What Jev should decide, rate, or judge true/false, phrased "
            "about the state. A string, or a structured object/array."
        ),
    )


class ChoiceQuestion(_QuestionBase):
    """Pick exactly one of the named options in ``criteria``."""

    type: Literal["choice"]
    criteria: Annotated[
        dict[str, _FreeForm | None],
        AfterValidator(_count_between(1, 255, "options")),
    ] = Field(
        description=(
            "1-255 options: option name -> description of when it applies "
            "(null to let the name speak for itself). The answer's "
            "`choice` is one of these names."
        ),
    )
    fail_below: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Optional. Fail the call if the answer's `confidence` (0-1) is "
            "strictly below this value. Omit to never fail on confidence."
        ),
    )


class ScoreQuestion(_QuestionBase):
    """Rate against an ordered rubric; the answer is a 0-indexed expected level."""

    type: Literal["score"]
    criteria: Annotated[
        list[_FreeForm],
        AfterValidator(_count_between(1, 10, "levels")),
    ] = Field(
        description=(
            "1-10 ordered level descriptions, lowest first. The answer's "
            "`score` is the probability-weighted level index (0-based, may "
            "be fractional)."
        ),
    )
    fail_below: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Optional. Fail the call if the answer's `score` (0-based level "
            "index) is strictly below this value."
        ),
    )


class NoulCriteria(BaseModel):
    """Optional clarification of what counts as yes / no for a noul question."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    true_: _FreeForm | None = Field(
        default=None,
        alias="true",
        description="What counts as a yes / true answer.",
    )
    false_: _FreeForm | None = Field(
        default=None,
        alias="false",
        description="What counts as a no / false answer.",
    )


class NoulQuestion(_QuestionBase):
    """Yes/no question or statement; the answer is P(yes) in 0-1."""

    type: Literal["noul"]
    criteria: NoulCriteria | None = Field(
        default=None,
        description=("Optional {true, false} descriptions of what counts as yes / no."),
    )
    fail_below: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Optional. Fail the call if the answer's `noul` (P(yes), 0-1) is "
            "strictly below this value."
        ),
    )


JevQuestion = Annotated[
    ChoiceQuestion | ScoreQuestion | NoulQuestion,
    Field(discriminator="type"),
]


class JevDecideArgs(BaseModel):
    """Argument schema for the ``jev_decide`` tool."""

    model_config = ConfigDict(extra="forbid")

    state: _FreeForm = Field(
        description=(
            "The content every question refers to: a string, or a JSON "
            "object/array (e.g. {'subject': ..., 'body': ...})."
        ),
    )
    questions: Annotated[
        dict[str, JevQuestion],
        AfterValidator(_count_between(1, None, "questions")),
    ] = Field(
        description=(
            "One or more named questions about the state. Batch related "
            "questions into ONE call. Each has `type` 'choice', 'score', or "
            "'noul', `instructions`, type-specific `criteria`, and an "
            "optional `fail_below` threshold."
        ),
    )
    model: str = Field(
        default=_DEFAULT_MODEL,
        description=(
            "Jev model id. Default 'typesafe/jev-1.14'. Leave unset unless "
            "the user asks for a specific version."
        ),
    )


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class _ChoiceAnswer(BaseModel):
    # NaN / Infinity metrics would silently pass the `<` threshold
    # comparison; reject them as a malformed response (fail closed).
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    type: Literal["choice"]
    choice: str
    confidence: float
    probabilities: dict[str, float] | None = None


class _ScoreAnswer(BaseModel):
    # NaN / Infinity metrics would silently pass the `<` threshold
    # comparison; reject them as a malformed response (fail closed).
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    type: Literal["score"]
    score: float
    confidence: float
    legend: dict[str, Any] | None = None
    probabilities: dict[str, float] | None = None


class _NoulAnswer(BaseModel):
    # NaN / Infinity metrics would silently pass the `<` threshold
    # comparison; reject them as a malformed response (fail closed).
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    type: Literal["noul"]
    noul: float


_JevAnswer = Annotated[
    _ChoiceAnswer | _ScoreAnswer | _NoulAnswer,
    Field(discriminator="type"),
]


class _Usage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int
    output_tokens: int
    cost: float | None = None


class _JevResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    answers: dict[str, _JevAnswer] = Field(min_length=1)
    usage: _Usage


# Metric each question type's `fail_below` is compared against.
_THRESHOLD_METRIC: Final[dict[str, str]] = {
    "choice": "confidence",
    "score": "score",
    "noul": "noul",
}


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@tool(
    name=_TOOL_NAME,
    summary=(
        "Ask Jev fast, typed questions (choice / score / yes-no) about some "
        "content and get calibrated answers."
    ),
    description=(
        "Ask Jev (TypeSafe AI's decision model) one or more typed questions "
        "about a piece of content (`state`) and get back calibrated, "
        "structured answers in well under a second. Use it for BOUNDED "
        "judgements: classify or route something into one of a fixed set "
        "of options, rate it on an ordered rubric, or decide a yes/no "
        "question. Do NOT use it for free-text answers, summaries, "
        "extraction, or open-ended reasoning — it only returns the typed "
        "answers below."
        "\n\n"
        "Question types (each named question has `type`, `instructions`, "
        "and `criteria`):\n"
        "- 'choice': criteria = {option_name: description or null} (1-255 "
        "options). Answer: {type, choice: <option name>, confidence 0-1, "
        "probabilities: {option: p}}.\n"
        "- 'score': criteria = [ordered level descriptions, lowest first] "
        "(1-10 levels). Answer: {type, score: 0-based expected level (may "
        "be fractional), confidence, legend, probabilities}.\n"
        "- 'noul': yes/no; criteria optional {true: ..., false: ...}. "
        "Answer: {type, noul: probability of yes 0-1}."
        "\n\n"
        "Batch related questions into ONE call (e.g. category + urgency + "
        "is_spam together) — that is cheaper and faster than several calls. "
        "Optional per-question `fail_below` fails the whole call when the "
        "answer's confidence (choice), score (score), or noul (noul) is "
        "strictly below it; only set it when the user asked for a minimum "
        "certainty."
        "\n\n"
        "Example args: {state: 'I was charged twice.', questions: {billing: "
        "{type: 'noul', instructions: 'Is this about billing?'}}}. Returns "
        "{model, answers: {<question name>: <answer>}, usage}."
    ),
    args_schema=JevDecideArgs,
    secrets_needed=[_OPENROUTER_SECRET],
    timeout_seconds=_TOOL_BUDGET_SECONDS,
)
async def jev_decide(args: JevDecideArgs, ctx: ToolContext) -> dict[str, Any]:
    """Ask Jev ``args.questions`` about ``args.state``.

    :returns: ``{model, answers, usage}`` — ``answers`` keyed by question
        name exactly as Jev returned them; ``usage.cost`` only when the
        upstream sent it.
    :raises tool.jev_below_threshold: a ``fail_below`` was breached.
    :raises tool.execution_failed: upstream HTTP error, exhausted retry
        budget, network error, or a malformed upstream body.
    :raises tool.timeout: a request exceeded its budget.
    :raises secret.not_found: the profile's API key is unavailable.
    :raises exchange.cancelled: cancelled via ``ctx.cancellation``.
    """
    profile = _active_profile()
    api_key = ctx.secrets.get(profile.secret_name)
    if not api_key:
        raise SecretNotFound(
            name=profile.secret_name,
            where="kolsch.tools.jev_decide",
        )

    # Lazy import so a missing runtime dep surfaces as a Diagnostic on
    # the tool call, not as a module-load failure.
    try:
        import httpx  # noqa: PLC0415 — deferred by design
    except ImportError as exc:
        raise ToolExecutionFailed(
            tool_name=_TOOL_NAME,
            error_class="RuntimeDepMissing",
            recovery_hint=recovery_hint("RuntimeDepMissing"),
        ) from exc

    url = f"{profile.base_url.rstrip('/')}/v1/systemone"
    body = _wire_body(args, profile)
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS),
        ) as client:
            resp, attempts = await _post_with_retry(
                lambda: client.post(url, json=body, headers=headers),
                ctx,
            )
    except ExchangeCancelled:
        raise
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise ToolTimeout(
            tool_name=_TOOL_NAME,
            timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
        ) from exc
    except httpx.HTTPError as exc:
        raise ToolExecutionFailed(
            tool_name=_TOOL_NAME,
            error_class="NetworkError",
        ) from exc

    if resp.status_code != 200:
        raise _status_error(resp, attempts=attempts, profile=profile)

    raw_answers, validated, result = _parse_response(resp, args)
    _check_thresholds(args, validated, raw_answers)
    return result


def _active_profile() -> _Profile:
    """Return the active transport profile (read at call time, test-patchable)."""
    profile = _PROFILES[_ACTIVE_PROFILE]
    if not profile.selectable:
        raise ToolExecutionFailed(
            tool_name=_TOOL_NAME,
            error_class="ProfileNotSelectable",
            extra_detail={"profile": profile.name},
        )
    return profile


def _wire_body(args: JevDecideArgs, profile: _Profile) -> dict[str, Any]:
    """Build the exact ``{model, state, questions}`` request body.

    Kolsch-only fields (``fail_below``) are stripped from every
    question; only fields the caller actually set are sent, so the
    upstream applies its own defaults. When the caller left ``model``
    unset, the profile's default model id is used (OpenRouter's
    ``typesafe/jev-1.14`` vs TypeSafe's bare ``jev-1.14.0``); the rest
    of the body is identical across profiles.
    """
    model = args.model if "model" in args.model_fields_set else profile.default_model
    questions: dict[str, dict[str, Any]] = {}
    for name, question in args.questions.items():
        dumped = question.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
            exclude={"fail_below"},
        )
        # An explicit `criteria: null` (only valid on noul) means "none".
        if "criteria" in dumped and dumped["criteria"] is None:
            del dumped["criteria"]
        questions[name] = dumped
    return {"model": model, "state": args.state, "questions": questions}


async def _post_with_retry(
    send: Callable[[], Coroutine[Any, Any, httpx_mod.Response]],
    ctx: ToolContext,
) -> tuple[httpx_mod.Response, int]:
    """POST, retrying 429 / 529 up to ``_MAX_ATTEMPTS`` attempts in total.

    Returns the final response (which may still be a 429 / 529 when the
    budget is exhausted) and the number of attempts made. Every request
    and every wait races ``ctx.cancellation``.
    """
    attempt = 0
    while True:
        attempt += 1
        resp = await _request_with_cancel(send(), ctx)
        if resp.status_code not in _RETRY_STATUSES or attempt >= _MAX_ATTEMPTS:
            return resp, attempt
        await _sleep_with_cancel(_retry_delay(resp, attempt), ctx)


def _retry_delay(resp: httpx_mod.Response, attempt: int) -> float:
    """Seconds to wait before the next attempt.

    Honours a ``retry-after`` header in either form (delta-seconds or
    HTTP-date); otherwise backs off exponentially (1 s, 2 s, ...).
    Always capped at ``_MAX_RETRY_WAIT_SECONDS``.
    """
    raw = resp.headers.get("retry-after")
    if raw is not None:
        seconds = _parse_retry_after(raw)
        if math.isfinite(seconds) and seconds >= 0:
            return min(seconds, _MAX_RETRY_WAIT_SECONDS)
    backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    return float(min(backoff, _MAX_RETRY_WAIT_SECONDS))


def _parse_retry_after(raw: str) -> float:
    """Return a ``retry-after`` value in seconds, or NaN when unparseable."""
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return math.nan
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


async def _request_with_cancel(
    request_coro: Coroutine[Any, Any, httpx_mod.Response],
    ctx: ToolContext,
) -> httpx_mod.Response:
    """Race one HTTP request against ``ctx.cancellation`` when present.

    Mirrors ``fetch_url._request_with_cancel``: with no cancellation
    token (programmatic callers) the request is bounded by
    ``asyncio.wait_for`` instead.
    """
    cancellation = getattr(ctx, "cancellation", None)
    if cancellation is None:
        resp: httpx_mod.Response = await asyncio.wait_for(
            request_coro,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        return resp

    request_task = asyncio.create_task(request_coro)
    cancel_task = asyncio.create_task(cancellation.wait_cancelled())
    try:
        done, _pending = await asyncio.wait(
            {request_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        # The awaiting task itself was cancelled (or the wait raised):
        # don't orphan the in-flight request.
        request_task.cancel()
        with contextlib.suppress(BaseException):
            await request_task
        raise
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
    if cancel_task in done and not request_task.done():
        request_task.cancel()
        with contextlib.suppress(BaseException):
            await request_task
        raise ExchangeCancelled(
            cancelled_at_event_id="",
            where="kolsch.tools.jev_decide._request_with_cancel",
        )
    final_resp: httpx_mod.Response = await request_task
    return final_resp


async def _sleep_with_cancel(seconds: float, ctx: ToolContext) -> None:
    """Sleep ``seconds``, returning early with ``ExchangeCancelled`` on cancel."""
    cancellation = getattr(ctx, "cancellation", None)
    if cancellation is None:
        await asyncio.sleep(seconds)
        return

    sleep_task = asyncio.create_task(asyncio.sleep(seconds))
    cancel_task = asyncio.create_task(cancellation.wait_cancelled())
    try:
        done, _pending = await asyncio.wait(
            {sleep_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in (sleep_task, cancel_task):
            if not task.done():
                task.cancel()
    if cancel_task in done:
        raise ExchangeCancelled(
            cancelled_at_event_id="",
            where="kolsch.tools.jev_decide._sleep_with_cancel",
        )


def _status_error(
    resp: httpx_mod.Response,
    *,
    attempts: int,
    profile: _Profile,
) -> ToolExecutionFailed:
    """Map a non-200 upstream response to ``ToolExecutionFailed``.

    The error class names the status (``UpstreamHTTP<status>``);
    ``detail`` carries the status plus the upstream message so a
    calling agent can correct its question. The request's
    ``Authorization`` header is never echoed.
    """
    status = resp.status_code
    extra: dict[str, Any] = {"status": status}
    hint: str | None = None
    if status in _RETRY_STATUSES:
        extra["reason"] = (
            "rate limited" if status == 429 else "overloaded"
        ) + f"; retry budget exhausted after {attempts} attempts"
        extra["attempts"] = attempts
    elif status in (401, 403):
        extra["reason"] = "authentication failed"
        hint = profile.auth_recovery_hint
    elif status >= 500:
        extra["reason"] = "upstream failure"
    else:
        extra["reason"] = "request rejected"
    extra["upstream_message"] = _upstream_message(resp)
    return ToolExecutionFailed(
        tool_name=_TOOL_NAME,
        error_class=f"UpstreamHTTP{status}",
        recovery_hint=hint,
        extra_detail=extra,
    )


def _upstream_message(resp: httpx_mod.Response) -> str | list[dict[str, Any]]:
    """Extract a model-readable message from an upstream error body.

    Handles the TypeSafe / FastAPI validation shape
    (``{"detail": [{loc, msg, type}, ...]}``), OpenRouter's
    ``{"error": {"message": ...}}``, and plain ``detail`` / ``message``
    strings; falls back to the truncated raw text.
    """
    try:
        data = resp.json()
    except ValueError:
        return _truncate(resp.text)
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, list):
            return [
                {
                    "loc": item.get("loc"),
                    "msg": item.get("msg"),
                    "type": item.get("type"),
                }
                for item in detail[:_MAX_UPSTREAM_VALIDATION_ITEMS]
                if isinstance(item, dict)
            ]
        if isinstance(detail, str):
            return _truncate(detail)
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return _truncate(error["message"])
        if isinstance(error, str):
            return _truncate(error)
        message = data.get("message")
        if isinstance(message, str):
            return _truncate(message)
    return _truncate(json.dumps(data))


def _truncate(text: str) -> str:
    if len(text) <= _MAX_UPSTREAM_MESSAGE_CHARS:
        return text
    return text[:_MAX_UPSTREAM_MESSAGE_CHARS] + "…"


def _malformed(reason: str) -> ToolExecutionFailed:
    return ToolExecutionFailed(
        tool_name=_TOOL_NAME,
        error_class="MalformedResponse",
        extra_detail={"reason": reason},
    )


def _parse_response(
    resp: httpx_mod.Response,
    args: JevDecideArgs,
) -> tuple[dict[str, Any], _JevResponse, dict[str, Any]]:
    """Validate a 200 body; return ``(raw_answers, validated, tool_result)``.

    Every asked question must have an answer of the matching type.
    """
    try:
        data = resp.json()
    except ValueError as exc:
        reason = "response body is not JSON"
        raise _malformed(reason) from exc
    try:
        validated = _JevResponse.model_validate(data)
    except ValidationError as exc:
        reason = f"response failed validation ({exc.error_count()} error(s))"
        raise _malformed(reason) from exc
    for name, question in args.questions.items():
        answer = validated.answers.get(name)
        if answer is None:
            reason = f"no answer for question {name!r}"
            raise _malformed(reason)
        if answer.type != question.type:
            reason = (
                f"answer for {name!r} has type {answer.type!r}, "
                f"expected {question.type!r}"
            )
            raise _malformed(reason)
    raw_answers: dict[str, Any] = data["answers"]
    raw_usage: dict[str, Any] = data["usage"]
    usage: dict[str, Any] = {
        "input_tokens": validated.usage.input_tokens,
        "output_tokens": validated.usage.output_tokens,
    }
    if "cost" in raw_usage:
        usage["cost"] = raw_usage["cost"]
    result = {"model": validated.model, "answers": raw_answers, "usage": usage}
    return raw_answers, validated, result


def _check_thresholds(
    args: JevDecideArgs,
    validated: _JevResponse,
    raw_answers: dict[str, Any],
) -> None:
    """Raise ``JevBelowThreshold`` if any ``fail_below`` is breached (D6).

    A metric equal to its threshold passes; questions without
    ``fail_below`` are never checked.
    """
    failures: list[dict[str, Any]] = []
    for name, question in args.questions.items():
        if question.fail_below is None:
            continue
        metric = _THRESHOLD_METRIC[question.type]
        value = float(getattr(validated.answers[name], metric))
        if value < question.fail_below:
            failures.append(
                {
                    "question": name,
                    "metric": metric,
                    "value": value,
                    "threshold": question.fail_below,
                }
            )
    if failures:
        raise JevBelowThreshold(failures=failures, answers=raw_answers)

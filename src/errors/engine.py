"""Agent Engine errors, plus the catalogued `platform.internal_error` safety net.

The engine boundary is where most Fatal-level escalations land —
authorization denial at pre-flight, retry-budget exhaustion,
cancellation, timeout, and the safety net for any otherwise-unhandled
exception. See the
[error model](../explanation/error-model.md) for the
classification contract.
"""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class AgentUnauthorized(PlatformError):
    """Raised when authorization denied the agent at engine pre-flight."""

    code = "agent.unauthorized"
    level = Level.FATAL

    def __init__(
        self,
        agent_id: str,
        action: str,
        principal: str,
        matched_rule: dict[str, Any] | None = None,
        *,
        where: str = "engine.preflight.authorize",
    ) -> None:
        super().__init__(
            message=f"Agent {agent_id!r} unauthorized for action {action!r}",
            where=where,
            detail={
                "agent_id": agent_id,
                "action": action,
                "principal": principal,
                "matched_rule": matched_rule,
            },
        )


class CapabilityMismatch(PlatformError):
    """Raised when the engine does not support a capability the config requires."""

    code = "agent.capability_mismatch"
    level = Level.FATAL

    def __init__(
        self,
        required: list[str],
        available: list[str],
        *,
        where: str = "engine.preflight.check_capabilities",
    ) -> None:
        missing = sorted(set(required) - set(available))
        super().__init__(
            message=(f"Engine missing required capabilities: {missing}"),
            where=where,
            detail={
                "required": list(required),
                "available": list(available),
            },
        )


class ModelRetryExhausted(PlatformError):
    """Raised when every retry attempt for a model call failed."""

    code = "model.retry_exhausted"
    level = Level.FATAL

    def __init__(
        self,
        attempts: list[dict[str, Any]],
        *,
        where: str = "engine.execute.model_call",
    ) -> None:
        super().__init__(
            message=f"All {len(attempts)} model-call attempts failed",
            where=where,
            # Attempts are sibling failures captured as Error envelopes,
            # not a `cause` chain — preserve the per-attempt detail
            # without conflating them with the wrapping fatal.
            detail={"attempts": list(attempts)},
        )


class ModelMalformedResponse(PlatformError):
    """Raised when the model returned a response the engine could not parse."""

    code = "model.malformed_response"
    level = Level.RECOVERABLE

    def __init__(
        self,
        raw_response: str,
        parse_error: str,
        *,
        where: str = "engine.execute.model_call",
    ) -> None:
        super().__init__(
            message=f"Model response did not parse: {parse_error}",
            where=where,
            detail={"raw_response": raw_response, "parse_error": parse_error},
        )


class ModelUpstreamError(PlatformError):
    """Raised when the model provider returned a transport-level failure."""

    code = "model.upstream_error"
    level = Level.RECOVERABLE

    def __init__(
        self,
        provider: str,
        http_status: int,
        response: str,
        *,
        where: str = "engine.execute.model_call",
    ) -> None:
        super().__init__(
            message=(f"Model provider {provider!r} returned HTTP {http_status}"),
            where=where,
            detail={
                "provider": provider,
                "http_status": http_status,
                "response": response,
            },
        )


class ExchangeCancelled(PlatformError):
    """Raised when the exchange was cancelled via `CancelToken`."""

    code = "exchange.cancelled"
    level = Level.FATAL

    def __init__(
        self,
        cancelled_at_event_id: str,
        *,
        where: str = "engine.execute.loop",
    ) -> None:
        super().__init__(
            message=f"Exchange cancelled at event {cancelled_at_event_id!r}",
            where=where,
            detail={"cancelled_at_event_id": cancelled_at_event_id},
        )


class ExchangeTimeout(PlatformError):
    """Raised when the exchange exceeded its budget."""

    code = "exchange.timeout"
    level = Level.FATAL

    def __init__(
        self,
        timeout_seconds: float,
        elapsed_seconds: float,
        *,
        where: str = "engine.execute.loop",
    ) -> None:
        super().__init__(
            message=(
                f"Exchange timed out after {elapsed_seconds}s "
                f"(budget {timeout_seconds}s)"
            ),
            where=where,
            detail={
                "timeout_seconds": timeout_seconds,
                "elapsed_seconds": elapsed_seconds,
            },
        )


class AssemblyProcedureFailed(PlatformError):
    """Raised when the Assembly Procedure failed at the named step."""

    code = "assembly.procedure_failed"
    level = Level.FATAL

    def __init__(
        self,
        assembly_version: str,
        failed_step: str,
        *,
        where: str = "engine.assemble_context",
    ) -> None:
        super().__init__(
            message=(
                f"Assembly procedure v{assembly_version} failed at step {failed_step!r}"
            ),
            where=where,
            detail={
                "assembly_version": assembly_version,
                "failed_step": failed_step,
            },
        )


class PlatformInternalError(PlatformError):
    """Raised at the engine boundary when an unhandled exception escapes.

    Fatal safety net — anything that reaches the engine boundary as a
    non-`PlatformError` exception is wrapped in this class. The
    original exception's type and args are captured in `detail`; the
    in-process object is preserved on `__cause__`.
    """

    code = "platform.internal_error"
    level = Level.FATAL

    def __init__(
        self,
        original_type: str,
        args: list[Any],
        *,
        where: str = "engine.execute.boundary",
        traceback: str | None = None,
    ) -> None:
        detail: dict[str, Any] = {
            "original_type": original_type,
            "args": list(args),
        }
        if traceback is not None:
            # Optional per the error model — present only when the
            # boundary captured a traceback.
            detail["traceback"] = traceback
        super().__init__(
            message=f"Internal platform error: {original_type}",
            where=where,
            detail=detail,
        )

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        where: str = "engine.execute.boundary",
        traceback: str | None = None,
    ) -> "PlatformInternalError":
        """Construct from an arbitrary exception caught at the engine boundary.

        Args:
            exc: The unhandled exception that reached the boundary.
            where: Dotted-string locator of the boundary call site.
            traceback: Optional formatted traceback to attach to
                `detail.traceback`.

        Returns:
            A `PlatformInternalError` whose `detail` carries the
            original exception's type and args.
        """
        return cls(
            original_type=f"{type(exc).__module__}.{type(exc).__name__}",
            args=list(exc.args),
            where=where,
            traceback=traceback,
        )

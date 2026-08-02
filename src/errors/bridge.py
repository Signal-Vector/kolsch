"""MCP tool-bridge errors (spec-120).

Failure modes of the `ToolBridgeServer` + `kolsch mcp bridge` pair —
the trust boundary that lets an external MCP client call kolsch tools
over a Unix socket. Two hard rules shape this module:

1. **Nothing here reaches the external wire.** The bridge maps every
   failure to a constant generic literal before it crosses the
   socket / stdio boundary; these classes exist for the *internal*
   log, the CLI exit path, and the catalog contract. Their `detail`
   payloads (which reason a bind was refused, which wire name
   collided) are server-side diagnostics only.
2. **Uniform auth failure.** Bad, expired, and already-bound tokens
   all raise the single [`BridgeAuthFailed`][errors.bridge.BridgeAuthFailed]
   class so no code path can accidentally build a token oracle out of
   distinct types; the `reason` lives in `detail` for the log.

Levels: transport/auth failures are Recoverable (the exchange in the
running kolsch process continues; only the external client's session
is refused). Security-invariant violations (`BridgeSocketInsecure`)
and catalog corruption (`BridgeNameCollision`) are Fatal — serving on
an insecure socket or with ambiguous routing would be an authz hole.
"""

from errors.base import PlatformError
from errors.level import Level


class BridgeAuthFailed(PlatformError):
    """A bridge `bind` was refused — bad, expired, or already-bound token.

    ONE class for all three reasons (no-oracle by construction). The
    distinguishing `reason` is server-log detail; the wire response is
    a constant generic refusal either way.
    """

    code = "bridge.auth_failed"
    level = Level.RECOVERABLE

    def __init__(
        self,
        reason: str,
        *,
        where: str = "tools.bridge.server.bind",
    ) -> None:
        super().__init__(
            message="Bridge bind refused",
            where=where,
            detail={"reason": reason},
        )


class BridgeNotBound(PlatformError):
    """A `list_tools`/`call_tool` frame arrived on an unbound connection."""

    code = "bridge.not_bound"
    level = Level.RECOVERABLE

    def __init__(
        self,
        op: str,
        *,
        where: str = "tools.bridge.server.dispatch",
    ) -> None:
        super().__init__(
            message="Bridge operation refused: connection is not bound",
            where=where,
            detail={"op": op},
        )


class BridgeSocketUnavailable(PlatformError):
    """The bridge socket could not be reached (connect/IPC failure)."""

    code = "bridge.socket_unavailable"
    level = Level.RECOVERABLE

    def __init__(
        self,
        transport_error: str,
        *,
        where: str = "tools.bridge.proxy.connect",
    ) -> None:
        super().__init__(
            message="Bridge socket unavailable",
            where=where,
            detail={"transport_error": transport_error},
        )


class BridgeSocketInsecure(PlatformError):
    """A socket/dir security invariant failed at `open()`.

    Covers: parent dir not owned by the current uid / wrong mode /
    symlinked, a pre-existing node at the socket path, or a post-bind
    verification failure (not a socket, wrong owner). Fatal — serving
    tool authority on an insecure path is never acceptable.
    """

    code = "bridge.socket_insecure"
    level = Level.FATAL

    def __init__(
        self,
        check: str,
        path: str,
        *,
        where: str = "tools.bridge.server.open",
    ) -> None:
        super().__init__(
            message=f"Bridge socket security check failed: {check}",
            where=where,
            detail={"check": check, "path": path},
        )


class BridgeFrameTooLarge(PlatformError):
    """A frame declared a length above the bridge's frame-size cap.

    Raised from the declared length BEFORE the body is read, so an
    adversarial peer cannot make the server buffer an arbitrary body.
    """

    code = "bridge.frame_too_large"
    level = Level.RECOVERABLE

    def __init__(
        self,
        declared: int,
        max_bytes: int,
        *,
        where: str = "tools.bridge.framing.read",
    ) -> None:
        super().__init__(
            message="Bridge frame exceeds the size cap",
            where=where,
            detail={"declared": declared, "max_bytes": max_bytes},
        )


class BridgeFrameMalformed(PlatformError):
    """A frame body was not a JSON object."""

    code = "bridge.frame_malformed"
    level = Level.RECOVERABLE

    def __init__(
        self,
        *,
        where: str = "tools.bridge.framing.read",
    ) -> None:
        super().__init__(
            message="Bridge frame body is not a JSON object",
            where=where,
            detail={},
        )


class BridgeMalformedRequest(PlatformError):
    """An MCP request failed structural validation at the proxy.

    Internal control flow for the stdio proxy: raised by a method
    handler, caught by the request wrapper, and answered as a JSON-RPC
    `-32602` with a constant message.
    """

    code = "bridge.malformed_request"
    level = Level.RECOVERABLE

    def __init__(
        self,
        method: str,
        *,
        where: str = "tools.bridge.proxy.dispatch",
    ) -> None:
        super().__init__(
            message="Malformed MCP request",
            where=where,
            detail={"method": method},
        )


class BridgeRequestRefused(PlatformError):
    """The bridge server answered a frame with a refusal.

    The proxy-side view of any `ok: false` frame — deliberately
    reason-free (the server's refusals are uniform on the wire).
    """

    code = "bridge.request_refused"
    level = Level.RECOVERABLE

    def __init__(
        self,
        op: str,
        *,
        where: str = "tools.bridge.proxy.request",
    ) -> None:
        super().__init__(
            message="Bridge request refused",
            where=where,
            detail={"op": op},
        )


class BridgeNameCollision(PlatformError):
    """Two registered tool names map to one MCP wire name.

    A routing + authz bug (a `tools/call` for the wire name could
    dispatch the wrong tool) — fail loudly, never disambiguate
    silently at serve time.
    """

    code = "bridge.name_collision"
    level = Level.FATAL

    def __init__(
        self,
        wire_name: str,
        first: str,
        second: str,
        *,
        where: str = "tools.bridge.server.catalog",
    ) -> None:
        super().__init__(
            message=f"Tool wire-name collision on {wire_name!r}",
            where=where,
            detail={"wire_name": wire_name, "first": first, "second": second},
        )

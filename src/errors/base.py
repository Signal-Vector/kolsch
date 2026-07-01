"""PlatformError base class, WrappedError, and the subclass registry.

Implements the canonical error envelope and the class-time catalog
guard every concrete subclass relies on. See the
[error model](../explanation/error-model.md) for the contract.

`__init_subclass__` validates at class-definition time that the
declared `code` is registered in `CATALOG` and that `level` agrees
with the catalog entry; drift fails at import, not at runtime.
`to_dict` walks `__cause__` recursively, wrapping any non-platform
cause in a `WrappedError` envelope so serialized output stays
canonical. Concrete subclasses self-register in a module-level list so
test and inspection helpers can iterate without scanning
`__subclasses__` (O(N), order-undefined).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from errors.catalog import CATALOG
from errors.level import Level

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# Module-private registry of every concrete PlatformError subclass that
# has been defined (i.e. imported). `iter_error_classes()` is the public
# read view. The base class itself is intentionally omitted — it carries
# the abstract default code "platform.unknown" which is not catalogued.
_REGISTERED_SUBCLASSES: list[type[PlatformError]] = []


class PlatformError(Exception):
    """Base class for every error raised across module boundaries.

    Subclasses set `code` (a catalogued string registered in
    `CATALOG`) and `level` (the catalogue's assigned `Level`);
    `__init_subclass__` enforces both at class-definition time.

    The constructor signature is the canonical one:

    ```python
    raise SecretNotFound(name="MY_KEY")
    # internally:
    super().__init__(
        message="Secret 'MY_KEY' is not available",
        where="secrets.dotenv.resolve",
        detail={"name": "MY_KEY"},
    )
    ```

    Subclasses typically expose a domain-shaped constructor and call
    `super().__init__` with the canonical message and populated detail.
    """

    code: ClassVar[str] = "platform.unknown"
    level: ClassVar[Level] = Level.FATAL

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Validate the subclass `code` and `level` against the error catalog."""
        super().__init_subclass__(**kwargs)
        # Validate against the catalog at class-definition time so
        # drift between code and catalog surfaces at import, not as a
        # deferred test failure.
        if cls.code not in CATALOG:
            msg = (
                f"{cls.__name__}.code={cls.code!r} is not registered in the "
                f"error catalog. Add it to errors/catalog.py (Part 4) or "
                f"remove this subclass."
            )
            raise TypeError(msg)
        expected_level = CATALOG[cls.code]
        if cls.level != expected_level:
            msg = (
                f"{cls.__name__}.level={cls.level!r} disagrees with catalog "
                f"entry {expected_level!r} for code {cls.code!r}."
            )
            raise TypeError(msg)
        _REGISTERED_SUBCLASSES.append(cls)

    def __init__(
        self,
        message: str,
        *,
        where: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if not where:
            # Empty `where` is a contract violation.
            msg = "PlatformError.where must be a non-empty string"
            raise ValueError(msg)
        super().__init__(message)
        self.where = where
        self.detail: dict[str, Any] = detail if detail is not None else {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the canonical Error envelope.

        The `cause` field is recursive: a `PlatformError` cause nests
        its own envelope; a non-platform cause is wrapped via
        `WrappedError.from_exception` so the serialized output never
        contains raw stdlib or third-party exception data.
        """
        return {
            "type": self.code,
            "message": str(self),
            "where": self.where,
            "detail": self.detail,
            "cause": _serialize_cause(self.__cause__, fallback_where=self.where),
        }


class WrappedError(PlatformError):
    """Generic wrapper for non-platform exceptions.

    Default level is Diagnostic. When a wrapped exception must surface
    at Fatal severity — the engine-boundary safety net —
    [`PlatformInternalError`][errors.engine.PlatformInternalError] is
    the right tool instead.
    """

    code = "platform.wrapped"
    level = Level.DIAGNOSTIC

    @classmethod
    def from_exception(cls, exc: BaseException, where: str) -> WrappedError:
        """Wrap an arbitrary exception in the canonical envelope.

        The original exception's type and args are captured in
        `detail`; the in-process object is preserved on `__cause__`
        for callers that want to inspect it directly.

        Args:
            exc: The exception to wrap.
            where: Dotted-string locator of the call site doing the
                wrapping.

        Returns:
            A `WrappedError` whose `detail` carries the original
            exception's type and args.
        """
        return cls(
            message=f"{type(exc).__name__}: {exc}",
            where=where,
            detail={
                "original_type": f"{type(exc).__module__}.{type(exc).__name__}",
                "args": list(exc.args),
            },
        )


def _serialize_cause(
    cause: BaseException | None,
    *,
    fallback_where: str,
) -> dict[str, Any] | None:
    """Render `cause` as a nested envelope, wrapping non-platform ones.

    Lives outside `PlatformError.to_dict` so the recursion is explicit
    and free of partial-construction state.
    """
    if cause is None:
        return None
    if isinstance(cause, PlatformError):
        return cause.to_dict()
    # Non-platform — wrap so the serialized output stays canonical.
    return WrappedError.from_exception(cause, where=fallback_where).to_dict()


def iter_error_classes() -> Iterator[type[PlatformError]]:
    """Yield every concrete `PlatformError` subclass that has been imported.

    The base class itself is excluded. Order is registration order —
    the order in which the subclass modules were imported. The return
    value is a snapshot iterator so callers can safely consume during
    imports that would extend the registry.
    """
    return iter(list(_REGISTERED_SUBCLASSES))


def registered_subclasses() -> Iterable[type[PlatformError]]:
    """Return a read-only view of the registered subclasses."""
    return tuple(_REGISTERED_SUBCLASSES)

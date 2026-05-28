"""PlatformError base + WrappedError + iteration helpers — spec §3.1-3.4.

Design notes:

- Every concrete subclass declares ``code`` and ``level`` as class
  attributes; the catalog (Part 4) is the registry. ``__init_subclass__``
  validates at definition time that ``code`` is catalogued and that
  ``level`` agrees with the catalog entry. Drift fails at import, not
  at runtime (E-I2, E-I3).
- ``to_dict`` walks ``__cause__`` recursively; a non-platform cause is
  itself wrapped in a ``WrappedError`` envelope (E-I5).
- Concrete subclasses register themselves in a module-level list so
  test/inspection helpers can iterate without scanning ``__subclasses__``
  (which is O(N) and order-undefined).
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

    Subclasses MUST set ``code`` (a catalogued string from §4) and
    ``level`` (the catalogue's assigned :class:`Level`). The
    ``__init_subclass__`` hook enforces both.

    The constructor signature is the canonical one used by the spec
    example (§3.1):

    .. code-block:: python

        raise SecretNotFound(name="MY_KEY")
        # internally:
        super().__init__(
            message="Secret 'MY_KEY' is not available",
            where="secrets.dotenv.resolve",
            detail={"name": "MY_KEY"},
        )

    Subclasses typically expose a domain-shaped constructor and call
    ``super().__init__`` with the canonical message + populated detail.
    """

    code: ClassVar[str] = "platform.unknown"
    level: ClassVar[Level] = Level.FATAL

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Validate against the catalog (E-I2, E-I3). The check runs at
        # class-definition time so drift between code and catalog
        # surfaces at import, not as a deferred test failure.
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
            # E-I6 — empty `where` is a contract violation.
            msg = "PlatformError.where must be a non-empty string"
            raise ValueError(msg)
        super().__init__(message)
        self.where = where
        self.detail: dict[str, Any] = detail if detail is not None else {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the canonical Error envelope (spec §2.1).

        The ``cause`` field is recursive: a ``PlatformError`` cause
        nests its own envelope; a non-platform cause is wrapped via
        :meth:`WrappedError.from_exception` so the serialized output
        never contains raw stdlib/third-party exception data (E-I5).
        """
        return {
            "type": self.code,
            "message": str(self),
            "where": self.where,
            "detail": self.detail,
            "cause": _serialize_cause(self.__cause__, fallback_where=self.where),
        }


class WrappedError(PlatformError):
    """Generic wrapper for non-platform exceptions — spec §3.4.

    Default level is :attr:`Level.DIAGNOSTIC` per the spec reference
    implementation. When a wrapped exception must surface at Fatal
    severity (the engine-boundary safety net), raise
    :class:`PlatformInternalError` instead.
    """

    code = "platform.wrapped"
    level = Level.DIAGNOSTIC

    @classmethod
    def from_exception(cls, exc: BaseException, where: str) -> WrappedError:
        """Wrap an arbitrary exception in the canonical envelope.

        The original exception's type and args are captured in
        ``detail``; the caller may still inspect ``__cause__`` for the
        in-process object if needed.
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

    Lives outside :meth:`PlatformError.to_dict` so the recursion is
    explicit and free of partial-construction state.
    """
    if cause is None:
        return None
    if isinstance(cause, PlatformError):
        return cause.to_dict()
    # Non-platform — wrap so the serialized output stays canonical.
    return WrappedError.from_exception(cause, where=fallback_where).to_dict()


def iter_error_classes() -> Iterator[type[PlatformError]]:
    """Yield every concrete PlatformError subclass that has been imported.

    The base class itself is excluded. Order is registration order
    (i.e. the order in which the subclass modules were imported).
    """
    # Return a snapshot iterator so callers can safely consume during
    # imports that would extend the registry.
    return iter(list(_REGISTERED_SUBCLASSES))


def registered_subclasses() -> Iterable[type[PlatformError]]:
    """Return a read-only view of the registered subclasses."""
    return tuple(_REGISTERED_SUBCLASSES)

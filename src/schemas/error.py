"""The canonical Error envelope shared across modules.

A Pydantic mirror of what
[`PlatformError.to_dict`][errors.PlatformError.to_dict] emits. Use
this model whenever an error appears as event data (`tool.failed.
data.error`, `thread.exchange_failed.data.error`,
`model.response.data.error`) so the consumer has a typed handle on
`type / message / where / detail / cause`.

The `cause` field is self-referential; Pydantic v2 handles the
forward reference automatically when the class definition completes.

See the [error model](../explanation/error-model.md) for the
full classification contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from errors import PlatformError


class Error(BaseModel):
    """The canonical error envelope shared across modules.

    Field semantics match
    [`PlatformError`][errors.PlatformError]'s `to_dict` output so
    `from_platform_error` is a structural one-shot: take what
    `PlatformError` emits, hand it to Pydantic, get a typed object
    back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    message: str
    where: str
    detail: dict[str, Any] = Field(default_factory=dict)
    cause: Error | None = None

    @classmethod
    def from_platform_error(cls, error: PlatformError) -> Error:
        """Build a typed `Error` from a raised `PlatformError`.

        Equivalent to `Error.model_validate(error.to_dict())` but
        named so callers don't have to remember the round-trip.
        """
        return cls.model_validate(error.to_dict())

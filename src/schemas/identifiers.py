"""Prefixed-UUID identifier types — spec §13.1 + §A.3.

Each identifier carries a fixed prefix and a body:

- ``ses_<uuid>`` — session
- ``th_<uuid>`` — thread
- ``ev_<uuid>`` — event
- ``mem_<uuid>`` — memory item
- ``cl_<uuid>`` — agent call
- ``ag_<user-chosen-id>`` — agent (body is user-chosen, validated as
  identifier-like rather than a UUID)
- ``SubscriptionId`` — opaque, no fixed prefix (Module API Reference
  §4.2 declares it as a plain ``NewType``)

The :class:`PrefixedId` base subclasses ``str`` so the resulting types
work transparently as dict keys, JSON serialize naturally, and pass
``isinstance(x, str)`` checks. Pydantic v2 wires the same constructor
into field validation via ``__get_pydantic_core_schema__``, so the
prefix check runs whether the id is built directly in code or
deserialized from JSON.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, ClassVar

from pydantic_core import CoreSchema, core_schema

if TYPE_CHECKING:
    from typing import Any

    from pydantic import GetCoreSchemaHandler


_AGENT_ID_BODY = re.compile(r"^[a-zA-Z0-9_\-.]+$")


class PrefixedId(str):
    """Base for every identifier with a structural prefix.

    Subclasses set :attr:`prefix` (the literal prefix including the
    trailing underscore). Set :attr:`body_must_be_uuid` to ``True`` for
    UUID-suffixed ids, or override :meth:`_validate_body` for custom
    body checks (see :class:`AgentId`).
    """

    prefix: ClassVar[str] = ""
    body_must_be_uuid: ClassVar[bool] = False

    def __new__(cls, value: str) -> PrefixedId:
        # Note: callers pass `str` per the annotation (mypy --strict
        # enforces this; Pydantic's `str_schema()` validates the JSON
        # case before this runs). No defensive isinstance check is
        # needed — and adding one is dead code per the type system.
        if not value.startswith(cls.prefix):
            msg = f"{cls.__name__} requires prefix {cls.prefix!r}; got {value!r}"
            raise ValueError(msg)
        body = value[len(cls.prefix) :]
        if not body:
            msg = f"{cls.__name__} {value!r} has empty body after prefix {cls.prefix!r}"
            raise ValueError(msg)
        cls._validate_body(body, value)
        return super().__new__(cls, value)

    @classmethod
    def _validate_body(cls, body: str, value: str) -> None:
        """Hook for subclass-specific body validation.

        Default behavior validates a UUID when
        :attr:`body_must_be_uuid` is True; otherwise no body check.
        """
        if cls.body_must_be_uuid:
            try:
                uuid.UUID(body)
            except ValueError as exc:
                msg = f"{cls.__name__} body must be a uuid; {value!r} failed: {exc}"
                raise ValueError(msg) from exc

    # --- Pydantic v2 integration -----------------------------------------
    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,  # noqa: ANN401 — Pydantic protocol takes Any
        _handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        # Validate the raw string then call cls(...) so the prefix and
        # body checks run identically for code-built and
        # JSON-deserialized values.
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
        )


class SessionId(PrefixedId):
    """`ses_<uuid>` — a session."""

    prefix = "ses_"
    body_must_be_uuid = True


class ThreadId(PrefixedId):
    """`th_<uuid>` — a thread."""

    prefix = "th_"
    body_must_be_uuid = True


class EventId(PrefixedId):
    """`ev_<uuid>` — an event."""

    prefix = "ev_"
    body_must_be_uuid = True


class MemoryItemId(PrefixedId):
    """`mem_<uuid>` — a memory item."""

    prefix = "mem_"
    body_must_be_uuid = True


class AgentCallId(PrefixedId):
    """`cl_<uuid>` — one model-call invocation inside an exchange."""

    prefix = "cl_"
    body_must_be_uuid = True


class AgentId(PrefixedId):
    """`ag_<user-chosen-id>` — an agent identity.

    The body is user-chosen (spec §A.3), so it is NOT a UUID. The body
    must be a non-empty identifier-like string: ASCII letters, digits,
    underscore, hyphen, or dot.
    """

    prefix = "ag_"

    @classmethod
    def _validate_body(cls, body: str, value: str) -> None:
        if not _AGENT_ID_BODY.fullmatch(body):
            msg = (
                f"AgentId body {body!r} (in {value!r}) contains invalid "
                f"characters; allowed: ASCII letters, digits, _, -, ."
            )
            raise ValueError(msg)


class SubscriptionId(PrefixedId):
    """An event-bus subscription handle.

    Module API Reference §4.2 declares this as a plain ``NewType("...",
    str)`` — opaque to consumers, no fixed prefix. Inheriting
    :class:`PrefixedId` keeps the Pydantic integration and the
    str-subclass ergonomics; the empty prefix means any non-empty
    string validates. (The base class rejects empty bodies.)
    """

    prefix = ""

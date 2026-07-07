"""Prefixed-UUID identifier types for the platform's persistent records.

Each identifier carries a short prefix plus a body so it's recognisable
at a glance in logs, JSONL files, and error messages. The supported
shapes:

- `ses_<uuid>` — session
- `th_<uuid>` — thread
- `ev_<uuid>` — event
- `mem_<uuid>` — memory item
- `cl_<uuid>` — agent call
- `ag_<user-chosen-id>` — agent (body is a user-chosen identifier-like
  string, not a UUID)
- [`SubscriptionId`][schemas.identifiers.SubscriptionId] — an event-bus
  handle with no fixed prefix

[`PrefixedId`][schemas.identifiers.PrefixedId] subclasses `str` so each
typed id works transparently as a dict key, serialises to JSON without
a wrapper, and passes `isinstance(x, str)` checks. Pydantic v2 calls
the same constructor during field validation, so the prefix and body
checks run whether the id is built in code or parsed from JSON.

For the narrative on how each id is produced, who owns it, and which
records reference it, see the
[Schemas architecture page](../architecture/schemas.md).
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

    Subclasses declare `prefix` (the literal prefix string, including
    the trailing underscore). For UUID-suffixed ids, set
    `body_must_be_uuid = True` and the base validator checks the body.
    For custom body rules, override `_validate_body` instead —
    [`AgentId`][schemas.identifiers.AgentId] does this to accept a
    user-chosen identifier rather than a UUID.

    Instances are real `str` values, so they compare, hash, and
    JSON-serialise like any other string. Pydantic v2 wires the
    constructor via `__get_pydantic_core_schema__`, so validation runs
    identically whether an id is built in code or deserialised.
    """

    prefix: ClassVar[str] = ""
    body_must_be_uuid: ClassVar[bool] = False

    def __new__(cls, value: str) -> PrefixedId:
        """Validate `value` against the subclass's prefix and body rule."""
        # Callers pass `str` per the annotation (mypy --strict enforces
        # this; Pydantic's `str_schema()` validates the JSON case before
        # this runs). No defensive isinstance check is needed — and
        # adding one is dead code per the type system.
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
        """Validate the body of a prefixed id.

        The default checks for a UUID when `body_must_be_uuid` is set
        and is a no-op otherwise. Subclasses override this to apply
        their own body shape — see
        [`AgentId._validate_body`][schemas.identifiers.AgentId._validate_body].

        Args:
            body: The part of the id after the prefix.
            value: The full id string. Used in error messages so the
                caller sees what they passed.

        Raises:
            ValueError: The body does not satisfy the subclass's rule.
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
        """Wire `cls(...)` into Pydantic v2 field validation.

        Pydantic validates the raw value as a string first, then calls
        `cls(...)` — so the prefix and body checks run identically for
        ids built in code and ids deserialised from JSON.
        """
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
        )


class SessionId(PrefixedId):
    """`ses_<uuid>` — identifies a session (one conversation instance)."""

    prefix = "ses_"
    body_must_be_uuid = True


class ThreadId(PrefixedId):
    """`th_<uuid>` — identifies a thread within a session."""

    prefix = "th_"
    body_must_be_uuid = True


class EventId(PrefixedId):
    """`ev_<uuid>` — identifies one immutable event on the JSONL log."""

    prefix = "ev_"
    body_must_be_uuid = True


class MemoryItemId(PrefixedId):
    """`mem_<uuid>` — identifies one item the memory provider holds."""

    prefix = "mem_"
    body_must_be_uuid = True


class PromptId(PrefixedId):
    """`prm_<uuid>` — identifies one prompt request.

    Emitted by [`PromptsImpl`][cli.chat.services.prompts_impl.PromptsImpl]
    when it fires a `kolsch.prompt.requested` UI event; the
    matching `.answered` / `.cancelled` events carry the same id so
    the impl's pending-future can be resolved by the
    [`EventAdapter`][cli.chat.event_adapter.EventAdapter] dispatch
    path.
    """

    prefix = "prm_"
    body_must_be_uuid = True


def new_prompt_id() -> PromptId:
    """Build a fresh [`PromptId`][schemas.identifiers.PromptId].

    Wraps a uuid4 in the `prm_` prefix; used at every
    `PromptsImpl.choice` / `.text` call site so each prompt event
    carries a unique id downstream consumers can dedupe on.
    """
    return PromptId(f"prm_{uuid.uuid4()}")


class AgentCallId(PrefixedId):
    """`cl_<uuid>` — identifies one model call inside an exchange.

    An exchange may make several model calls (an initial call, plus
    one per tool-use round); each call gets its own id so the event
    log can attribute every event to the specific call that produced
    it.
    """

    prefix = "cl_"
    body_must_be_uuid = True


class AgentId(PrefixedId):
    """`ag_<user-chosen-id>` — identifies an agent.

    The body is whatever the agent's author chose, not a UUID. The
    only constraint is that it reads as an identifier — ASCII letters,
    digits, underscore, hyphen, or dot — so the id stays safe in
    paths, URLs, and JSONL records.
    """

    prefix = "ag_"

    @classmethod
    def _validate_body(cls, body: str, value: str) -> None:
        """Check that `body` is identifier-like, not a UUID."""
        if not _AGENT_ID_BODY.fullmatch(body):
            msg = (
                f"AgentId body {body!r} (in {value!r}) contains invalid "
                f"characters; allowed: ASCII letters, digits, _, -, ."
            )
            raise ValueError(msg)


def derive_agent_id(bundle: str, name: str) -> AgentId:
    """Derive an [`AgentId`][schemas.identifiers.AgentId] from a bundle + asset name.

    Used by the asset manager when an agent YAML file omits its
    `agent_id` field — the resolved config still carries a canonical
    id derived from where the file lives.

    Args:
        bundle: The bundle the agent lives in. Bundle names are already
            lowercase alphanumeric plus `-`/`_` (enforced by the
            manifest), so they always satisfy the body class.
        name: The agent's asset name (typically the YAML file stem).

    Returns:
        An `AgentId` of the shape `ag_<bundle>_<name>`.

    Raises:
        ValueError: `name` contains characters outside the body class
            (anything other than ASCII letters, digits, `_`, `-`,
            `.`). The raise surfaces the bad input rather than
            producing a malformed id.
    """
    return AgentId(f"ag_{bundle}_{name}")


class SubscriptionId(PrefixedId):
    """An event-bus subscription handle.

    Declared as an opaque token: no fixed prefix, no required shape
    beyond non-empty. Inheriting
    [`PrefixedId`][schemas.identifiers.PrefixedId] keeps the Pydantic
    integration and the `str`-subclass ergonomics; the empty prefix
    means any non-empty string validates. (The base class still
    rejects empty bodies.)
    """

    prefix = ""

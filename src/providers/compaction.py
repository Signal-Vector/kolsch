"""Context compaction provider — modular, platform-config-driven.

Compaction shortens the projected message list an agent sends to the
LLM when a session approaches the model's context window. The provider
never rewrites the on-disk event log; it emits a
[`ContextCompactedData`][schemas.events.ContextCompactedData] payload
that Assembly consumes when projecting later exchanges.

This module owns the Protocol (:class:`ContextCompactionProvider`),
the v1 variant (:class:`ReferenceSummaryCompactionProvider`), and a
tiny registry seam mirroring
[`providers.memory`][providers.memory] and
[`providers.secrets`][providers.secrets] so future variants slot in
without touching bootstrap.

Section-absent semantics: when `platform.yaml` has no `compaction:`
section, :func:`make_compaction_provider` returns `None` and the
engine skips the trigger check entirely. This is the documented "off"
mode — matches the `author_workflow` precedent in
:mod:`assets.platform_config`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from errors import ConfigInvalid
from kernel import ValidationError, ValidationResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from engine import LLMRequest, LLMResponse
    from schemas import ModelSpec, ThreadId
    from schemas.events import ContextCompactedData, Event

_log = logging.getLogger("providers.compaction")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ContextCompactionProvider(Protocol):
    """The contract every compaction variant honours.

    Implementations receive the full assembled-event list plus the
    boundaries the engine has already computed (`keep_head` /
    `keep_tail`) and return a
    [`ContextCompactedData`][schemas.events.ContextCompactedData]
    payload. The engine emits the `context.compacted` event and
    Assembly folds it into the next projection.

    `keep_head` is 0 in v1 because kolsch's system prompt is derived at
    Assembly time (not stored on the event log) and therefore never
    part of `events`. The parameter is present so future variants that
    want to pin the first N events verbatim have a place to plug in.
    """

    async def compact(
        self,
        *,
        events: list[Event],
        keep_head: int,
        keep_tail: int,
        model_context_length: int,
        thread_id: ThreadId,
        config: dict[str, Any],
    ) -> ContextCompactedData:
        """Produce a compaction payload for `events`.

        Args:
            events: The full projected event list for the exchange.
            keep_head: Head events to preserve verbatim (0 in v1).
            keep_tail: Recent events to preserve verbatim.
            model_context_length: The model's declared context window
                in tokens; the provider budgets `refs`/`summary` size
                against this.
            thread_id: The thread whose context is being compacted.
            config: Variant-specific args from `platform.yaml`.
        """
        ...


# ---------------------------------------------------------------------------
# v1 variant — reference_summary
# ---------------------------------------------------------------------------


class ReferenceSummaryCompactionProvider:
    """The v1 `reference_summary` variant.

    Produces a JSON summary with `event_id` refs pointing back into
    the session log; downstream turns can retrieve originals via the
    `context.reference_lookup` tool. Phase 1 ships the constructor +
    registration only — the summariser algorithm lands in Phase 2.

    The `llm_for_model` factory is stored for Phase 2's summariser
    LLM call (the variant may reuse the exchange's model, or a
    configured `summarizer_model` — see `config`).
    """

    def __init__(
        self,
        *,
        llm_for_model: (
            Callable[
                [ModelSpec],
                Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]],
            ]
            | None
        ),
        config: dict[str, Any],
    ) -> None:
        self._llm_for_model = llm_for_model
        self._config = dict(config)

    async def compact(
        self,
        *,
        events: list[Event],
        keep_head: int,
        keep_tail: int,
        model_context_length: int,
        thread_id: ThreadId,
        config: dict[str, Any],
    ) -> ContextCompactedData:
        """Phase 1 stub — the real algorithm lands in Phase 2.

        Kept as an explicit `NotImplementedError` so any caller that
        invokes the provider before Phase 2 lands surfaces a clear
        signal rather than silently returning garbage.
        """
        raise NotImplementedError(
            "ReferenceSummaryCompactionProvider.compact is a Phase 1 stub; "
            "the summariser algorithm lands in Phase 2 of feat-054."
        )


# ---------------------------------------------------------------------------
# Layer 3 validator (kernel ConfigValidator signature)
# ---------------------------------------------------------------------------


def validate_reference_summary_config(
    body: dict[str, Any],
) -> ValidationResult:
    """Validate a `reference_summary` config body at Layer 3.

    Mirrors :func:`providers.memory.validate_recent_buffer_config`.
    Checks:

    - `trigger_at` (the platform-level threshold, but the whole
      compaction dict is passed to the validator per convention) must
      be a float in `(0, 1]` if present.
    - `config.keep_recent_fraction` must be a float in `(0, 1]` if
      present.
    - `config.max_refs` must be an int >= 1 if present.
    - `config.summarizer_model` must be a mapping with `provider` and
      `model` keys if present.

    Fields absent from `body` are permitted — the provider fills
    defaults at construction time.
    """
    errors: list[ValidationError] = []

    trigger_at = body.get("trigger_at")
    if trigger_at is not None:
        if isinstance(trigger_at, bool) or not isinstance(trigger_at, (int, float)):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/trigger_at",
                    code="not_a_number",
                    message=(
                        f"trigger_at must be a number in (0, 1]; "
                        f"got {type(trigger_at).__name__}"
                    ),
                )
            )
        elif not (0 < float(trigger_at) <= 1):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/trigger_at",
                    code="out_of_range",
                    message=(
                        f"trigger_at must be in (0, 1]; got {trigger_at}"
                    ),
                )
            )

    inner = body.get("config") or {}
    if not isinstance(inner, dict):
        errors.append(
            ValidationError(
                layer=3,
                source="compaction.reference_summary",
                path="/config",
                code="not_a_mapping",
                message=(
                    f"config must be a mapping; got {type(inner).__name__}"
                ),
            )
        )
        return ValidationResult(ok=not errors, errors=errors)

    keep_recent = inner.get("keep_recent_fraction")
    if keep_recent is not None:
        if isinstance(keep_recent, bool) or not isinstance(keep_recent, (int, float)):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/keep_recent_fraction",
                    code="not_a_number",
                    message=(
                        f"keep_recent_fraction must be a number in (0, 1]; "
                        f"got {type(keep_recent).__name__}"
                    ),
                )
            )
        elif not (0 < float(keep_recent) <= 1):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/keep_recent_fraction",
                    code="out_of_range",
                    message=(
                        f"keep_recent_fraction must be in (0, 1]; "
                        f"got {keep_recent}"
                    ),
                )
            )

    max_refs = inner.get("max_refs")
    if max_refs is not None:
        if isinstance(max_refs, bool) or not isinstance(max_refs, int):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/max_refs",
                    code="not_an_int",
                    message=(
                        f"max_refs must be an integer; got {type(max_refs).__name__}"
                    ),
                )
            )
        elif max_refs < 1:
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/max_refs",
                    code="out_of_range",
                    message=(f"max_refs must be >= 1; got {max_refs}"),
                )
            )

    summarizer = inner.get("summarizer_model")
    if summarizer is not None:
        if not isinstance(summarizer, dict):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/summarizer_model",
                    code="not_a_mapping",
                    message=(
                        f"summarizer_model must be a mapping; "
                        f"got {type(summarizer).__name__}"
                    ),
                )
            )
        else:
            for required in ("provider", "model"):
                value = summarizer.get(required)
                if not isinstance(value, str) or not value:
                    errors.append(
                        ValidationError(
                            layer=3,
                            source="compaction.reference_summary",
                            path=f"/config/summarizer_model/{required}",
                            code="missing_or_not_a_string",
                            message=(
                                f"summarizer_model.{required} must be a "
                                f"non-empty string"
                            ),
                        )
                    )

    return ValidationResult(ok=not errors, errors=errors)


# ---------------------------------------------------------------------------
# Registry seam
# ---------------------------------------------------------------------------


CompactionFactory = "Callable[..., ContextCompactionProvider]"
_REGISTRY: dict[str, Callable[..., ContextCompactionProvider]] = {}


def register_compaction_variant(
    name: str,
    factory: Callable[..., ContextCompactionProvider],
) -> None:
    """Register a variant factory under `name`. Idempotent on same input."""
    _REGISTRY[name] = factory


def make_compaction_provider(
    *,
    config: dict[str, Any],
    llm_for_model: (
        Callable[
            [ModelSpec],
            Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]],
        ]
        | None
    ),
) -> ContextCompactionProvider | None:
    """Dispatch by `config['variant']` — silent-disable on empty config.

    `config` is the whole `compaction:` block from `platform.yaml`
    (e.g. `{"variant": "reference_summary", "trigger_at": 0.8,
    "config": {...}}`). When the block is missing or empty, returns
    `None` — the engine treats `None` as "compaction disabled" and
    skips the trigger check.

    Raises:
        ConfigInvalid: `variant` is set but names no registered factory.
    """
    if not config:
        return None
    variant = config.get("variant")
    if variant is None:
        return None
    body = config.get("config", {}) or {}
    factory = _REGISTRY.get(variant)
    if factory is None:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 2,
                    "source": "providers.compaction",
                    "path": "/compaction/variant",
                    "code": "unknown_variant",
                    "message": (
                        f"unknown compaction variant {variant!r}; "
                        f"registered: {sorted(_REGISTRY)}"
                    ),
                }
            ],
            where="providers.compaction.make_compaction_provider",
        )
    return factory(llm_for_model=llm_for_model, config=body)


# Self-register the v1 variant at import time.
register_compaction_variant("reference_summary", ReferenceSummaryCompactionProvider)

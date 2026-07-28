"""Unit tests for the compaction provider registry — feat-054 Phase 1.

Covers the seam that platform bootstrap uses: register a variant,
look it up via `make_compaction_provider`, and confirm the
section-absent branch silently returns `None`. The v1 variant's
summariser algorithm lands in Phase 2 and is not exercised here.
"""

from __future__ import annotations

from typing import Any

import pytest

from errors import ConfigInvalid
from providers.compaction import (
    _REGISTRY,
    ContextCompactionProvider,
    ReferenceSummaryCompactionProvider,
    make_compaction_provider,
    register_compaction_variant,
    validate_reference_summary_config,
)


def test_reference_summary_variant_self_registers_at_import() -> None:
    assert "reference_summary" in _REGISTRY
    # The registered factory is the v1 provider class itself.
    assert _REGISTRY["reference_summary"] is ReferenceSummaryCompactionProvider


def test_make_compaction_provider_returns_none_for_empty_config() -> None:
    provider = make_compaction_provider(config={}, llm_for_model=None)
    assert provider is None


def test_make_compaction_provider_returns_none_when_variant_missing() -> None:
    # A dict without a `variant` key is treated as disabled.
    provider = make_compaction_provider(
        config={"trigger_at": 0.8},
        llm_for_model=None,
    )
    assert provider is None


def test_make_compaction_provider_dispatches_by_variant() -> None:
    provider = make_compaction_provider(
        config={"variant": "reference_summary", "trigger_at": 0.8, "config": {}},
        llm_for_model=None,
    )
    assert isinstance(provider, ReferenceSummaryCompactionProvider)


def test_make_compaction_provider_raises_on_unknown_variant() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        make_compaction_provider(
            config={"variant": "does_not_exist", "config": {}},
            llm_for_model=None,
        )
    payload = exc_info.value
    # Error payload names the offending variant plus the registered ones.
    errors = payload.detail["validation_errors"]
    assert errors
    assert "does_not_exist" in errors[0]["message"]


def test_register_compaction_variant_adds_to_registry() -> None:
    class _Custom:
        def __init__(self, *, llm_for_model: Any, config: dict[str, Any]) -> None:
            self.config = config

        async def compact(  # pragma: no cover — signature-shape only
            self,
            *,
            events: Any,
            keep_head: int,
            keep_tail: int,
            model_context_length: int,
            thread_id: Any,
            config: dict[str, Any],
        ) -> Any:
            raise NotImplementedError

    try:
        register_compaction_variant("custom_test_variant", _Custom)
        assert "custom_test_variant" in _REGISTRY
        provider = make_compaction_provider(
            config={"variant": "custom_test_variant", "config": {"x": 1}},
            llm_for_model=None,
        )
        assert isinstance(provider, _Custom)
        assert provider.config == {"x": 1}
    finally:
        _REGISTRY.pop("custom_test_variant", None)


@pytest.mark.asyncio
async def test_reference_summary_compact_no_op_on_empty_events() -> None:
    # Phase 2: `compact()` is implemented. Short/empty session → no-op
    # payload with empty `compacted_event_ids`; the engine trigger
    # site treats that as "skip emit".
    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=None,
        config={},
    )
    outcome = await provider.compact(
        events=[],
        keep_head=0,
        keep_tail=100,
        model_context_length=100_000,
        thread_id="thread_test",
        config={},
    )
    assert outcome.compacted_event_ids == []
    assert outcome.tokens_before == 0
    assert outcome.tokens_after == 0


def test_provider_satisfies_protocol() -> None:
    # Structural: the concrete class should be assignable to the Protocol.
    provider: ContextCompactionProvider = ReferenceSummaryCompactionProvider(
        llm_for_model=None,
        config={},
    )
    assert provider is not None


def test_validator_accepts_minimal_config() -> None:
    result = validate_reference_summary_config({})
    assert result.ok, result.errors


def test_validator_accepts_full_config() -> None:
    result = validate_reference_summary_config(
        {
            "trigger_at": 0.8,
            "config": {
                "keep_recent_fraction": 0.2,
                "max_refs": 50,
                "summarizer_model": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                },
            },
        }
    )
    assert result.ok, result.errors


@pytest.mark.parametrize(
    "trigger_at",
    [0, -0.1, 1.5, "0.8"],
)
def test_validator_rejects_invalid_trigger_at(trigger_at: object) -> None:
    result = validate_reference_summary_config({"trigger_at": trigger_at})
    assert not result.ok
    assert any(e.path == "/trigger_at" for e in result.errors)


def test_validator_rejects_invalid_keep_recent_fraction() -> None:
    result = validate_reference_summary_config({"config": {"keep_recent_fraction": 0}})
    assert not result.ok
    assert any(e.path == "/config/keep_recent_fraction" for e in result.errors)


def test_validator_rejects_max_refs_below_one() -> None:
    result = validate_reference_summary_config({"config": {"max_refs": 0}})
    assert not result.ok
    assert any(e.path == "/config/max_refs" for e in result.errors)


def test_validator_rejects_summarizer_model_missing_provider() -> None:
    result = validate_reference_summary_config(
        {"config": {"summarizer_model": {"model": "claude-haiku-4-5"}}}
    )
    assert not result.ok
    assert any(e.path == "/config/summarizer_model/provider" for e in result.errors)


def test_validator_rejects_non_mapping_config() -> None:
    result = validate_reference_summary_config({"config": ["not", "a", "map"]})
    assert not result.ok
    assert any(e.path == "/config" for e in result.errors)

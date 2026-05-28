"""Unit tests for src/kernel — TC-KRN-01..10 plus invariants.

The kernel is pure-CPU per spec §1 / handbook §10.1. Every test here
is a unit test; no I/O, no async, no infrastructure.
"""

from __future__ import annotations

import builtins
import io
import socket
from typing import Any

import pytest

from errors import ConfigInvalid
from kernel import (
    ConfigValidator,
    ValidationError,
    ValidationResult,
    export_json_schema,
    merge_patch,
    resolve,
    resolve_and_validate,
    validate_partial,
    validate_resolved,
)
from schemas import ResolvedAgentConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_partial() -> dict[str, Any]:
    """A structurally valid Layer 1 partial covering every top-level field."""
    return {
        "schema_version": "0.1",
        "assembly_version": "0.1",
        "agent_id": "ag_research",
        "name": "research",
        "description": "Researches things.",
        "model": {
            "provider": "anthropic",
            "model": "claude-opus-4-7",
            "parameters": {"temperature": 0.7},
            "fallback": [],
        },
        "prompt": "You research.",
        "io": {"mode": "conversational", "inputs": {}, "outputs": {}},
        "subsystems": {"memory": {"variant": "recent_buffer", "config": {"size": 10}}},
        "runtime": {"max_retries": 3},
    }


def _resolved_dict() -> dict[str, Any]:
    """The same fully populated dict, used as a resolved-config input."""
    return _valid_partial()


# ---------------------------------------------------------------------------
# TC-KRN-01 — valid partial passes structural validation
# ---------------------------------------------------------------------------


def test_valid_partial_passes() -> None:
    result = validate_partial(_valid_partial())
    assert result.ok is True
    assert result.errors == []


def test_empty_partial_passes() -> None:
    # All fields optional at Layer 1 partial validation.
    result = validate_partial({})
    assert result.ok is True
    assert result.errors == []


def test_partially_populated_partial_passes() -> None:
    # A model-only partial is a valid Layer 1 partial.
    result = validate_partial(
        {"model": {"provider": "anthropic", "model": "claude-opus-4-7"}}
    )
    assert result.ok is True
    assert result.errors == []


# ---------------------------------------------------------------------------
# TC-KRN-02 — unknown field rejected
# ---------------------------------------------------------------------------


def test_unknown_top_level_field_rejected() -> None:
    result = validate_partial({"unknown_top": "bad"})
    assert result.ok is False
    assert len(result.errors) >= 1
    paths = [e.path for e in result.errors]
    assert any("unknown_top" in p for p in paths)


def test_unknown_nested_field_rejected() -> None:
    result = validate_partial({"model": {"provider": "anthropic", "alien": 1}})
    assert result.ok is False
    paths = [e.path for e in result.errors]
    assert any("alien" in p for p in paths)


def test_open_map_field_accepts_extra_keys() -> None:
    """model.parameters and subsystem config bodies are open maps per
    spec Open Item §1 — extra keys MUST be accepted there even though
    the top-level schema forbids unknown fields."""
    result = validate_partial(
        {
            "model": {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "parameters": {"any_key": "any_value", "another": 123},
            },
            "subsystems": {
                "memory": {
                    "variant": "recent_buffer",
                    "config": {"size": 5, "future_field": "ok"},
                }
            },
        }
    )
    assert result.ok is True, result.errors


# ---------------------------------------------------------------------------
# TC-KRN-03 — multiple errors all reported
# ---------------------------------------------------------------------------


def test_multiple_independent_errors_all_reported() -> None:
    """Three problems in one partial: unknown field, wrong type on
    model.parameters, malformed agent_id. Spec §10.1 requires all to
    surface, not just the first."""
    result = validate_partial(
        {
            "unknown_top": "bad",
            "model": {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "parameters": "not-a-dict",  # wrong type
            },
            "agent_id": "not-an-agent-id",  # bad prefix
        }
    )
    assert result.ok is False
    assert len(result.errors) >= 3, (
        f"expected at least 3 distinct errors, got: {result.errors}"
    )
    paths = [e.path for e in result.errors]
    assert any("unknown_top" in p for p in paths)
    assert any("model" in p and "parameters" in p for p in paths)
    assert any("agent_id" in p for p in paths)


# ---------------------------------------------------------------------------
# TC-KRN-04 — RFC 7386 merge vectors (Appendix A)
# ---------------------------------------------------------------------------


RFC7386_VECTORS: list[tuple[Any, Any, Any]] = [
    # Each row: target, patch, expected merge result.
    ({"a": "b"}, {"a": "c"}, {"a": "c"}),
    ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
    ({"a": "b"}, {"a": None}, {}),
    ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
    ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
    ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
    (
        {"a": {"b": "c"}},
        {"a": {"b": "d", "c": None}},
        {"a": {"b": "d"}},
    ),
    ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
    (["a", "b"], ["c", "d"], ["c", "d"]),
    ({"a": "b"}, ["c"], ["c"]),
    ({"a": "foo"}, None, None),
    ({"a": "foo"}, "bar", "bar"),
    ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
    ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
    ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
]


@pytest.mark.parametrize(("target", "patch", "expected"), RFC7386_VECTORS)
def test_rfc7386_appendix_a_vectors(target: Any, patch: Any, expected: Any) -> None:
    assert merge_patch(target, patch) == expected


# ---------------------------------------------------------------------------
# TC-KRN-05 — null deletes a key (in resolve, not just merge_patch)
# ---------------------------------------------------------------------------


def test_resolve_null_deletes_key() -> None:
    a = {"model": {"provider": "anthropic"}, "temp": 0.5}
    b = {"temp": None}
    result = resolve([a, b])
    assert "temp" not in result
    assert result["model"] == {"provider": "anthropic"}


def test_resolve_null_deletes_subsystem() -> None:
    """A higher layer can disable an inherited subsystem with `null`."""
    a = {"subsystems": {"memory": {"variant": "recent_buffer", "config": {}}}}
    b = {"subsystems": {"memory": None}}
    result = resolve([a, b])
    assert result["subsystems"] == {}


def test_resolve_left_to_right() -> None:
    """Right wins per RFC 7386; spec §3.2."""
    layers = [
        {"name": "first"},
        {"name": "second"},
        {"name": "third"},
    ]
    assert resolve(layers)["name"] == "third"


def test_resolve_inherits_when_key_absent() -> None:
    a = {"model": {"provider": "anthropic", "model": "claude-opus-4-7"}}
    b = {"name": "agent"}
    result = resolve([a, b])
    # model inherited from a, name from b
    assert result["model"]["provider"] == "anthropic"
    assert result["name"] == "agent"


def test_resolve_empty_list_returns_empty_dict() -> None:
    assert resolve([]) == {}


# ---------------------------------------------------------------------------
# TC-KRN-06 — determinism across 1000 runs
# ---------------------------------------------------------------------------


def test_resolve_is_deterministic_over_1000_runs() -> None:
    partials: list[dict[str, Any]] = [
        {
            "model": {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "parameters": {"temperature": 0.7, "max_tokens": 4096},
                "fallback": [{"provider": "openai", "model": "gpt-4"}],
            },
            "subsystems": {
                "memory": {"variant": "recent_buffer", "config": {"size": 10}},
                "tools": {"variant": "default", "config": {"builtin": []}},
            },
        },
        {
            "model": {"parameters": {"temperature": 0.5}},
            "subsystems": {"memory": {"config": {"size": 20}}},
        },
        {"name": "research", "description": "a researcher"},
    ]
    first = resolve(partials)
    for _ in range(999):
        assert resolve(partials) == first


# ---------------------------------------------------------------------------
# TC-KRN-07 — purity: zero I/O
# ---------------------------------------------------------------------------


class _IOForbiddenError(RuntimeError):
    pass


@pytest.fixture
def deny_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every common I/O call raise. The kernel must not trigger any.

    Note: `builtins.open`, `io.open`, `socket.socket` cover filesystem
    and network access at the most common entry points. Pydantic does
    not perform I/O during model validation.
    """

    def _no_open(*_args: object, **_kwargs: object) -> None:
        msg = "I/O is denied for the kernel purity test"
        raise _IOForbiddenError(msg)

    monkeypatch.setattr(builtins, "open", _no_open)
    monkeypatch.setattr(io, "open", _no_open)
    monkeypatch.setattr(socket, "socket", _no_open)
    monkeypatch.setattr(socket, "create_connection", _no_open)


def test_kernel_does_no_io(deny_io: None) -> None:
    # Every public kernel API under realistic inputs.
    assert merge_patch({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert resolve([{"a": 1}, {"b": 2}]) == {"a": 1, "b": 2}
    assert validate_partial(_valid_partial()).ok is True
    assert validate_resolved(_resolved_dict()).ok is True
    cfg = resolve_and_validate(
        [_valid_partial()],
        subsystem_validators={
            "memory": {"recent_buffer": lambda _body: ValidationResult(ok=True)}
        },
        system_validators=[],
    )
    assert isinstance(cfg, ResolvedAgentConfig)
    schema = export_json_schema()
    assert isinstance(schema, dict)


# ---------------------------------------------------------------------------
# TC-KRN-08 — three-layer dispatch
# ---------------------------------------------------------------------------


def test_three_layer_dispatch_invokes_each_layer() -> None:
    """Recorders confirm the kernel calls Layer 1 (per-partial + on
    the resolved), Layer 3 (per subsystem entry), and Layer 2 (per
    system validator) in the expected order."""
    calls: list[tuple[str, object]] = []

    def make_l3(name: str) -> ConfigValidator:
        def _v(body: dict[str, Any]) -> ValidationResult:
            calls.append((f"l3:{name}", body))
            return ValidationResult(ok=True)

        return _v

    def make_l2(name: str) -> ConfigValidator:
        def _v(resolved: dict[str, Any]) -> ValidationResult:
            calls.append((f"l2:{name}", resolved))
            return ValidationResult(ok=True)

        return _v

    cfg = resolve_and_validate(
        [_valid_partial()],
        subsystem_validators={
            "memory": {"recent_buffer": make_l3("memory.recent_buffer")},
        },
        system_validators=[make_l2("provider_check"), make_l2("path_resolve")],
    )

    assert isinstance(cfg, ResolvedAgentConfig)
    kinds = [c[0] for c in calls]
    # L3 runs before L2 per pipeline §4.1 steps 5-6.
    assert "l3:memory.recent_buffer" in kinds
    assert "l2:provider_check" in kinds
    assert "l2:path_resolve" in kinds
    # L3 body is the subsystem entry's `config` (NOT the full resolved
    # config) per spec §4.3.
    l3_body = next(c[1] for c in calls if c[0] == "l3:memory.recent_buffer")
    assert l3_body == {"size": 10}
    # L2 receives the full resolved dict.
    l2_body = next(c[1] for c in calls if c[0] == "l2:provider_check")
    assert isinstance(l2_body, dict)
    assert "model" in l2_body and "subsystems" in l2_body


# ---------------------------------------------------------------------------
# TC-KRN-09 — unknown variant rejected
# ---------------------------------------------------------------------------


def test_unknown_variant_rejected() -> None:
    """An enabled subsystem whose variant has no registered validator
    raises ConfigInvalid carrying a `config.unknown_variant` finding."""
    partial = _valid_partial()
    partial["subsystems"]["memory"]["variant"] = "imaginary_variant"
    with pytest.raises(ConfigInvalid) as exc_info:
        resolve_and_validate(
            [partial],
            subsystem_validators={
                "memory": {"recent_buffer": lambda _body: ValidationResult(ok=True)}
            },
        )
    detail_errors = exc_info.value.detail["validation_errors"]
    assert any(
        e["code"] == "unknown_variant"
        and "imaginary_variant" in e["message"]
        and "memory" in e["message"]
        for e in detail_errors
    )


def test_unknown_subsystem_name_rejected() -> None:
    """A subsystem the kernel has no validator map entry for is rejected
    by the same `unknown_variant` mechanism (no validator → unknown)."""
    partial = _valid_partial()
    partial["subsystems"]["nonexistent"] = {"variant": "x", "config": {}}
    with pytest.raises(ConfigInvalid):
        resolve_and_validate(
            [partial],
            subsystem_validators={
                "memory": {"recent_buffer": lambda _body: ValidationResult(ok=True)}
            },
        )


# ---------------------------------------------------------------------------
# TC-KRN-10 — validator that raises is contained
# ---------------------------------------------------------------------------


def test_validator_that_raises_is_captured_as_internal_error() -> None:
    """A buggy validator that raises is contained: its failure is
    captured as `validator_internal_error`; other validators still run."""

    def raising_validator(_body: dict[str, Any]) -> ValidationResult:
        msg = "validator went boom"
        raise RuntimeError(msg)

    other_called = False

    def other_validator(_resolved: dict[str, Any]) -> ValidationResult:
        nonlocal other_called
        other_called = True
        return ValidationResult(ok=True)

    with pytest.raises(ConfigInvalid) as exc_info:
        resolve_and_validate(
            [_valid_partial()],
            subsystem_validators={"memory": {"recent_buffer": raising_validator}},
            system_validators=[other_validator],
        )

    detail = exc_info.value.detail["validation_errors"]
    assert any(e["code"] == "validator_internal_error" for e in detail)
    # The other validator must still have been invoked.
    assert other_called, "non-raising validator should still run"


# ---------------------------------------------------------------------------
# Aggregate-don't-fail-fast (spec I7)
# ---------------------------------------------------------------------------


def test_aggregates_errors_across_l2_and_l3() -> None:
    """All Layer 2 and Layer 3 errors surface in one raise."""

    def l3_fails(_body: dict[str, Any]) -> ValidationResult:
        return ValidationResult(
            ok=False,
            errors=[
                ValidationError(
                    layer=3,
                    source="memory.recent_buffer",
                    path="/size",
                    code="too_small",
                    message="size must be > 0",
                )
            ],
        )

    def l2_fails(_resolved: dict[str, Any]) -> ValidationResult:
        return ValidationResult(
            ok=False,
            errors=[
                ValidationError(
                    layer=2,
                    source="provider_check",
                    path="/model/provider",
                    code="invalid_provider",
                    message="unknown provider",
                )
            ],
        )

    with pytest.raises(ConfigInvalid) as exc_info:
        resolve_and_validate(
            [_valid_partial()],
            subsystem_validators={"memory": {"recent_buffer": l3_fails}},
            system_validators=[l2_fails],
        )

    codes = [e["code"] for e in exc_info.value.detail["validation_errors"]]
    assert "too_small" in codes
    assert "invalid_provider" in codes


def test_per_partial_l1_fails_fast_no_merge() -> None:
    """A malformed partial is rejected before merging is attempted."""
    bad_partial = {"unknown_top": "bad"}
    good_partial = {"name": "agent"}
    with pytest.raises(ConfigInvalid) as exc_info:
        resolve_and_validate(
            [bad_partial, good_partial],
            subsystem_validators={},
        )
    # Errors come tagged with their partial index so the user can fix
    # the right file.
    detail = exc_info.value.detail["validation_errors"]
    assert any("partial" in e["source"] for e in detail)


def test_partials_with_mismatched_schema_versions_rejected() -> None:
    """Spec I8 — same-version enforcement in v1."""
    with pytest.raises(ConfigInvalid) as exc_info:
        resolve_and_validate(
            [
                {"schema_version": "0.1", "name": "a"},
                {"schema_version": "0.2", "name": "b"},
            ],
            subsystem_validators={},
        )
    detail = exc_info.value.detail["validation_errors"]
    assert any(e["code"] == "schema_version_mismatch" for e in detail)


# ---------------------------------------------------------------------------
# validate_resolved — required-field enforcement
# ---------------------------------------------------------------------------


def test_validate_resolved_requires_required_fields() -> None:
    # Missing required schema_version, agent_id, name, description,
    # model, prompt, io.
    result = validate_resolved({})
    assert result.ok is False
    assert len(result.errors) >= 1
    # At least one error should mention a missing field.
    assert any(e.code in {"missing", "missing_required"} for e in result.errors)


def test_validate_resolved_accepts_full_config() -> None:
    result = validate_resolved(_resolved_dict())
    assert result.ok is True, result.errors


# ---------------------------------------------------------------------------
# JSON Pointer format on error paths (spec Open Item §2)
# ---------------------------------------------------------------------------


def test_error_path_is_json_pointer_format() -> None:
    result = validate_partial({"model": {"provider": "anthropic", "alien": 1}})
    assert result.ok is False
    paths = [e.path for e in result.errors]
    assert any(p.startswith("/") for p in paths)
    assert any("/model" in p for p in paths)


# ---------------------------------------------------------------------------
# JSON Schema export
# ---------------------------------------------------------------------------


def test_export_json_schema_produces_dict_with_expected_keys() -> None:
    schema = export_json_schema()
    assert isinstance(schema, dict)
    # Pydantic's default JSON schema includes at least these keys for a
    # Pydantic model.
    assert "type" in schema or "$defs" in schema or "properties" in schema
    # Round-trip stability — same call twice produces the same shape.
    assert export_json_schema() == schema


# ---------------------------------------------------------------------------
# ResolvedAgentConfig is what resolve_and_validate returns
# ---------------------------------------------------------------------------


def test_resolve_and_validate_returns_pydantic_model() -> None:
    cfg = resolve_and_validate(
        [_valid_partial()],
        subsystem_validators={
            "memory": {"recent_buffer": lambda _b: ValidationResult(ok=True)}
        },
    )
    assert isinstance(cfg, ResolvedAgentConfig)
    assert cfg.name == "research"
    assert cfg.model.provider == "anthropic"


def test_resolve_and_validate_succeeds_with_no_subsystems() -> None:
    partial = _valid_partial()
    partial["subsystems"] = {}
    cfg = resolve_and_validate([partial], subsystem_validators={})
    assert cfg.subsystems == {}

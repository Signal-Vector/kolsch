"""bug-028 — dynamic per-workflow tool-schema builder unit tests.

Covers the five ACs the ``src/workflows/tool_schema.py`` module
carries alone:

1. ``build_virtual_tool_definitions`` returns one virtual per
   discoverable workflow (AC #1).
2. Virtual tool names are identifier-safe, ≤ 64 chars, and truncate
   with a deterministic blake2b hash suffix past that (AC #2).
3. The dynamic args model has a required ``str`` field per
   ``{{ inputs.X }}`` reference (AC #3).
4. Optional ``| default(...)`` inputs are optional fields with the
   YAML default value probed to int/bool/str (AC #3).
5. Each field description references the YAML line where the input
   first appears (AC #3).

The builder is exercised directly against fake YAML strings — no
catalog, no service registry, no ChatApp.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from workflows.tool_schema import (
    build_virtual_tool_definitions,
    build_workflow_args_model,
    virtual_tool_name,
)


# ---------------------------------------------------------------------------
# Test fixtures — three workflow YAMLs the tests reuse.
# ---------------------------------------------------------------------------


_JD_YAML = """\
description: 'Given a job description URL, review it.'
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: 'curl {{ inputs.url }}'
graph:
  edges:
    - from: fetch
      to: fetch
      binds:
        limit: '{{ inputs.limit | default(10) }}'
"""

_HELLO_YAML = """\
description: 'Hello workflow'
nodes:
  greet:
    type: kolsch/nodes/noop
graph:
  edges:
    - from: greet
      to: greet
      binds:
        message: '{{ inputs.message | default("hi") }}'
"""

_MULTI_YAML = """\
description: 'Multi-input workflow'
nodes:
  first:
    type: kolsch/nodes/bash
    command: '{{ inputs.name }} {{ inputs.email }}'
graph:
  edges:
    - from: first
      to: first
      binds:
        verbose: '{{ inputs.verbose | default(true) }}'
"""


class _FakeWorkflows:
    """Duck-typed workflows impl exposing the two discovery seams."""

    def __init__(self, *, yaml_by_ref: dict[str, str]) -> None:
        self._yaml_by_ref = yaml_by_ref

    def list_workflow_refs(self) -> list[str]:
        return sorted(self._yaml_by_ref)

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        return self._yaml_by_ref.get(ref)


# ---------------------------------------------------------------------------
# Test 1 — AC #1: one virtual per catalogued workflow.
# ---------------------------------------------------------------------------


def test_build_virtual_defs_returns_one_per_workflow() -> None:
    """Two workflows in catalog → two virtual defs."""
    workflows = _FakeWorkflows(
        yaml_by_ref={
            "andre/workflows/job_description_review": _JD_YAML,
            "kolsch/workflows/hello": _HELLO_YAML,
        },
    )
    defs = build_virtual_tool_definitions(
        workflows_impl=workflows,
        home_bundle="kolsch",
    )
    assert len(defs) == 2
    names = sorted(d.name for d in defs)
    assert names == sorted([
        "run_workflow_andre_workflows_job_description_review",
        "run_workflow_kolsch_workflows_hello",
    ])
    # Home bundle stamped uniformly.
    assert all(d.home_bundle == "kolsch" for d in defs)


def test_build_virtual_defs_empty_when_no_seams() -> None:
    """Impl missing ``list_workflow_refs`` short-circuits to []."""

    class _MinimalWorkflows:
        def resolve_workflow_yaml(self, ref: str) -> str | None:
            return None

    defs = build_virtual_tool_definitions(
        workflows_impl=_MinimalWorkflows(),
        home_bundle="kolsch",
    )
    assert defs == []


# ---------------------------------------------------------------------------
# Test 2 — AC #2: naming is identifier-safe + hash-truncated when long.
# ---------------------------------------------------------------------------


def test_virtual_tool_name_is_identifier_safe() -> None:
    """No dots, no dashes, ≤ 64 chars; matches Pydantic name pattern."""
    name = virtual_tool_name("andre/workflows/job_description_review")
    assert name == "run_workflow_andre_workflows_job_description_review"
    assert "." not in name
    assert "-" not in name
    assert len(name) <= 64
    # Matches the ``^[a-z][a-z0-9_]*$`` tool-name pattern.
    import re
    assert re.fullmatch(r"[a-z][a-z0-9_]*", name)


def test_virtual_tool_name_hash_truncation_deterministic() -> None:
    """A ref that mangles past 64 chars gets a stable 8-char hash suffix."""
    long_ref = "a" * 60 + "/workflows/" + "b" * 30
    name_1 = virtual_tool_name(long_ref)
    name_2 = virtual_tool_name(long_ref)
    assert name_1 == name_2  # deterministic
    assert len(name_1) == 64
    # Structure: run_workflow_...<55 chars>...<underscore><8 hex chars>.
    assert name_1.startswith("run_workflow_")
    hash_suffix = name_1[-8:]
    assert all(c in "0123456789abcdef" for c in hash_suffix)


def test_virtual_tool_name_collapses_hyphens_to_underscores() -> None:
    """``hello-agent`` → ``hello_agent`` under the mangling pass."""
    name = virtual_tool_name("kolsch/workflows/hello-agent")
    assert name == "run_workflow_kolsch_workflows_hello_agent"


# ---------------------------------------------------------------------------
# Test 3 — AC #3: required str field per ``{{ inputs.X }}``.
# ---------------------------------------------------------------------------


def test_virtual_args_model_has_required_field_for_url() -> None:
    """Unguarded ``{{ inputs.url }}`` → ``url: str`` required."""
    model = build_workflow_args_model(
        "andre/workflows/job_description_review",
        _JD_YAML,
    )
    assert issubclass(model, BaseModel)
    fields = model.model_fields
    assert "url" in fields
    assert fields["url"].annotation is str
    assert fields["url"].is_required()

    schema = model.model_json_schema()
    assert "url" in schema.get("required", [])
    assert schema["properties"]["url"]["type"] == "string"


def test_virtual_args_model_missing_required_raises_validation_error() -> None:
    """Constructing the args model without ``url`` raises."""
    from pydantic import ValidationError

    model = build_workflow_args_model(
        "andre/workflows/job_description_review",
        _JD_YAML,
    )
    with pytest.raises(ValidationError):
        model()


# ---------------------------------------------------------------------------
# Test 4 — AC #3: optional field with default type probed from YAML.
# ---------------------------------------------------------------------------


def test_virtual_args_model_has_optional_field_with_probed_default() -> None:
    """``{{ inputs.limit | default(10) }}`` → optional ``limit: int = 10``."""
    model = build_workflow_args_model(
        "andre/workflows/job_description_review",
        _JD_YAML,
    )
    fields = model.model_fields
    assert "limit" in fields
    assert fields["limit"].annotation is int
    assert not fields["limit"].is_required()
    assert fields["limit"].default == 10

    schema = model.model_json_schema()
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["limit"]["default"] == 10


def test_virtual_args_model_probes_bool_default() -> None:
    """``| default(true)`` → optional ``bool = True``."""
    model = build_workflow_args_model(
        "andre/workflows/multi",
        _MULTI_YAML,
    )
    fields = model.model_fields
    assert fields["verbose"].annotation is bool
    assert fields["verbose"].default is True


def test_virtual_args_model_probes_string_default() -> None:
    """``| default("hi")`` → optional ``str = "hi"``."""
    model = build_workflow_args_model(
        "kolsch/workflows/hello",
        _HELLO_YAML,
    )
    fields = model.model_fields
    assert fields["message"].annotation is str
    assert fields["message"].default == "hi"


# ---------------------------------------------------------------------------
# Test 5 — AC #3: field descriptions reference the YAML source line.
# ---------------------------------------------------------------------------


def test_virtual_args_model_field_description_references_yaml_line() -> None:
    """Field descriptions carry the YAML line where the input appears."""
    model = build_workflow_args_model(
        "andre/workflows/job_description_review",
        _JD_YAML,
    )
    schema = model.model_json_schema()
    url_desc = schema["properties"]["url"]["description"]
    assert "{{ inputs.url }}" in url_desc
    # The YAML has ``inputs.url`` on line 5 (1-based).
    assert "line 5" in url_desc

    limit_desc = schema["properties"]["limit"]["description"]
    assert "{{ inputs.limit | default(...) }}" in limit_desc
    # ``inputs.limit`` appears on line 11.
    assert "line 11" in limit_desc

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

The builder is exercised directly against mock YAML strings — no
catalog, no service registry, no ChatApp.
"""

from __future__ import annotations

from typing import Any

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


class _MockWorkflows:
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
    workflows = _MockWorkflows(
        yaml_by_ref={
            "acme/workflows/job_description_review": _JD_YAML,
            "kolsch/workflows/hello": _HELLO_YAML,
        },
    )
    defs = build_virtual_tool_definitions(
        workflows_impl=workflows,
        home_bundle="kolsch",
    )
    assert len(defs) == 2
    names = sorted(d.name for d in defs)
    assert names == sorted(
        [
            "run_workflow_acme_workflows_job_description_review",
            "run_workflow_kolsch_workflows_hello",
        ]
    )
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
    name = virtual_tool_name("acme/workflows/job_description_review")
    assert name == "run_workflow_acme_workflows_job_description_review"
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
        "acme/workflows/job_description_review",
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
        "acme/workflows/job_description_review",
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
        "acme/workflows/job_description_review",
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
        "acme/workflows/multi",
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
        "acme/workflows/job_description_review",
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


# ---------------------------------------------------------------------------
# qol-101 — per-workflow typed tools reflect qol-097 promoted required set.
# ---------------------------------------------------------------------------


# Parent-with-sub-workflow-child YAML that has ZERO direct
# ``{{ inputs.X }}`` references. Post-qol-101 the args model still gains
# a required ``url`` field when the caller passes ``effective_required``
# (qol-097 promotes ``url`` from the child).
_PARENT_NO_DIRECT_REFS_YAML = """\
description: 'Parent workflow that calls a child requiring url.'
nodes:
  jd:
    type: kolsch/nodes/workflow
    ref: some/workflows/child
"""


# Parent with a direct ``{{ inputs.url }}`` reference AND a child that
# also requires ``url`` — exercises the "both direct and promoted"
# precedence branch. ``role`` is promoted-only.
_PARENT_WITH_DIRECT_URL_YAML = """\
description: 'Parent with direct url ref.'
nodes:
  echo:
    type: kolsch/nodes/bash
    command: 'echo {{ inputs.url }}'
  jd:
    type: kolsch/nodes/workflow
    ref: some/workflows/child
"""


def test_build_args_model_uses_effective_required_when_provided() -> None:
    """qol-101 AC-1: promoted set adds required fields the direct scan misses."""
    model = build_workflow_args_model(
        "p/workflows/apply",
        _PARENT_NO_DIRECT_REFS_YAML,
        effective_required=["url", "role"],
    )
    fields = model.model_fields
    assert set(fields.keys()) == {"url", "role"}
    assert fields["url"].annotation is str
    assert fields["url"].is_required()
    assert fields["role"].annotation is str
    assert fields["role"].is_required()

    schema = model.model_json_schema()
    assert set(schema.get("required", [])) == {"url", "role"}


def test_build_args_model_no_kwarg_matches_legacy_behavior() -> None:
    """qol-101 AC-2: omitting the kwarg preserves pre-qol-101 field set."""
    with_default_kwarg = build_workflow_args_model(
        "p/workflows/a",
        _JD_YAML,
    )
    with_explicit_none = build_workflow_args_model(
        "p/workflows/a",
        _JD_YAML,
        effective_required=None,
    )
    assert set(with_default_kwarg.model_fields.keys()) == set(
        with_explicit_none.model_fields.keys()
    )
    # And matches the direct-scan expectation (JD_YAML: 'url' required,
    # 'limit' optional).
    assert set(with_default_kwarg.model_fields.keys()) == {"url", "limit"}
    assert with_default_kwarg.model_fields["url"].is_required()
    assert not with_default_kwarg.model_fields["limit"].is_required()


def test_build_args_model_merges_direct_and_promoted_preserving_line_info() -> None:
    """qol-101 AC-5: direct-scan names keep line-referencing description."""
    model = build_workflow_args_model(
        "p/workflows/apply",
        _PARENT_WITH_DIRECT_URL_YAML,
        effective_required=["url", "role"],
    )
    schema = model.model_json_schema()
    # ``url`` is BOTH direct and promoted → line-referencing description
    # wins (existing ``_describe_required`` path).
    url_desc = schema["properties"]["url"]["description"]
    assert "line " in url_desc
    assert "sub-workflow child" not in url_desc
    # ``role`` is promoted-only → generic promoted blurb.
    role_desc = schema["properties"]["role"]["description"]
    assert "required by a sub-workflow child" in role_desc
    assert "qol-097" in role_desc
    # Both remain required.
    assert schema["properties"]["url"]  # present
    assert set(schema.get("required", [])) == {"url", "role"}


class _MockWorkflowsWithPromoted:
    """qol-101 duck-typed impl exposing the promoted-set seam."""

    def __init__(
        self,
        *,
        yaml_by_ref: dict[str, str],
        promoted_by_ref: dict[str, list[str]],
    ) -> None:
        self._yaml_by_ref = yaml_by_ref
        self._promoted_by_ref = promoted_by_ref
        self.promoted_calls: list[str] = []

    def list_workflow_refs(self) -> list[str]:
        return sorted(self._yaml_by_ref)

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        return self._yaml_by_ref.get(ref)

    def resolve_effective_required_inputs(self, ref: str) -> list[str] | None:
        self.promoted_calls.append(ref)
        return self._promoted_by_ref.get(ref)


def test_build_virtual_tool_definitions_probes_and_threads_effective_required() -> None:
    """qol-101 AC-3: the call-site probes the seam and threads its return."""
    impl = _MockWorkflowsWithPromoted(
        yaml_by_ref={"p/workflows/apply": _PARENT_NO_DIRECT_REFS_YAML},
        promoted_by_ref={"p/workflows/apply": ["url"]},
    )
    defs = build_virtual_tool_definitions(
        workflows_impl=impl,
        home_bundle="kolsch",
    )
    assert len(defs) == 1
    assert impl.promoted_calls == ["p/workflows/apply"]
    args_schema = defs[0].args_schema
    assert args_schema is not None
    fields = args_schema.model_fields
    assert "url" in fields
    assert fields["url"].is_required()


@pytest.mark.parametrize(
    "impl_maker",
    [
        # (a) attribute entirely absent.
        pytest.param(
            lambda yaml_by_ref: _MockWorkflows(yaml_by_ref=yaml_by_ref),
            id="seam-absent",
        ),
        # (b) attribute exists but raises.
        pytest.param(
            lambda yaml_by_ref: _make_broken_promoted_impl(
                yaml_by_ref=yaml_by_ref,
                raiser=lambda ref: (_ for _ in ()).throw(RuntimeError("boom")),
            ),
            id="seam-raises",
        ),
        # (c) attribute exists but returns non-list.
        pytest.param(
            lambda yaml_by_ref: _make_broken_promoted_impl(
                yaml_by_ref=yaml_by_ref,
                raiser=lambda ref: "not-a-list",
            ),
            id="seam-returns-non-list",
        ),
    ],
)
def test_build_virtual_tool_definitions_fallback_when_seam_absent_or_broken(
    impl_maker: Any,
) -> None:
    """qol-101 AC-3: absent / raising / non-list seam → silent legacy fallback."""
    impl = impl_maker({"p/workflows/apply": _PARENT_NO_DIRECT_REFS_YAML})
    defs = build_virtual_tool_definitions(
        workflows_impl=impl,
        home_bundle="kolsch",
    )
    assert len(defs) == 1
    args_schema = defs[0].args_schema
    assert args_schema is not None
    # No direct refs in YAML, no promoted set → empty field map.
    assert args_schema.model_fields == {}


def _make_broken_promoted_impl(
    *,
    yaml_by_ref: dict[str, str],
    raiser: Any,
) -> Any:
    """Build a duck-typed impl whose promoted-set seam behaves badly."""

    class _Impl:
        def list_workflow_refs(self) -> list[str]:
            return sorted(yaml_by_ref)

        def resolve_workflow_yaml(self, ref: str) -> str | None:
            return yaml_by_ref.get(ref)

        def resolve_effective_required_inputs(self, ref: str) -> Any:
            return raiser(ref)

    return _Impl()


def test_virtual_tool_dispatch_rejects_missing_promoted_input() -> None:
    """qol-101 AC-4: dispatcher-level Pydantic validation catches the miss."""
    from pydantic import ValidationError

    impl = _MockWorkflowsWithPromoted(
        yaml_by_ref={"p/workflows/apply": _PARENT_NO_DIRECT_REFS_YAML},
        promoted_by_ref={"p/workflows/apply": ["url"]},
    )
    defs = build_virtual_tool_definitions(
        workflows_impl=impl,
        home_bundle="kolsch",
    )
    args_schema = defs[0].args_schema
    assert args_schema is not None
    # This is exactly what ``src/tools/dispatch.py`` does before
    # calling the invoker. If ``url`` is missing, Pydantic raises —
    # the deep ``ChildWorkflowFailed`` code path is never reached.
    with pytest.raises(ValidationError) as excinfo:
        args_schema()
    err_str = str(excinfo.value)
    assert "url" in err_str
    assert "required" in err_str.lower()


# Parent workflow that "optionalizes" `url` via ``| default("foo")``
# while a sub-workflow child still requires it. Reviewer #2 caught
# that the optional-field write used to overwrite the promoted-
# required entry, downgrading `url` to `default="foo"` and silently
# defeating the dispatcher-level rejection.
_PARENT_OPTIONALIZES_PROMOTED_YAML = """\
description: 'Parent guards url with default but a child requires it.'
nodes:
  echo:
    type: kolsch/nodes/bash
    command: 'echo {{ inputs.url | default("foo") }}'
  jd:
    type: kolsch/nodes/workflow
    ref: some/workflows/child
"""


def test_promoted_required_wins_over_parent_optional_default() -> None:
    """qol-101 Reviewer #2 regression: promoted-required beats parent's `| default(...)`.

    If the parent's direct scan flags `url` as OPTIONAL (via a
    ``| default(...)`` guard) but a sub-workflow child requires it,
    the resulting field MUST stay required — otherwise the model can
    be called with ``{}``, Pydantic fills in the parent's default,
    and the child gets a bogus value instead of a dispatch-time
    rejection.
    """
    from pydantic import ValidationError

    model = build_workflow_args_model(
        "p/workflows/apply",
        _PARENT_OPTIONALIZES_PROMOTED_YAML,
        effective_required=["url"],
    )
    fields = model.model_fields
    assert "url" in fields
    assert fields["url"].is_required(), (
        "url must stay required — the promoted set beats the parent's "
        "| default(...) guard"
    )
    schema = model.model_json_schema()
    assert "url" in schema.get("required", [])
    # And the whole point of qol-101 — empty call must be rejected.
    with pytest.raises(ValidationError):
        model()


def test_apply_pipeline_reproducer_end_to_end() -> None:
    """qol-101 AC-4: whole-loop reproducer against the ses_f15a failure shape."""
    parent_yaml = (
        "description: 'Parent — no direct refs, no with:, no inputs block.'\n"
        "nodes:\n"
        "  jd:\n"
        "    type: kolsch/nodes/workflow\n"
        "    ref: child/workflows/child\n"
    )
    # Duck-typed impl that returns the promoted set the qol-097
    # ``effective_required_inputs`` scanner would return against the
    # child's ``{{ inputs.url }}`` reference.
    impl = _MockWorkflowsWithPromoted(
        yaml_by_ref={"parent/workflows/apply_pipeline": parent_yaml},
        promoted_by_ref={"parent/workflows/apply_pipeline": ["url"]},
    )
    defs = build_virtual_tool_definitions(
        workflows_impl=impl,
        home_bundle="parent",
    )
    assert len(defs) == 1
    assert defs[0].name == "run_workflow_parent_workflows_apply_pipeline"
    args_schema = defs[0].args_schema
    assert args_schema is not None
    assert "url" in args_schema.model_fields
    assert args_schema.model_fields["url"].is_required()

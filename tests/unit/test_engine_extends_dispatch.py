"""Unit tests for the engine's extends-dispatch wiring.

bug-engine-extends-parent-not-rendered.md Step 2. Covers the
pure-function pieces of the dispatch:

- ``prompt_uses_extends_directive`` — the AST-walking detector
  the engine uses to spot orphan ``{% extends parent %}`` (prompt
  uses the directive but the YAML has no ``extends:`` field).
- ``render_with_parents`` — the chain-form render. Pins the
  single-parent shape (equivalent to the legacy
  ``render_with_parent``) and the multi-parent shape (E1-style
  three-level chain).
- ``resolve_parent_chain_from_extends`` — the engine-side
  ancestor walker. Pins single-level + multi-level + cycle
  detection.
- The engine integration end-to-end (a real exchange with
  ``{% extends parent %}`` rendering) lives in Step 3's fixture
  corpus; here we only test the dispatch pieces in isolation so
  Step 2 has a unit-level guard against regressions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from errors import (
    ConfigAgentInheritanceCycle,
    ConfigExtendsTemplateWithoutInheritance,
    ConfigRequiredBlockMissing,
)
from templating.inheritance import (
    prompt_uses_extends_directive,
    render_with_parents,
    resolve_parent_chain_from_extends,
)


# ---------------------------------------------------------------------------
# prompt_uses_extends_directive — the AST detector.
# ---------------------------------------------------------------------------


def test_detector_true_on_variable_extends_form() -> None:
    """The canonical author shape: ``{% extends parent %}`` where
    ``parent`` is a variable bound at render time."""
    assert prompt_uses_extends_directive(
        "{% extends parent %}\n{% block role %}x{% endblock %}"
    )


def test_detector_true_on_literal_extends_form() -> None:
    """Literal-string extends should also trigger so the orphan
    check still fires if the author hand-rolls
    ``{% extends 'foo' %}`` without the YAML field."""
    assert prompt_uses_extends_directive('{% extends "foo" %}')


def test_detector_false_on_plain_prompt() -> None:
    assert not prompt_uses_extends_directive(
        "You are an agent. Be concise."
    )


def test_detector_false_on_jinja_without_extends() -> None:
    """Variable interpolation + blocks without an extends directive
    must NOT trigger — only a top-of-template ``{% extends %}`` is
    the loader-requiring shape."""
    assert not prompt_uses_extends_directive(
        "Hello {{ agent.name }}\n"
        "{% block role %}default{% endblock %}\n"
        "{% for t in tools %}{{ t.name }}{% endfor %}"
    )


def test_detector_false_inside_jinja_comment() -> None:
    """A Jinja ``{# ... #}`` comment containing the literal
    substring must NOT false-trigger — Jinja's parser strips
    comments, so the AST has no ``Extends`` node. This is the
    reason we use an AST walk instead of a substring match."""
    assert not prompt_uses_extends_directive(
        "{# {% extends parent %} #}\nPlain body."
    )


def test_detector_false_on_malformed_template() -> None:
    """A parse error short-circuits the detector to ``False`` — the
    downstream ``render_prompt`` path will surface the real Jinja
    error with full StrictUndefined / template detail."""
    assert not prompt_uses_extends_directive("{% extends parent")


# ---------------------------------------------------------------------------
# render_with_parents — single + multi-parent.
# ---------------------------------------------------------------------------


def test_single_parent_renders_equivalently_to_legacy_helper() -> None:
    """One-element parent_chain is the no-multi-level case;
    behaviour must match the existing ``render_with_parent``
    contract — leaf overrides parent's block, parent's prose
    surrounds it."""
    parent = "Header\n{% block role %}default{% endblock %}\nFooter"
    child = (
        "{% extends parent %}\n"
        "{% block role %}OVERRIDDEN{% endblock %}\n"
    )
    out = render_with_parents(child, [parent], {})
    assert "Header" in out
    assert "Footer" in out
    assert "OVERRIDDEN" in out
    assert "default" not in out


def test_multi_parent_chain_resolves_three_levels() -> None:
    """E1 shape: A extends B extends C. A overrides C's deepest
    block. The render must walk C → B → A and emit A's value."""
    grandparent = (
        "ROOT_HEADER\n"
        "{% block role %}root-default{% endblock %}\n"
        "ROOT_FOOTER"
    )
    middle = (
        "{% extends parent %}\n"
        "{% block role %}middle-default{% endblock %}\n"
    )
    leaf = "{% extends parent %}\n{% block role %}leaf-wins{% endblock %}\n"
    out = render_with_parents(leaf, [middle, grandparent], {})
    assert "ROOT_HEADER" in out
    assert "ROOT_FOOTER" in out
    assert "leaf-wins" in out
    assert "middle-default" not in out
    assert "root-default" not in out


def test_multi_parent_chain_inherits_unfilled_block_from_middle() -> None:
    """When the leaf doesn't override a block but the middle layer
    does, the middle's value wins — Jinja's normal chain semantics
    must work across the rewritten directives."""
    grandparent = "{% block role %}root-default{% endblock %}"
    middle = (
        "{% extends parent %}\n"
        "{% block role %}middle-fills-it{% endblock %}\n"
    )
    leaf = "{% extends parent %}\n"  # no override
    out = render_with_parents(leaf, [middle, grandparent], {})
    assert "middle-fills-it" in out
    assert "root-default" not in out


def test_chain_render_passes_through_context_variables() -> None:
    """Variables in the render context (the standard ``tools``,
    ``agent`` etc. binding) reach the leaf's blocks under the
    chain form just like the single-parent helper."""
    parent = "{% block body %}{% endblock %}"
    leaf = "{% extends parent %}{% block body %}name={{ agent_name }}{% endblock %}"
    out = render_with_parents(leaf, [parent], {"agent_name": "butler"})
    assert out == "name=butler"


def test_chain_render_required_block_missing_raises_typed() -> None:
    """The chain form preserves the legacy required-block trap so
    D2's ``config.required_block_missing`` Fatal still fires."""
    parent = "{% block role required %}{% endblock %}"
    leaf = "{% extends parent %}"
    with pytest.raises(ConfigRequiredBlockMissing) as excinfo:
        render_with_parents(
            leaf, [parent], {}, template_name="agent:ag_test"
        )
    assert excinfo.value.code == "config.required_block_missing"
    assert excinfo.value.detail["agent_path"] == "agent:ag_test"


def test_empty_chain_raises_value_error() -> None:
    """``render_with_parents([], ...)`` would silently produce a
    template that has nothing to extend; callers must pass at
    least one parent. Catch the misuse loudly."""
    with pytest.raises(ValueError, match="at least one parent"):
        render_with_parents("{% extends parent %}", [], {})


# ---------------------------------------------------------------------------
# resolve_parent_chain_from_extends — engine-side ancestor walker.
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_resolve_parent_chain_single_level(tmp_path: Path) -> None:
    """One-level chain: leaf's extends points at a parent that has
    no further extends. Returns the single parent's payload."""
    parent_path = tmp_path / "parent.yaml"
    _write_yaml(parent_path, {"name": "parent", "prompt": "P_BODY"})
    chain = resolve_parent_chain_from_extends(
        "workspace:parent.yaml", tmp_path
    )
    assert len(chain) == 1
    assert chain[0]["name"] == "parent"
    assert chain[0]["prompt"] == "P_BODY"


def test_resolve_parent_chain_multi_level(tmp_path: Path) -> None:
    """A extends B extends C. Engine sees A.extends="…/b.yaml" →
    chain = [B, C] in that order (immediate-parent first)."""
    _write_yaml(
        tmp_path / "c.yaml", {"name": "root", "prompt": "C_BODY"}
    )
    _write_yaml(
        tmp_path / "b.yaml",
        {"name": "middle", "prompt": "B_BODY", "extends": "workspace:c.yaml"},
    )
    chain = resolve_parent_chain_from_extends(
        "workspace:b.yaml", tmp_path
    )
    assert [layer["name"] for layer in chain] == ["middle", "root"]


def test_resolve_parent_chain_detects_cycle(tmp_path: Path) -> None:
    """Two-node cycle (A extends B; B extends A) raises
    ``config.agent_inheritance_cycle`` Fatal — same envelope as
    the asset-manager-side detector."""
    _write_yaml(
        tmp_path / "a.yaml",
        {"name": "a", "extends": "workspace:b.yaml"},
    )
    _write_yaml(
        tmp_path / "b.yaml",
        {"name": "b", "extends": "workspace:a.yaml"},
    )
    with pytest.raises(ConfigAgentInheritanceCycle):
        resolve_parent_chain_from_extends("workspace:a.yaml", tmp_path)


# ---------------------------------------------------------------------------
# Orphan error class — smoke for the dispatch site's typed Fatal.
# ---------------------------------------------------------------------------


def test_orphan_error_class_carries_agent_path_detail() -> None:
    """The engine raises this class when the orphan condition is
    detected; the unit-level coverage is in test_errors.py, but
    re-pin the detail shape here so the dispatch contract is
    visible at this seam."""
    err = ConfigExtendsTemplateWithoutInheritance(
        agent_path="agent:ag_butler"
    )
    assert err.code == "config.extends_template_without_inheritance"
    assert err.level.name == "FATAL"
    assert err.detail["agent_path"] == "agent:ag_butler"

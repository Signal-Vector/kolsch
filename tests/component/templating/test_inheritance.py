"""Agent inheritance — Phase 2.2 Step 13.

Pins the scenarios the spec §8 + build plan Step 13 reviewer
prompt mandate:

- Single-level: child + parent, child fields override wholesale.
- Multi-level: A → B → C, leaf wins.
- Cycle detection: A → B → A raises
  :class:`ConfigAgentInheritanceCycle`.
- Missing parent: :class:`ConfigParentAgentNotFound`.
- Required block enforcement:
  :class:`ConfigRequiredBlockMissing`.
- Required block present → renders.
- Wholesale-per-field override (a child with ``tools: [bash]``
  AND a parent with ``tools: [read_file, bash]`` resolves to
  ``[bash]``, not the union).
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest
import yaml

from errors import (
    ConfigAgentInheritanceCycle,
    ConfigParentAgentNotFound,
    ConfigRequiredBlockMissing,
)
from templating.inheritance import (
    apply_inheritance,
    render_with_parent,
    resolve_extends_chain,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Chain resolution.
# ---------------------------------------------------------------------------


def test_single_level_chain_resolves_to_two_entries(tmp_path: Path) -> None:
    parent_yaml = {"name": "base", "model": {"provider": "x", "model": "y"}}
    (tmp_path / "_base.yaml").write_text(yaml.safe_dump(parent_yaml), encoding="utf-8")
    child_yaml = {
        "name": "child",
        "extends": "workspace:_base.yaml",
    }
    child_path = tmp_path / "child.yaml"
    child_path.write_text(yaml.safe_dump(child_yaml), encoding="utf-8")

    chain = resolve_extends_chain(child_path, tmp_path)
    assert len(chain) == 2
    assert chain[0]["name"] == "child"  # leaf-first
    assert chain[1]["name"] == "base"


def test_multi_level_chain_resolves_a_b_c(tmp_path: Path) -> None:
    """A extends B extends C — chain returned leaf-first."""
    (tmp_path / "c.yaml").write_text(
        yaml.safe_dump({"name": "c"}), encoding="utf-8"
    )
    (tmp_path / "b.yaml").write_text(
        yaml.safe_dump({"name": "b", "extends": "workspace:c.yaml"}),
        encoding="utf-8",
    )
    a_path = tmp_path / "a.yaml"
    a_path.write_text(
        yaml.safe_dump({"name": "a", "extends": "workspace:b.yaml"}),
        encoding="utf-8",
    )

    chain = resolve_extends_chain(a_path, tmp_path)
    assert [layer["name"] for layer in chain] == ["a", "b", "c"]


def test_cycle_detection_raises_inheritance_cycle(tmp_path: Path) -> None:
    """A → B → A. The visited set catches the loop and the error
    detail includes the trail."""
    (tmp_path / "a.yaml").write_text(
        yaml.safe_dump({"name": "a", "extends": "workspace:b.yaml"}),
        encoding="utf-8",
    )
    (tmp_path / "b.yaml").write_text(
        yaml.safe_dump({"name": "b", "extends": "workspace:a.yaml"}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigAgentInheritanceCycle) as excinfo:
        resolve_extends_chain(tmp_path / "a.yaml", tmp_path)
    assert len(excinfo.value.detail["cycle"]) >= 2


def test_missing_parent_raises_parent_not_found(tmp_path: Path) -> None:
    (tmp_path / "child.yaml").write_text(
        yaml.safe_dump({"name": "child", "extends": "workspace:nope.yaml"}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigParentAgentNotFound):
        resolve_extends_chain(tmp_path / "child.yaml", tmp_path)


# ---------------------------------------------------------------------------
# apply_inheritance — wholesale-per-field override.
# ---------------------------------------------------------------------------


def test_apply_inheritance_child_overrides_parent_wholesale() -> None:
    """Spec §8.2: child fields replace parent's wholesale —
    NEVER merged. ``tools: [bash]`` in child + ``tools:
    [read_file, bash]`` in parent → ``[bash]``."""
    chain = [
        {"name": "child", "tools": ["bash"], "model": "haiku"},  # leaf
        {"name": "parent", "tools": ["read_file", "bash"], "model": "opus"},
    ]
    resolved = apply_inheritance(chain)
    assert resolved["tools"] == ["bash"]  # wholesale — NOT merged
    assert resolved["model"] == "haiku"  # child wins


def test_apply_inheritance_inherits_unspecified_fields() -> None:
    """Spec §8.2: if a field isn't in the child, the parent's
    value carries through."""
    chain = [
        {"name": "child"},  # leaf — only `name`
        {"name": "parent", "model": "opus", "tools": ["bash"]},
    ]
    resolved = apply_inheritance(chain)
    assert resolved["name"] == "child"
    assert resolved["model"] == "opus"
    assert resolved["tools"] == ["bash"]


def test_apply_inheritance_strips_extends_from_resolved() -> None:
    """The ``extends:`` key is metadata for the chain walker;
    the composed dict shouldn't carry it through to validation."""
    chain = [
        {"name": "child", "extends": "workspace:_base.yaml"},
        {"name": "parent"},
    ]
    resolved = apply_inheritance(chain)
    assert "extends" not in resolved


# ---------------------------------------------------------------------------
# render_with_parent — Jinja loader + required blocks.
# ---------------------------------------------------------------------------


def test_render_with_parent_resolves_extends_variable() -> None:
    """``{% extends parent %}`` works because the loader pre-loads
    the parent template under the name ``"parent"`` and the
    context binds ``parent = "parent"``."""
    parent = "Header\n{% block role %}default-role{% endblock %}\nFooter"
    child = textwrap.dedent(
        """\
        {% extends parent %}
        {% block role %}OVERRIDE{% endblock %}
        """
    )
    out = render_with_parent(child, parent, {})
    assert "Header" in out
    assert "OVERRIDE" in out
    assert "Footer" in out
    assert "default-role" not in out


def test_render_with_parent_required_block_present_renders() -> None:
    """Spec §8.4: required blocks render normally when the child
    DOES override them."""
    parent = "{% block role required %}{% endblock %}"
    child = "{% extends parent %}{% block role %}Here.{% endblock %}"
    assert render_with_parent(child, parent, {}) == "Here."


def test_render_with_parent_required_block_missing_raises() -> None:
    """Spec §8.4 + ENV §4.18 — child extends without overriding
    a required block."""
    parent = "{% block role required %}{% endblock %}"
    child = "{% extends parent %}"
    with pytest.raises(ConfigRequiredBlockMissing) as excinfo:
        render_with_parent(
            child, parent, {}, template_name="bundles/x/agents/child.yaml"
        )
    err = excinfo.value
    assert err.code == "config.required_block_missing"
    assert err.detail["agent_path"] == "bundles/x/agents/child.yaml"
    # Block name should be parsed from Jinja's message.
    assert err.detail["block_name"] in ("role", "<unknown>")


def test_render_with_parent_passes_through_context_variables() -> None:
    """Variables in the context (the standard ``tools``, ``docs``
    etc. binding) reach the child template just like the inline
    render harness."""
    parent = "{% block body %}{% endblock %}"
    child = "{% extends parent %}{% block body %}name={{ agent_name }}{% endblock %}"
    out = render_with_parent(child, parent, {"agent_name": "helper"})
    assert out == "name=helper"

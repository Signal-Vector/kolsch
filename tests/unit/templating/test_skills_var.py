"""qol-skills-jinja-variable-symmetric-with-tools — SkillsVar unit tests.

Mirrors the ``test_tools_var`` patterns: name lookup, iteration order,
``list()`` / ``summary()`` / ``names()`` rendering, default ``__str__``,
the deliberately un-Pythonic ``__getitem__`` returning ``None`` for
unknown names.
"""

from __future__ import annotations

from templating.vars import SkillItem, SkillsVar


def _item(name: str, *, description: str = "", ref: str = "") -> SkillItem:
    return SkillItem(
        name=name,
        description=description or f"{name} description",
        ref=ref or f"kolsch/skills/{name}",
    )


def test_skills_var_empty_renders_empty_string() -> None:
    var = SkillsVar([])
    assert var.list() == ""
    assert var.summary() == ""
    assert var.names() == ""
    assert var.count == 0
    assert len(var) == 0
    assert str(var) == ""
    assert list(var) == []


def test_skills_var_list_format_matches_pre_qol_listing() -> None:
    """Per Step 1 writeup §4 — the per-skill bullet shape matches
    the pre-QoL ``format_skills_listing`` byte-identically so
    authors who write ``{{ skills }}`` see what they used to see
    from the auto-append."""
    var = SkillsVar(
        [
            _item("alpha", description="A skill.", ref="kolsch/skills/alpha"),
            _item("beta", description="B skill.", ref="kolsch/skills/beta"),
        ]
    )
    assert var.list() == (
        "- **alpha** — A skill. Path: `kolsch/skills/alpha/SKILL.md`\n"
        "- **beta** — B skill. Path: `kolsch/skills/beta/SKILL.md`"
    )


def test_skills_var_summary_renders_one_line_per_skill() -> None:
    var = SkillsVar([_item("alpha"), _item("beta")])
    assert var.summary() == ("alpha: alpha description\nbeta: beta description")


def test_skills_var_names_renders_comma_separated() -> None:
    var = SkillsVar([_item("alpha"), _item("beta"), _item("gamma")])
    assert var.names() == "alpha, beta, gamma"


def test_skills_var_str_defaults_to_list() -> None:
    """``{{ skills }}`` in a Jinja template renders as
    ``{{ skills.list() }}`` — mirrors :class:`ToolsVar`."""
    var = SkillsVar([_item("alpha")])
    assert str(var) == var.list()


def test_skills_var_iteration_preserves_declared_order() -> None:
    items = [_item("c"), _item("a"), _item("b")]
    var = SkillsVar(items)
    assert [s.name for s in var] == ["c", "a", "b"]


def test_skills_var_get_returns_item_or_none() -> None:
    var = SkillsVar([_item("alpha")])
    found = var.get("alpha")
    assert found is not None
    assert found.name == "alpha"
    assert var.get("missing") is None


def test_skills_var_subscript_mirrors_get_and_returns_none_for_missing() -> None:
    """Deliberately un-Pythonic — :class:`ToolsVar` does the same so
    templates can pair with ``| default("…")``. KeyError would block
    that pattern."""
    var = SkillsVar([_item("alpha")])
    assert var["alpha"] is not None
    assert var["missing"] is None


def test_skills_var_contains_check() -> None:
    var = SkillsVar([_item("alpha")])
    assert "alpha" in var
    assert "missing" not in var
    # Non-string keys don't raise — match the dict.__contains__ discipline.
    assert 42 not in var


def test_skill_item_carries_three_fields() -> None:
    item = SkillItem(name="x", description="d", ref="b/skills/x")
    assert item.name == "x"
    assert item.description == "d"
    assert item.ref == "b/skills/x"

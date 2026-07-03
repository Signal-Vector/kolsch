"""Render-harness unit tests — Phase 2.2 Step 2 (ENV §3).

Pins five contracts the harness must hold:

1. **Empty template → empty output.** Trivial but useful to have a
   sanity-floor test.
2. **Plain text (no Jinja markers) → byte-identical output.** The
   backward-compatibility contract: every Phase 1.x / 2.0 / 2.1
   agent's existing ``prompt:`` must keep working unchanged.
3. **Variable substitution.** The smoke test for Jinja being wired
   at all.
4. **StrictUndefined.** A typo'd variable name surfaces as
   :class:`errors.ConfigTemplateRenderFailed` (``config
   .template_render_failed``) — Fatal at agent-load, never silently
   sent to the LLM.
5. **The ``| default`` filter still works** under StrictUndefined.
   This is the Step 2 escalation gate: Jinja's filter system runs
   ``default`` BEFORE ``StrictUndefined.__str__`` trips, so
   ``{{ var | default("x") }}`` returns ``"x"`` for an undefined
   ``var`` rather than raising. Verified manually with jinja2 3.1.6
   before this test landed; the test pins the behaviour.

Plus a thin error-detail check confirming the Fatal envelope's
``detail.template`` / ``detail.reason`` carry the right
identifiers — Phase 2.2 ENV-R6 §4.18's contract for actionable
errors.
"""

from __future__ import annotations

import pytest

from errors import ConfigTemplateRenderFailed
from templating import render_prompt

# ---------------------------------------------------------------------------
# Floor + plain-text + substitution.
# ---------------------------------------------------------------------------


def test_empty_template_renders_to_empty_string() -> None:
    """Trivial sanity floor — an empty source renders to an empty
    output. Pinning so a future refactor of the Environment setup
    doesn't accidentally inject an extra newline."""
    assert render_prompt("", {}) == ""


def test_plain_text_no_jinja_markers_renders_byte_identical() -> None:
    """Phase 2.2 §3.2 backward-compatibility contract: a prompt with
    no ``{{ }}`` / ``{% %}`` markers must round-trip byte-identical.
    Every Phase 1.x agent's plain-text prompt continues to work
    after Phase 2.2 lands; this pin protects that promise."""
    source = (
        "You are a helpful assistant.\n"
        "Use the tools you have. Don't invent reasons to use tools "
        "you don't need.\n\n"
        "Be concise."
    )
    assert render_prompt(source, {}) == source


def test_simple_variable_substitution_renders() -> None:
    """Smoke test for Jinja being wired at all. Pin the canonical
    ``{{ name }}`` form against a populated context."""
    assert render_prompt("Hello, {{ name }}!", {"name": "world"}) == "Hello, world!"


def test_nested_attribute_access_renders() -> None:
    """The five Phase 2.2 variables expose attribute access
    (e.g. ``{{ agent.name }}``). Pin that the harness honours
    standard Jinja attribute resolution against a dict — Steps 3-7's
    variable objects also expose attributes, so the same path
    exercises both."""
    out = render_prompt(
        "{{ agent.name }} runs on {{ agent.model }}",
        {"agent": {"name": "helper", "model": "claude-opus-4-7"}},
    )
    assert out == "helper runs on claude-opus-4-7"


# ---------------------------------------------------------------------------
# StrictUndefined — the load-bearing safety property.
# ---------------------------------------------------------------------------


def test_typoed_variable_name_raises_template_render_failed() -> None:
    """The StrictUndefined contract: an unknown variable surfaces
    as ``config.template_render_failed`` (Fatal) at render time
    rather than silently rendering an empty string.

    This is what catches typos like ``{{ workspce }}`` or
    ``{{ agnet.name }}`` at agent-load before the broken prompt
    ever reaches the model — Phase 2.2 ENV-R6 §4.18's load-bearing
    discipline.
    """
    with pytest.raises(ConfigTemplateRenderFailed) as excinfo:
        render_prompt("Hello, {{ workspce }}!", {})
    err = excinfo.value
    assert err.code == "config.template_render_failed"
    # Detail names the bad reference + template — agent author can
    # locate the typo without reading a Jinja traceback.
    assert err.detail["template"] == "<inline>"
    assert "workspce" in err.detail["reason"]


def test_typoed_attribute_on_known_variable_raises() -> None:
    """``{{ agent.nme }}`` against ``agent = {"name": "x"}`` still
    raises under StrictUndefined: attribute lookups return the
    undefined sentinel, which trips on first use. Pinning so
    dict-vs-attribute behaviour stays consistent."""
    with pytest.raises(ConfigTemplateRenderFailed):
        render_prompt("{{ agent.nme }}", {"agent": {"name": "x"}})


def test_template_name_surfaces_in_error_detail() -> None:
    """Production callers pass the agent's YAML path as
    ``template_name``; the error envelope carries it through so
    error-renderers can point the user at the right file."""
    with pytest.raises(ConfigTemplateRenderFailed) as excinfo:
        render_prompt(
            "{{ ghost }}",
            {},
            template_name="bundles/kolsch/agents/helper.yaml",
        )
    assert excinfo.value.detail["template"] == "bundles/kolsch/agents/helper.yaml"


def test_malformed_jinja_syntax_raises_template_render_failed() -> None:
    """Syntax errors (unclosed braces, malformed control blocks)
    surface through the same envelope as undefined-variable errors
    — both are Jinja ``TemplateError`` subclasses; the harness
    flattens them to one Fatal code per ENV-R6 §4.18."""
    with pytest.raises(ConfigTemplateRenderFailed) as excinfo:
        render_prompt("{% if %}broken{% endif %}", {})
    assert excinfo.value.code == "config.template_render_failed"


# ---------------------------------------------------------------------------
# The escalation gate — `| default("x")` must work under StrictUndefined.
# ---------------------------------------------------------------------------


def test_default_filter_compensates_for_undefined_variable() -> None:
    """**Escalation gate from build-plan Step 2.** If
    ``{{ var | default("x") }}`` raised, the harness would force
    every author to guard every optional reference with ``{% if %}``
    blocks — verbose and error-prone. Jinja's filter system runs
    ``default`` BEFORE the ``StrictUndefined.__str__`` trip-wire,
    so the filter returns the substitute and the trip-wire never
    fires.

    Verified manually against jinja2 3.1.6 (the floor pinned in
    ``pyproject.toml``); this test pins the behaviour against
    accidental regression if a future bump changes the
    interaction.
    """
    assert render_prompt('{{ var | default("x") }}', {}) == "x"


def test_default_filter_passes_through_when_value_is_defined() -> None:
    """Counterpart to the gate above — when the variable IS in the
    context, ``default`` is a no-op."""
    assert render_prompt('{{ var | default("x") }}', {"var": "real"}) == "real"


def test_default_filter_works_with_attribute_access() -> None:
    """``{{ workspace.summary | default("…") }}`` is the canonical
    prompt pattern for an optional attribute (per spec §4.4 + the
    agent-templating how-to). Pin that the filter survives an
    undefined attribute on a defined variable too."""
    out = render_prompt(
        '{{ workspace.summary | default("Active Kolsch workspace.") }}',
        {"workspace": {}},
    )
    assert out == "Active Kolsch workspace."

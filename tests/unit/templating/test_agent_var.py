"""``AgentVar`` + ``ModelView`` unit tests — Phase 2.2 Step 6
(spec §4.4 + Step 6 reviewer's four checks).

Pins:

- All six attributes from spec §4.4 (``name`` / ``id`` /
  ``model`` / ``tools`` / ``permissions`` / ``context_files``).
- ``agent.model`` returns the :class:`ModelView` projection (NOT
  a string) with ``.ref`` / ``.provider`` / ``.model`` /
  ``.context_length`` / ``.supports_tools`` / ``.fallback`` —
  reviewer's check 1.
- ``agent.tools`` is the SAME instance as the top-level
  :class:`ToolsVar` (object identity) — reviewer's check 2.
- Typo'd attribute names raise :class:`AttributeError`
  (``slots=True`` discipline) — reviewer's check 3.
- :meth:`ModelView.from_model_spec` reuses the existing
  :class:`schemas.ModelSpec` — reviewer's check 4 (not
  redefined).
- ``__str__`` produces the spec §4.4 default rendering:
  ``<name> (<id>) — <provider>/<model>``.
"""

from __future__ import annotations

import pytest

from providers.authz import Rule
from schemas import ModelSpec
from templating.vars.agent import AgentVar, ModelView
from templating.vars.tools import ToolItem, ToolsVar

# ---------------------------------------------------------------------------
# ModelView — the presentation wrapper around ModelSpec.
# ---------------------------------------------------------------------------


def test_model_view_from_model_spec_populates_ref_and_passes_through_fields() -> None:
    """Spec §4.4 ``agent.model`` attributes — ``ref`` is the
    canonical ``<provider>/<model>`` string; ``provider`` /
    ``model`` mirror the source."""
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    view = ModelView.from_model_spec(spec)
    assert view.ref == "anthropic/claude-opus-4-7"
    assert view.provider == "anthropic"
    assert view.model == "claude-opus-4-7"


def test_model_view_context_length_and_supports_tools_default_to_none() -> None:
    """Spec §4.4: ``context_length`` is ``int or None``;
    ``supports_tools`` is ``bool or None``. Step 14's pre-flight
    fills them when the provider catalog is available; until
    then the defaults let ``{{ agent.model.context_length |
    default("?") }}`` work in templates."""
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    view = ModelView.from_model_spec(spec)
    assert view.context_length is None
    assert view.supports_tools is None


def test_model_view_accepts_explicit_metadata() -> None:
    """The pre-flight wiring path — Step 14 passes metadata
    sourced from the provider catalog."""
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    view = ModelView.from_model_spec(spec, context_length=200_000, supports_tools=True)
    assert view.context_length == 200_000
    assert view.supports_tools is True


def test_model_view_passes_through_fallback_chain() -> None:
    """Spec §4.4: ``agent.model.fallback`` is the list of
    :class:`ModelSpec` from the resolved config. Order preserved;
    elements pass through unmodified."""
    fallback = [
        ModelSpec(provider="anthropic", model="claude-haiku"),
        ModelSpec(provider="openai", model="gpt-4o"),
    ]
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7", fallback=fallback)
    view = ModelView.from_model_spec(spec)
    assert view.fallback == fallback


def test_model_view_typo_raises_attribute_error() -> None:
    """``slots=True`` discipline applies to :class:`ModelView`
    too — ``agent.model.contxt_length`` (typo) raises rather
    than silently rendering ``None``."""
    view = ModelView.from_model_spec(
        ModelSpec(provider="anthropic", model="claude-opus-4-7")
    )
    with pytest.raises(AttributeError):
        _ = view.contxt_length


# ---------------------------------------------------------------------------
# AgentVar — attribute access + object identity for tools.
# ---------------------------------------------------------------------------


def _make_tools_var() -> ToolsVar:
    return ToolsVar(
        [
            ToolItem(
                name="bash",
                description="Run a shell command.",
                summary="Run a shell command.",
                signature="command: str",
                schema={},
            ),
        ]
    )


def test_agent_var_exposes_six_documented_attributes() -> None:
    """Spec §4.4 names exactly six attributes for ``agent``."""
    tools = _make_tools_var()
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    agent = AgentVar(
        name="helper",
        id="ag_helper",
        model=ModelView.from_model_spec(spec),
        tools=tools,
    )
    assert agent.name == "helper"
    assert agent.id == "ag_helper"
    assert isinstance(agent.model, ModelView)
    assert isinstance(agent.tools, ToolsVar)
    assert agent.permissions == []
    assert agent.context_files == []


def test_agent_model_returns_model_view_not_string() -> None:
    """Reviewer's check 1 — ``agent.model`` must return the
    object (with ``.ref`` / ``.provider`` / ``.model`` / etc.),
    NOT a string projection."""
    tools = _make_tools_var()
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    agent = AgentVar(
        name="helper",
        id="ag_helper",
        model=ModelView.from_model_spec(spec),
        tools=tools,
    )
    assert not isinstance(agent.model, str)
    assert agent.model.provider == "anthropic"
    assert agent.model.model == "claude-opus-4-7"
    assert agent.model.ref == "anthropic/claude-opus-4-7"


def test_agent_tools_is_same_instance_as_top_level_tools() -> None:
    """Reviewer's check 2 — ``agent.tools`` and the top-level
    ``tools`` variable must be the SAME instance (object
    identity), not equal lists. Templates that alias one as the
    other should see consistent state."""
    tools = _make_tools_var()
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    agent = AgentVar(
        name="helper",
        id="ag_helper",
        model=ModelView.from_model_spec(spec),
        tools=tools,
    )
    assert agent.tools is tools


def test_agent_permissions_returns_rule_objects() -> None:
    """``agent.permissions`` is a list of :class:`providers.authz
    .Rule` objects when the resolved config carries any. Step 8
    adds the field to :class:`ResolvedAgentConfig`; until then
    the default is an empty list."""
    tools = _make_tools_var()
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    rule = Rule(what="kolsch/tools/bash", pattern="*", effect="prompt")
    agent = AgentVar(
        name="helper",
        id="ag_helper",
        model=ModelView.from_model_spec(spec),
        tools=tools,
        permissions=[rule],
    )
    assert agent.permissions == [rule]
    assert all(isinstance(r, Rule) for r in agent.permissions)


def test_agent_typo_attribute_raises() -> None:
    """Reviewer's check 3 — ``slots=True`` makes unknown
    attribute access raise. ``agent.nme`` (typo of ``name``)
    fails fast under StrictUndefined at render time."""
    tools = _make_tools_var()
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    agent = AgentVar(
        name="helper",
        id="ag_helper",
        model=ModelView.from_model_spec(spec),
        tools=tools,
    )
    with pytest.raises(AttributeError):
        _ = agent.nme


# ---------------------------------------------------------------------------
# __str__ — spec §4.4 default rendering.
# ---------------------------------------------------------------------------


def test_default_rendering_matches_spec_format() -> None:
    """Spec §4.4: ``<name> (<id>) — <provider>/<model>``. Em dash
    is U+2014 (matches the spec's example). Pinning so a future
    refactor that changes the separator breaks loudly."""
    tools = _make_tools_var()
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    agent = AgentVar(
        name="helper",
        id="ag_helper",
        model=ModelView.from_model_spec(spec),
        tools=tools,
    )
    assert str(agent) == "helper (ag_helper) — anthropic/claude-opus-4-7"

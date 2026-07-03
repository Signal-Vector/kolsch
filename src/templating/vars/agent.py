"""The `agent` template variable — single-object agent self-introspection.

Attributes: `name` (display name from `config.name`), `id` (the
`config.agent_id` projection), `model` (a
[`ModelView`][templating.vars.agent.ModelView] projection over the
resolved [`ModelSpec`][schemas.ModelSpec]), `tools` (the same
[`ToolsVar`][templating.vars.tools.ToolsVar] instance as the
top-level `tools` variable, not a copy), `permissions` (a list of
[`Rule`][providers.authz.Rule] objects from the agent's resolved
`permissions:` field), and `context_files` (a list of
[`DocItem`][templating.vars.docs.DocItem] projections from
`context_files:`).

`slots=True` makes typo'd attribute access raise `AttributeError`
cleanly. `__str__` produces `<name> (<id>) — <provider>/<model>`.

**Why ModelView.** [`ModelSpec`][schemas.ModelSpec] carries
`provider`, `model`, `parameters`, and `fallback` only;
`agent.model` needs `ref`, `context_length`, and `supports_tools` on
top. The templating layer owns its presentation types, so
`ModelSpec` is unchanged and `ModelView` wraps it via
[`from_model_spec`][templating.vars.agent.ModelView.from_model_spec].
A future merge — extending `ModelSpec` and aliasing `ModelView` —
would be mechanical through that seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from providers.authz import Rule
    from schemas import ModelSpec
    from templating.vars.tools import ToolsVar


# ---------------------------------------------------------------------------
# ModelView — presentation wrapper around ModelSpec.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ModelView:
    """Presentation projection for `agent.model` over a `ModelSpec`.

    `ref` is the canonical `<provider>/<model>` string. `provider`
    and `model` mirror [`ModelSpec`][schemas.ModelSpec].
    `context_length` and `supports_tools` come from the provider's
    catalog metadata or `None` when unavailable. `fallback` is the
    agent's fallback chain (a list of
    [`ModelSpec`][schemas.ModelSpec], exposed as-is so templates
    that iterate get the canonical objects).
    """

    ref: str
    provider: str
    model: str
    context_length: int | None
    supports_tools: bool | None
    fallback: list[ModelSpec]

    @classmethod
    def from_model_spec(
        cls,
        spec: ModelSpec,
        *,
        context_length: int | None = None,
        supports_tools: bool | None = None,
    ) -> ModelView:
        """Construct a `ModelView` from a `ModelSpec`.

        `context_length` and `supports_tools` default to `None`; the
        engine's pre-flight passes the resolved values when the
        provider's catalog is available, and tests either pass
        concrete values or accept the `None` defaults.
        """
        return cls(
            ref=f"{spec.provider}/{spec.model}",
            provider=spec.provider,
            model=spec.model,
            context_length=context_length,
            supports_tools=supports_tools,
            fallback=list(spec.fallback),
        )


# ---------------------------------------------------------------------------
# AgentVar.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentVar:
    """The `agent` template variable — single-object self-introspection.

    See the module docstring for the field surface and the
    `__str__` rendering shape.
    """

    name: str
    id: str
    model: ModelView
    tools: ToolsVar
    permissions: list[Rule] = field(default_factory=list)
    context_files: list[Any] = field(default_factory=list)

    def __str__(self) -> str:
        """Render `<name> (<id>) — <provider>/<model>`.

        Used when a template writes `{{ agent }}` directly rather
        than reaching into specific attributes. The em dash (U+2014)
        is intentional.
        """
        return f"{self.name} ({self.id}) — {self.model.provider}/{self.model.model}"

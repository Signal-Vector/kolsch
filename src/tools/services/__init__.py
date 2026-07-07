"""Tool services scaffold — Protocols, ABC stubs, and the narrowing helper.

Four capability-scoped services the dispatcher can inject into a
tool's [`ToolContext`][tools.decorator.ToolContext] based on the
tool's declared `requires=[...]`:

- `cards` — [`Cards`][tools.services.cards.Cards] +
  [`AbstractCards`][tools.services.cards.AbstractCards]
- `prompts` — [`Prompts`][tools.services.prompts.Prompts] +
  [`AbstractPrompts`][tools.services.prompts.AbstractPrompts]
- `workflows` — [`Workflows`][tools.services.workflows.Workflows] +
  [`AbstractWorkflows`][tools.services.workflows.AbstractWorkflows]
- `session` — [`SessionView`][tools.services.session_view.SessionView] +
  [`AbstractSessionView`][tools.services.session_view.AbstractSessionView]

Author usage — reach the recommended narrowing pattern via
[`require_services`][tools.services.require_services]:

```python
@tool(name="my_tool", description="...", args_schema=MyArgs,
      requires=["cards", "prompts"])
async def my_tool(args: MyArgs, ctx: ToolContext) -> str:
    services = require_services(ctx, ["cards", "prompts"])
    await services.cards.notice("starting")
    ...
```

The scaffold ships no behaviour — spec-004 through spec-012 land the
concrete impls that populate the fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from tools.services.cards import AbstractCards, Cards
from tools.services.exceptions import CardClosed, PromptBusy, ToolNotAvailable
from tools.services.prompts import (
    AbstractPrompts,
    Prompts,
    PromptSource,
    PromptSourceKind,
)
from tools.services.session_view import AbstractSessionView, SessionView
from tools.services.workflows import AbstractWorkflows, Workflows

if TYPE_CHECKING:
    from tools.decorator import ToolContext


ServiceName = Literal["cards", "prompts", "workflows", "session"]
"""Closed set of tool-service names the `@tool(requires=[...])` kwarg accepts.

The single source of truth: anywhere a string discriminates a
service, it must be `ServiceName`-typed.
"""


@dataclass(frozen=True)
class RequiredServices:
    """Non-nullable view over a subset of `ToolContext` services.

    Fields NOT requested via `require_services(ctx, [...])` are
    always `None`. Fields that were requested carry the injected
    handle. Callers dereference requested fields directly — mypy
    treats them as `<Service> | None` at the type level; the
    dataclass shape means the caller already checked availability
    before receiving this view.
    """

    cards: Cards | None
    prompts: Prompts | None
    workflows: Workflows | None
    session: SessionView | None


def require_services(
    ctx: ToolContext,
    which: list[ServiceName],
) -> RequiredServices:
    """Return a narrowed view over the requested services.

    Args:
        ctx: The current [`ToolContext`][tools.decorator.ToolContext].
        which: Names of the services to require. Every name must be a
            member of [`ServiceName`][tools.services.ServiceName].

    Returns:
        A frozen [`RequiredServices`][tools.services.RequiredServices]
        with the requested fields populated from `ctx` and the
        un-requested fields left `None`.

    Raises:
        ValueError: `which` contains a name not in `ServiceName`.
            Same shape as the decorator's validation.
        ToolNotAvailable: One or more requested services is `None`
            on `ctx` (the dispatcher didn't populate them — usually
            because the tool ran headless or the registry lacked
            the impl).
    """
    valid = set(get_args(ServiceName))
    unknown = [s for s in which if s not in valid]
    if unknown:
        msg = f"Unknown service name(s): {unknown}. Valid: {sorted(valid)}"
        raise ValueError(msg)
    missing = [s for s in which if getattr(ctx, s) is None]
    if missing:
        msg = f"Services not available: {missing}"
        raise ToolNotAvailable(msg)
    return RequiredServices(
        cards=ctx.cards if "cards" in which else None,
        prompts=ctx.prompts if "prompts" in which else None,
        workflows=ctx.workflows if "workflows" in which else None,
        session=ctx.session if "session" in which else None,
    )


__all__ = [
    "AbstractCards",
    "AbstractPrompts",
    "AbstractSessionView",
    "AbstractWorkflows",
    "CardClosed",
    "Cards",
    "PromptBusy",
    "PromptSource",
    "PromptSourceKind",
    "Prompts",
    "RequiredServices",
    "ServiceName",
    "SessionView",
    "ToolNotAvailable",
    "Workflows",
    "require_services",
]

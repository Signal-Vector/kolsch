"""Hello, world — the simplest card-producing tool.

Demonstrates:
- `@tool(requires=["cards"])` — opts the tool into the Cards service.
- `require_services(ctx, ["cards"])` — the recommended narrowing
  helper. Fails loud with ``ToolNotAvailable`` when the tool runs
  headless (`ctx.cards is None`).
- Async-context-manager card usage — auto-close on exit maps normal
  return -> ``success``, exception -> ``error``, cancel ->
  ``cancelled`` (see ``AbstractCard.__aexit__``).
- Streaming markdown appends — chunks arrive in the transcript in
  order.

See ``docs/tutorials/write-a-card-tool.md`` for the walkthrough.
"""

from __future__ import annotations

from pydantic import BaseModel

from tools.decorator import ToolContext, tool
from tools.services import require_services


class HelloCardArgs(BaseModel):
    """No args — the tool renders a fixed three-chunk demo."""


@tool(
    name="hello_card",
    description="Demonstrate a streaming markdown card.",
    args_schema=HelloCardArgs,
    requires=["cards"],
)
async def hello_card(args: HelloCardArgs, ctx: ToolContext) -> dict[str, str]:
    """Open a markdown card, stream three chunks, auto-close success."""
    _ = args
    services = require_services(ctx, ["cards"])
    assert services.cards is not None  # noqa: S101 — type-narrowing after require_services
    async with await services.cards.open("Hello", kind="markdown") as card:
        await card.append("# Hello!\n")
        await card.append("\nStreaming card demo.\n")
        await card.append("\nBye.\n")
    return {"message": "hello card demo complete"}

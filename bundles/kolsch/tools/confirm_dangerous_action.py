"""Ask the user to confirm a dangerous action — the recommended
Prompts pattern.

Demonstrates:
- `@tool(requires=["prompts"])` — opts the tool into the Prompts
  service.
- ``Prompts.choice`` DIRECTLY — NOT ``Card.prompt_choice`` sugar
  (that overload was cut in spec-005; direct is the recommended
  pattern).
- ``PromptSource(kind="tool")`` — identifies this call to the
  slot arbiter. ``id`` is the invocation's ``tool_call_id`` when
  available (engine-driven path); a fresh UUID otherwise (test /
  REPL probes).
- ``PromptBusy`` catch-and-retry — if the slot is occupied,
  ``wait_for_slot(timeout=5)`` blocks up to 5 s then re-issues.
  If still busy the tool returns a structured error dict instead
  of raising.

See ``docs/tutorials/write-a-card-tool.md`` for the walkthrough.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel

from tools.decorator import ToolContext, tool
from tools.services import require_services
from tools.services.exceptions import PromptBusy

if TYPE_CHECKING:
    from tools.services.prompts import PromptSource


class ConfirmArgs(BaseModel):
    """Free-text description shown to the user."""

    action_description: str


def _make_source(ctx: ToolContext) -> PromptSource:
    """Build the ``PromptSource`` for this invocation.

    ``tool_call_id`` is optional (per spec-004's ``NotRequired``
    field); when the engine populated it we thread it through so the
    crash-scan can correlate the request back to the parent
    ``tool.called`` event. Outside the engine (tests, REPL) we omit
    it and let the source stand on a fresh ``id``.
    """
    source: PromptSource = {
        "kind": "tool",
        "id": f"prm_src_{uuid4()}",
    }
    if ctx.tool_call_id is not None:
        source["tool_call_id"] = str(ctx.tool_call_id)
    return source


@tool(
    name="confirm_dangerous_action",
    description="Prompt the user to confirm a dangerous action.",
    args_schema=ConfirmArgs,
    requires=["prompts"],
)
async def confirm_dangerous_action(
    args: ConfirmArgs,
    ctx: ToolContext,
) -> dict[str, object]:
    """Ask a yes/no choice; retry once on ``PromptBusy``."""
    services = require_services(ctx, ["prompts"])
    assert services.prompts is not None  # narrowed by require_services
    source = _make_source(ctx)
    question = f"Confirm: {args.action_description}?"
    options = ["yes", "no"]
    try:
        response = await services.prompts.choice(
            question,
            options,
            source=source,
        )
    except PromptBusy:
        # Wait up to 5 s for the current prompt to clear, then retry
        # once. A second PromptBusy means the slot is stuck; return a
        # structured error rather than raising.
        try:
            await services.prompts.wait_for_slot(source=source, timeout=5)
        except PromptBusy:
            return {
                "user_answered": None,
                "confirmed": False,
                "error": "another prompt was pending too long",
            }
        response = await services.prompts.choice(
            question,
            options,
            source=source,
        )
    return {
        "user_answered": response,
        "confirmed": response == "yes",
    }

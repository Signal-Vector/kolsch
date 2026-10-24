"""Mock file-download demo — the ``progress`` card kind.

Demonstrates:
- `@tool(requires=["cards"])` — same opt-in as ``hello_card``.
- ``progress`` card kind — a determinate ``ProgressBar`` in the
  transcript.
- ``card.set_progress`` in a loop — one call per step; the widget
  layer coalesces frames so a fast loop doesn't spam the bus.
- Async-context-manager auto-close — a raised exception mid-loop
  closes the card as ``error`` with the truncated message; a normal
  return closes as ``success``.

See ``docs/tutorials/write-a-card-tool.md`` for the walkthrough.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from tools.decorator import ToolContext, tool
from tools.services import require_services


class DownloadArgs(BaseModel):
    """Arg shape for the mock download loop."""

    total_kb: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description="Total 'bytes' to download (KB units). Default 100.",
    )
    step_kb: int = Field(
        default=10,
        ge=1,
        description="Per-step increment. Default 10 KB.",
    )


@tool(
    name="download_with_progress",
    description="Simulate a file download with a progress bar.",
    args_schema=DownloadArgs,
    requires=["cards"],
)
async def download_with_progress(
    args: DownloadArgs,
    ctx: ToolContext,
) -> dict[str, int]:
    """Open a progress card; loop with ``set_progress``; auto-close."""
    services = require_services(ctx, ["cards"])
    assert services.cards is not None  # noqa: S101 — type-narrowing after require_services
    async with await services.cards.open(
        f"Downloading {args.total_kb} KB",
        kind="progress",
    ) as card:
        current = 0
        while current < args.total_kb:
            current = min(current + args.step_kb, args.total_kb)
            await card.set_progress(
                current,
                args.total_kb,
                message=f"{current} KB",
            )
            await asyncio.sleep(0.1)
    return {"downloaded_kb": args.total_kb}

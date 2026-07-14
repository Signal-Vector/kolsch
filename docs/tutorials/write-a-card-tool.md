# Write a card-producing tool

Learn how to add a chat tool that renders a rich card in the
transcript — a streaming markdown pane, a progress bar, or an
inline yes/no prompt. Four numbered steps, ~30 lines of Python per
tool.

## Prerequisites

- Your bundle lives under `bundles/<your-name>/tools/` with a
  `bundle.yaml` at the bundle root (see
  [`bundles/kolsch/bundle.yaml`](../../bundles/kolsch/bundle.yaml)
  for the shape).
- You've read [Write a tool](../how-to/write-a-tool.md) — the
  `@tool` decorator basics.
- You know the four v1 card kinds:
  - `markdown` — streaming text.
  - `progress` — determinate / indeterminate progress bar.
  - `confirm` — a static question + inline `[y] [n]` prompt.
  - `workflow` — reserved for the Workflows service (spec-013+).

## 1. Declare `requires=["cards"]`

The `@tool` decorator gains a `requires` kwarg listing the tool
services your body reaches for. Values are typed as
`Literal["cards", "prompts", "workflows", "session"]` — the
decorator raises `ValueError` at import time for anything else, so
typos fail loud.

```python
from pydantic import BaseModel
from tools.decorator import tool


class MyArgs(BaseModel):
    pass


@tool(
    name="my_tool",
    description="Does the thing.",
    args_schema=MyArgs,
    requires=["cards"],  # NEW — opts into the Cards service.
)
async def my_tool(args, ctx):
    ...
```

The dispatcher reads `requires` and populates the matching
`ToolContext` fields (`ctx.cards`, `ctx.prompts`, ...) before your
body runs. Fields you didn't ask for stay `None`.

## 2. Use `require_services` to narrow

`ctx.cards` is typed `Cards | None` because the tool might run
headless (workflow runner, one-shot CLI, tests). Instead of an
`isinstance` check, use the `require_services` helper — it
narrows the union AND fails loud if the platform didn't wire the
service:

```python
from tools.services import require_services


async def my_tool(args, ctx):
    services = require_services(ctx, ["cards"])
    # `services.cards` is now the concrete `Cards` handle. If
    # `ctx.cards` was `None` the helper already raised
    # `ToolNotAvailable` — you never see the None branch.
    await services.cards.notice("starting")
    ...
```

`ToolNotAvailable` is a `PlatformError` — the engine records it as
`tool.failed` with a `not_available_in_headless` hint, so the model
sees a clear signal that this tool isn't callable from a headless
context.

## 3. Open, stream, close

Use `async with` for auto-close on return, exception, or cancel.
The finalizer maps the exit kind onto the card's terminal status:

| Exit path                | `final_status` |
|--------------------------|----------------|
| Normal return            | `success`      |
| `CancelledError`         | `cancelled`    |
| Any other exception      | `error` (with `error_message` truncated to 200 chars) |

### markdown — streaming text

```python
async with await services.cards.open("Report", kind="markdown") as card:
    await card.append("# Findings\n")
    for finding in findings:
        await card.append(f"- {finding}\n")
# Card auto-closes success here.
```

### progress — a determinate bar

```python
async with await services.cards.open(
    "Downloading 100 KB", kind="progress",
) as card:
    for step in range(0, 101, 10):
        await card.set_progress(step, 100, message=f"{step} KB")
        await asyncio.sleep(0.1)
```

Pass `total=0` (or `None`) to render an indeterminate spinner
instead of a filled bar.

### Prompts — direct `Prompts.choice`

The `Card.prompt_choice` sugar was cut in spec-005 — use
`Prompts.choice` directly. Handle `PromptBusy` when another prompt
already holds the slot:

```python
from tools.services.exceptions import PromptBusy
from tools.services.prompts import PromptSource


source: PromptSource = {"kind": "tool", "id": "prm_src_..."}
try:
    response = await services.prompts.choice(
        "Delete file?", ["yes", "no"], source=source,
    )
except PromptBusy:
    await services.prompts.wait_for_slot(source=source, timeout=5)
    response = await services.prompts.choice(
        "Delete file?", ["yes", "no"], source=source,
    )
```

`PromptSource.tool_call_id` is optional — thread `str(ctx.tool_call_id)`
through when the engine populated it so the crash-scan can
correlate the request back to the parent `tool.called` event.

## 4. Where to look next

Runnable examples in this repo:

- [`bundles/kolsch/tools/hello_card.py`](../../bundles/kolsch/tools/hello_card.py)
  — simplest example, streaming markdown.
- [`bundles/kolsch/tools/download_with_progress.py`](../../bundles/kolsch/tools/download_with_progress.py)
  — progress bar in a loop.
- [`bundles/kolsch/tools/confirm_dangerous_action.py`](../../bundles/kolsch/tools/confirm_dangerous_action.py)
  — direct `Prompts.choice` with `PromptBusy` catch-and-retry.

Reference:

- [How-to: Write a tool](../how-to/write-a-tool.md) — decorator +
  args-schema basics, secrets, sandboxing.
- [Textual chat UI roadmap](../../process/roadmap-textual-chat-refactor.md)
  — the wider vision for cards, prompts, workflows.

Need a card kind that isn't in the v1 palette? File an issue with
the shape you want to render, the events it consumes, and a
mockup:

- https://github.com/Signal-Vector/kolsch/issues/new

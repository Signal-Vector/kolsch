"""``pi-platform`` CLI — Step 13.

Per v1-scope §2.4: the developer-facing entry surface for the v1
default tier. Commands:

- ``pi-platform run <agent-ref> [--input key=value ...] [--stream]``
  — single-shot or conversational exchange.
- ``pi-platform sessions list [--starred] [--archived] [--limit N]``
  — list sessions from the entity backend.
- ``pi-platform reconstruct`` — rebuild entity records from JSONL.
- ``pi-platform server [--host H] [--port P]`` — start the FastAPI
  surface (uvicorn).

The CLI is intentionally argparse-only (no click dep) so the
runtime footprint stays lean. Every command routes through
:func:`platform_runtime.bootstrap_runtime`; no logic is duplicated
with :mod:`api`.
"""

from cli.main import main

__all__ = ["main"]

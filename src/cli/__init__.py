"""``kolsch`` CLI — Step 13.

Per v1-scope §2.4: the developer-facing entry surface for the v1
default tier. Commands:

- ``kolsch run <agent-ref> [--input key=value ...] [--stream]``
  — single-shot or conversational exchange.
- ``kolsch sessions list [--starred] [--archived] [--limit N]``
  — list sessions from the entity backend.
- ``kolsch reconstruct`` — rebuild entity records from JSONL.
- ``kolsch server [--host H] [--port P]`` — start the FastAPI
  surface (uvicorn).

The CLI is intentionally argparse-only (no click dep) so the
runtime footprint stays lean. Every command routes through
:func:`platform_runtime.bootstrap_runtime`; no logic is duplicated
with :mod:`api`.
"""

from cli.main import main

__all__ = ["main"]

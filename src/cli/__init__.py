"""`kolsch` CLI — developer-facing entry surface.

Commands:

- `kolsch run <agent-ref> [--input key=value ...] [--stream]` —
  single-shot or conversational exchange.
- `kolsch sessions list [--starred] [--archived] [--limit N]` —
  list sessions from the entity backend.
- `kolsch reconstruct` — rebuild entity records from JSONL.
- `kolsch server [--host H] [--port P]` — start the FastAPI surface
  (uvicorn).

The CLI is intentionally argparse-only (no click dep) so the runtime
footprint stays lean. Every command routes through
`platform_runtime.bootstrap_runtime`; no logic is duplicated with
`api`.
"""

# Must run before any non-stdlib import: on an unsupported interpreter
# (e.g. 3.14, which uv will install into) importing cli.main crashes
# deep inside crewai/chromadb. See cli/_python_support.py.
from cli._python_support import ensure_supported_python

ensure_supported_python()

from cli.main import main  # noqa: E402

__all__ = ["main"]

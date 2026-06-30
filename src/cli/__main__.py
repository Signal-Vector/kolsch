"""Module entry point — enables `python -m cli ...` invocations.

Added for the CLI smoke-test layer: smoke tests use
`subprocess.run([sys.executable, "-m", "cli", ...])` so the full
argparse-to-dispatcher wiring is exercised in a fresh process, not
in-process via `cli.main()` directly. The console script `kolsch`
continues to work as before; this file just adds a parallel entry
point.
"""

from __future__ import annotations

import sys

from cli.main import main

if __name__ == "__main__":
    sys.exit(main())

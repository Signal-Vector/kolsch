"""Platform runtime — the shared composition root for the CLI + API.

Per the v1-scope §2.4 entry surfaces: both ``pi-platform`` (CLI) and
the FastAPI server need the same subsystem wiring. Centralising it
here means there is one place that knows how to stand up the v1
default tier (SQLite + in-process bus + dotenv secrets + native
sandbox + FakeFramework + EchoLLM) with zero external services.

Public surface:

- :class:`PlatformRuntime` — holds the wired subsystems + engine.
- :func:`bootstrap_runtime` — async factory that builds a
  :class:`PlatformRuntime` from a platform root.
"""

from platform_runtime.runtime import PlatformRuntime, bootstrap_runtime

__all__ = ["PlatformRuntime", "bootstrap_runtime"]

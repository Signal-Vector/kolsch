"""Integration-test fixtures + CrewAI environment setup.

- Disables CrewAI's first-run telemetry banner so test logs stay
  clean. CrewAI prints a "Tracing has been disabled / saved your
  preference" panel the first time it runs; setting
  ``CREWAI_TRACING_ENABLED=false`` makes the choice explicit and
  silences the banner across the whole pytest run.
"""

from __future__ import annotations

import os


def pytest_configure() -> None:
    os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")

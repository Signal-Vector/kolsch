"""Test session configuration shared across all layers.

Re-exports the factory fixtures from ``tests.fixtures`` so they are
discovered by pytest without each test module importing them
individually.
"""

from __future__ import annotations

from tests.fixtures.factories import temp_platform_root

__all__ = ["temp_platform_root"]

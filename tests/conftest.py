"""Test session configuration shared across all layers.

Re-exports the factory fixtures from ``tests.fixtures`` so they are
discovered by pytest without each test module importing them
individually.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from tests.fixtures.factories import temp_platform_root

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["temp_platform_root"]


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """Undo root-logger changes made by an in-process `cli.main(argv)` call.

    `cli.main` installs its own handlers (bug-148 `configure_cli_logging`):
    a stderr handler bound to the test's captured stream and a file
    handler on the test's workspace. Left in place, they would outlive
    the test and write to closed streams / stray files in later tests.
    """
    root = logging.getLogger()
    level = root.level
    yield
    for handler in list(root.handlers):
        # Only ours — pytest's own capture handlers come and go per phase.
        if getattr(handler, "_kolsch_cli_handler", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)

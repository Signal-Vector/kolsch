"""Smoke test: every declared subsystem package imports cleanly.

Catches typos in pyproject.toml's `packages = [...]`, a missing
`__init__.py`, and any module-load-time error before more involved
tests would surface it. Cheap insurance per AGENTS.md.
"""

import importlib

import pytest

SUBSYSTEM_PACKAGES = [
    "kernel",
    "errors",
    "schemas",
    "store",
    "bus",
    "assets",
    "providers",
    "tools",
    "skills",
    "engine",
]


@pytest.mark.parametrize("name", SUBSYSTEM_PACKAGES)
def test_subsystem_package_imports(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__name__ == name

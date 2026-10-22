"""The wheel's ``packages`` list must name every top-level package under ``src/``.

Regression: bug-150. ``src/workflows`` was missing from
``[tool.hatch.build.targets.wheel] packages`` for four months, so any
non-editable install (``pip install kolsch-*.whl``, ``uvx``) failed at
``kolsch --help`` with ``ModuleNotFoundError: No module named
'workflows'``. Editable installs (``uv sync``, the dev container, the
commit gate) resolve ``src/`` directly and hid the gap, so no import
test could catch it. This test compares the list with the source tree
instead: no build, no network.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"


def _src_top_level_packages() -> set[str]:
    return {p.name for p in _SRC.iterdir() if (p / "__init__.py").is_file()}


def _wheel_packages() -> set[str]:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    entries: list[str] = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"][
        "packages"
    ]
    return {Path(entry).name for entry in entries}


def test_wheel_packages_equal_src_top_level_packages() -> None:
    src = _src_top_level_packages()
    wheel = _wheel_packages()
    missing = sorted(src - wheel)
    extra = sorted(wheel - src)
    assert not missing and not extra, (
        "pyproject.toml [tool.hatch.build.targets.wheel] packages has drifted "
        f"from src/: missing from wheel = {missing}, extra in wheel "
        f"(no src/<name>/__init__.py) = {extra}. A package missing from the "
        "wheel imports fine in editable installs but breaks every "
        "non-editable install (bug-150)."
    )

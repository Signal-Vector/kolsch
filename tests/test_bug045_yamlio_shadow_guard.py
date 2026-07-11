"""Guard against stale ``_yamlio.py`` in ``site-packages`` shadowing ``src/``.

Regression: bug-045.

``pyproject.toml`` force-includes ``src/_yamlio.py`` at the wheel root
as top-level ``_yamlio.py`` so non-editable installs can resolve
``import _yamlio``. Editable installs (``uv sync``) build a wheel too,
so a physical copy lands in ``site-packages/`` **and** the editable
``.pth`` adds ``src/`` to ``sys.path``. Python's finder resolves
``_yamlio`` to the ``site-packages`` copy first — the ``src/`` entry
never wins.

The failure mode: feat-041 added ``load_with_positions`` to
``src/_yamlio.py``. The user's ``site-packages/_yamlio.py`` was still
the pre-feat-041 version (frozen at the last ``uv sync``). Every
``import _yamlio`` in the codebase resolved to the stale copy, so
``from _yamlio import load_with_positions`` blew ``ImportError`` on
CLI startup — the chat wouldn't launch.

The tests here fail loudly when the two copies drift, guiding the fix:

    rm .venv/lib/python*/site-packages/_yamlio.py
    uv sync

or the durable follow-up (move ``_yamlio`` into a package and drop the
force-include; tracked as a bug-045 follow-up).
"""

from __future__ import annotations

import _yamlio
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_YAMLIO = _REPO_ROOT / "src" / "_yamlio.py"


def test_yamlio_exports_load_with_positions() -> None:
    """feat-041's ``load_with_positions`` must be importable.

    If this fails after pulling a change that touched ``src/_yamlio.py``,
    a stale ``site-packages/_yamlio.py`` is shadowing. Fix:

        rm .venv/lib/python*/site-packages/_yamlio.py && uv sync
    """
    assert hasattr(_yamlio, "load_with_positions"), (
        "_yamlio.load_with_positions is missing. Likely a stale "
        "site-packages/_yamlio.py is shadowing src/_yamlio.py. Fix: "
        "rm .venv/lib/python*/site-packages/_yamlio.py && uv sync"
    )


def test_yamlio_all_matches_src() -> None:
    """``_yamlio.__all__`` must match ``src/_yamlio.py``'s ``__all__``.

    Catches drift where a new export in src/ hasn't propagated to the
    force-included site-packages copy.
    """
    src_source = _SRC_YAMLIO.read_text(encoding="utf-8")
    for expected in ("YAMLError", "dump", "load", "load_with_positions"):
        assert f'"{expected}"' in src_source, (
            f"src/_yamlio.py __all__ missing {expected!r} — "
            "test needs updating alongside the module."
        )
        assert expected in getattr(_yamlio, "__all__", ()), (
            f"_yamlio.__all__ missing {expected!r}. "
            "Shadowed by a stale site-packages copy — see this test's "
            "module docstring for the fix."
        )


def test_yamlio_resolved_module_matches_src_bytes() -> None:
    """Whichever ``_yamlio.py`` Python resolved must match ``src/_yamlio.py``.

    In editable mode Python may resolve to either the ``src/`` copy
    (via the ``.pth``) or a physical ``site-packages/_yamlio.py`` (via
    the force-include). Both are acceptable — but they MUST have
    identical bytes. When they drift the crash surfaces in prod code
    (importers of new exports fail), so we fail the test suite first.
    """
    resolved = Path(_yamlio.__file__).resolve()
    src_expected = _SRC_YAMLIO.resolve()
    if resolved == src_expected:
        return
    resolved_bytes = resolved.read_bytes()
    src_bytes = src_expected.read_bytes()
    assert resolved_bytes == src_bytes, (
        f"_yamlio shadowing drift:\n"
        f"  src/:          {src_expected}\n"
        f"  imported from: {resolved}\n"
        f"Fix: rm {resolved} && uv sync"
    )


def test_cli_import_chain_uses_load_with_positions() -> None:
    """The exact ImportError the user hit — importing the CLI entry
    walks through ``assets.manifest`` which does
    ``from _yamlio import ... load_with_positions``.

    If ``_yamlio`` is shadowed by a stale copy without
    ``load_with_positions``, this import raises ``ImportError`` at
    ``kolsch chat`` startup. This test catches that BEFORE it hits the
    user's terminal.
    """
    from assets import manifest as _manifest  # noqa: F401

    from cli.main import main  # noqa: F401

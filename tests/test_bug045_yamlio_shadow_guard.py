"""Institutional memory of the ``_yamlio`` shadow-drift failure mode.

Regression: bug-045 (diagnosis + CI guard) and bug-049 (durable fix).

**History.** Prior to bug-049, ``src/_yamlio.py`` was force-included
into the built wheel at the top level (via
``[tool.hatch.build.targets.wheel.force-include]`` in
``pyproject.toml``). Editable installs (``uv sync``) build a wheel
too, so a physical ``site-packages/_yamlio.py`` landed alongside the
editable ``.pth`` entry that added ``src/`` to ``sys.path``. Python's
finder resolves top-level modules from ``site-packages`` before
``.pth``-added paths, so the shadow copy won — and any drift between
the two (e.g. a new export like ``load_with_positions`` added on the
src side without a matching wheel reinstall) surfaced as
``ImportError`` at CLI startup.

**Post-bug-049.** The module now lives inside the ``kolsch`` package
(``src/kolsch/_yamlio.py``), which ships automatically via the
existing ``src/kolsch`` entry in the wheel ``packages`` list. No
force-include, no top-level shadow, no drift class. The failure mode
is architecturally gone.

**These tests remain as an import-chain smoke check** — they verify
the ``kolsch._yamlio`` module is importable and exports the expected
API, and that the ``cli.main`` transitive-import chain works. They no
longer detect a live shadow (there is no shadow to detect), but they
still catch a broken import graph or a stale ``__all__`` before a
user hits it at startup.
"""

from __future__ import annotations

from kolsch import _yamlio


def test_yamlio_exports_load_with_positions() -> None:
    """feat-041's ``load_with_positions`` must be importable from
    ``kolsch._yamlio``."""
    assert hasattr(_yamlio, "load_with_positions"), (
        "kolsch._yamlio.load_with_positions is missing — the module's "
        "public API has drifted from what its consumers (feat-041 "
        "authz introspection, assets.manifest, assets.platform_config) "
        "expect."
    )


def test_yamlio_all_matches_src() -> None:
    """``kolsch._yamlio.__all__`` must expose the documented API."""
    for expected in ("YAMLError", "dump", "load", "load_with_positions"):
        assert expected in getattr(_yamlio, "__all__", ()), (
            f"kolsch._yamlio.__all__ missing {expected!r} — public "
            "surface has drifted from consumer expectations."
        )


def test_cli_import_chain_uses_load_with_positions() -> None:
    """The exact ImportError bug-045 caught — importing the CLI entry
    walks through ``assets.manifest`` which does
    ``from kolsch._yamlio import ... load_with_positions``.

    If the import chain breaks (missing export, circular import,
    etc.) this test blows up before the user hits it at
    ``kolsch chat`` startup.
    """
    from assets import manifest as _manifest  # noqa: F401

    from cli.main import main  # noqa: F401

"""Umbrella package — Phase 2.2 ENV-R13 / E-2.2-001 A3.

The ``kolsch`` package exists solely so ``importlib.resources
.files("kolsch")`` resolves. The ``kolsch:`` prefix used in agent
``context_files[].path`` and ``extends:`` references resolves
through that lookup; ``pyproject.toml``'s
``[tool.hatch.build.targets.wheel.force-include]`` section ships the
repo-root ``docs/`` directory and ``AGENTS.md`` into this package
under ``docs/`` and ``AGENTS.md`` respectively, so the same lookup
works in both source-tree dev and installed contexts.

No code lives here — every other subsystem is its own top-level
package under ``src/`` (see ``pyproject.toml``'s ``[tool.hatch.build
.targets.wheel].packages`` list).
"""

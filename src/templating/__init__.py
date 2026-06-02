"""Phase 2.2 templating subsystem.

- Step 2 (this commit): :func:`templating.render.render_prompt` —
  the pure Jinja2 render harness with StrictUndefined.
- Steps 3–7 (subsequent commits): the five variable objects
  (``ToolsVar`` / ``DocsVar`` / ``WorkspaceVar`` / ``AgentVar`` /
  ``PlatformVar``).
- Step 12 (subsequent commit): the ``kolsch:`` / ``workspace:``
  context-file resolver.
- Step 13 (subsequent commit): the ``extends:`` chain resolver
  with cycle detection.

Each step ships its module in isolation; the engine pre-flight
wires them together at Step 14.
"""

from templating.render import render_prompt

__all__ = ["render_prompt"]

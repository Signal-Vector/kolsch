"""Chat CLI concrete service impls (spec-004 through spec-012).

Each module in this package supplies one impl behind a `Prompts` /
`Cards` / `Workflows` / `SessionView` handle registered on
[`ChatServiceRegistry`][cli.chat.service_registry.ChatServiceRegistry]
at `ChatApp.on_mount`. Tools that declare `requires=[...]` receive
these handles via `ToolContext` injection at dispatch time.

spec-004 ships
[`PromptsImpl`][cli.chat.services.prompts_impl.PromptsImpl] — the
first real impl. The other three arrive in spec-006 / spec-012 /
spec-013.
"""

from cli.chat.services.prompts_impl import PromptsImpl

__all__ = ["PromptsImpl"]

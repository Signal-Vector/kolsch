"""Reference providers — register every provider once, here.

Adding a provider is two steps:

1. Create `src/llm/providers/<name>.py` exporting a class that
   implements [`ProviderConnector`][llm.protocol.ProviderConnector].
2. Add a line below:
   `register(<NameConnector>.type_name, <NameConnector>)`.

The registry takes care of the rest.
"""

from llm.providers.claude_code import ClaudeCodeConnector
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector
from llm.registry import register

register(ClaudeCodeConnector.type_name, ClaudeCodeConnector)
register(OpenCodeConnector.type_name, OpenCodeConnector)
register(OpenRouterConnector.type_name, OpenRouterConnector)

__all__ = ["ClaudeCodeConnector", "OpenCodeConnector", "OpenRouterConnector"]

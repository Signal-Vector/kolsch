"""Reference connectors — register every provider once, here.

Adding a provider is two steps:

1. Create ``src/llm/providers/<name>.py`` exporting a class that
   implements :class:`llm.ProviderConnector`.
2. Add a line below: ``register(<NameConnector>.type_name, <NameConnector>)``.

That's it. The registry takes care of the rest.
"""

from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector
from llm.registry import register

register(OpenCodeConnector.type_name, OpenCodeConnector)
register(OpenRouterConnector.type_name, OpenRouterConnector)

__all__ = ["OpenCodeConnector", "OpenRouterConnector"]

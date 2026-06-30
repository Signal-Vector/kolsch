"""JSON Schema export for external consumers.

The published AgentConfig schema artifact — consumed by YAML linters,
the built-in agent, and non-Python clients. Single entry point so
callers never reach into Pydantic directly.
"""

from typing import Any

from schemas.config import ResolvedAgentConfig


def export_json_schema() -> dict[str, Any]:
    """Export the AgentConfig schema as JSON Schema.

    Pure and deterministic: derived from the Pydantic model definition,
    so the same call always returns the same shape.
    """
    return ResolvedAgentConfig.model_json_schema()

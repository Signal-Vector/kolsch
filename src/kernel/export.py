"""JSON Schema export — spec §2.3 + §5.

Phase 1 ships the JSON Schema as a published artifact for external
consumers (YAML linters, the built-in agent, non-Python clients).
This module is the single entry point so callers don't reach into
Pydantic directly.
"""

from typing import Any

from schemas.config import ResolvedAgentConfig


def export_json_schema() -> dict[str, Any]:
    """Export the AgentConfig schema as JSON Schema.

    Pure: derived from the Pydantic model definition. Same call twice
    returns the same shape (§I4-style determinism extended to the
    schema export).
    """
    return ResolvedAgentConfig.model_json_schema()

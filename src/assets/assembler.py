"""Three-layer assembly + prompt pre-resolution — spec §6.

Builds the ordered list of partial configs the kernel resolver merges
(``[system_defaults, agent_definition, call_overrides]``), pre-resolves
prompt refs into actual content (Am-I4), and passes tool/skill refs
through unchanged (Am-I5).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from errors import AssetsPromptLoadFailed

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog


def assemble_partials(
    *,
    defaults: dict[str, Any],
    agent_config: dict[str, Any],
    call_overrides: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Spec §6.1 — the Phase 1 ordered partial list."""
    partials: list[dict[str, Any]] = [defaults, agent_config]
    if call_overrides:
        partials.append(call_overrides)
    return partials


def load_agent_definition(agent_path: Path) -> dict[str, Any]:
    """Read an agent's ``.yaml`` from disk and return its partial dict."""
    payload = yaml.safe_load(agent_path.read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        msg = (
            f"agent file {agent_path} must contain a mapping; "
            f"got {type(payload).__name__}"
        )
        raise TypeError(msg)
    return payload


def pre_resolve_prompts(
    partial: dict[str, Any],
    catalog: AssetCatalog,
) -> dict[str, Any]:
    """Walk ``partial`` and replace each prompt ref with file content.

    Am-I4: prompt refs MUST be resolved before the kernel resolver
    runs. List-shape PromptSpec items that look like a ref
    (slash-delimited path resolving to a catalogued prompt) are
    loaded from disk; non-ref items (or string-shape PromptSpec)
    pass through unchanged.

    Am-I5: tool and skill refs (under ``subsystems.tools`` and
    ``subsystems.skills``) are NOT pre-resolved here — they remain
    refs in the resolved config; the respective subsystems load
    them at engine setup.
    """
    if "prompt" not in partial:
        return partial

    prompt = partial["prompt"]
    if isinstance(prompt, list):
        resolved = [_load_prompt_item(item, catalog) for item in prompt]
        new_partial = dict(partial)
        new_partial["prompt"] = resolved
        return new_partial
    # String-shape PromptSpec passes through unchanged.
    return partial


def _load_prompt_item(item: Any, catalog: AssetCatalog) -> Any:  # noqa: ANN401
    """Resolve one PromptSpec list item.

    Items that don't look like refs (no slash) pass through as inline
    content. Items that look like refs are loaded from disk; an
    unresolvable ref raises :class:`AssetsPromptLoadFailed`.
    """
    if not isinstance(item, str):
        # Already non-string — pass through; downstream validators
        # decide whether to accept it.
        return item
    if "/" not in item:
        return item  # inline content in a list-shape PromptSpec

    entry = catalog.assets.get(item)
    if entry is None or entry.asset_type != "prompt":
        raise AssetsPromptLoadFailed(
            ref=item,
            reason="prompt ref does not resolve to a catalogued prompt",
        )
    try:
        return Path(entry.path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AssetsPromptLoadFailed(ref=item, reason=str(exc)) from exc

"""Three-layer assembly plus prompt pre-resolution.

Builds the ordered list of partial configs the kernel resolver merges
(`[system_defaults, agent_definition, call_overrides]`), pre-resolves
prompt refs into actual file content, and passes tool and skill refs
through unchanged. See the
[asset management contract](../explanation/asset-management-contract.md)
for the assembly ordering and the pre-resolution rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from kolsch._yamlio import YAMLError, load
from assets.refs import is_three_segment, parse_three_segment
from errors import (
    AssetsPromptLoadFailed,
    ConfigInvalid,
    ConfigUnknownAlias,
)

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog


def assemble_partials(
    *,
    defaults: dict[str, Any],
    agent_config: dict[str, Any],
    call_overrides: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build the ordered partial list the kernel resolver consumes.

    Order is `[defaults, agent_config, call_overrides]`; the kernel
    merges them last-wins so an explicit call override beats the
    agent's YAML, which beats the platform defaults. `call_overrides`
    is omitted entirely when falsy so the resolver does not see an
    empty layer.
    """
    partials: list[dict[str, Any]] = [defaults, agent_config]
    if call_overrides:
        partials.append(call_overrides)
    return partials


def load_agent_definition(agent_path: Path) -> dict[str, Any]:
    """Read an agent's `.yaml` from disk and return its partial dict.

    Surfaces malformed-shape failures as
    [`ConfigInvalid`][errors.ConfigInvalid] rather than a raw
    `TypeError` so the public method honors the hard rule that no
    non-`PlatformError` exception escapes.

    Args:
        agent_path: Filesystem path to the agent YAML.

    Returns:
        The parsed top-level mapping, or an empty dict when the file
        parses to `None`.

    Raises:
        ConfigInvalid: The YAML is malformed or its top level is not
            a mapping.
    """
    try:
        payload = load(agent_path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 1,
                    "source": f"agent:{agent_path}",
                    "path": "/",
                    "code": "yaml_parse_error",
                    "message": str(exc),
                }
            ],
            where="assets.assembler.load_agent_definition",
        ) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 1,
                    "source": f"agent:{agent_path}",
                    "path": "/",
                    "code": "not_a_mapping",
                    "message": (
                        f"agent file {agent_path} must contain a mapping; "
                        f"got {type(payload).__name__}"
                    ),
                }
            ],
            where="assets.assembler.load_agent_definition",
        )
    return payload


def pre_resolve_prompts(
    partial: dict[str, Any],
    catalog: AssetCatalog,
) -> dict[str, Any]:
    """Walk `partial` and replace each prompt ref with file content.

    Prompt refs must be resolved before the kernel resolver runs.
    List-shape `PromptSpec` items that parse as a catalogued prompt
    ref are loaded from disk; non-ref items (or string-shape
    `PromptSpec`) pass through unchanged.

    Tool and skill refs (under `subsystems.tools` and
    `subsystems.skills`) are deliberately NOT pre-resolved here. They
    stay as refs in the resolved config; the respective subsystems
    load them at engine setup.

    Args:
        partial: One layer of the assembly chain.
        catalog: The built asset catalog used for ref lookup.

    Returns:
        A new dict with `prompt` entries resolved when applicable;
        the input is not mutated.
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
    """Resolve one `PromptSpec` list item to inline content.

    Ref detection is structural, not heuristic. An item only counts
    as a ref when it parses as a full three-segment
    `<bundle>/<plural>/<name>` AND the middle segment is `prompts`.
    Anything else (free-form text, URLs, accidental slashes) passes
    through as inline content; this avoids the "contains a slash =
    treat as ref" footgun where `["Use https://example.com"]` would
    otherwise hit the catalog lookup. Once an item IS recognised as a
    prompt ref, an unresolvable target raises
    [`AssetsPromptLoadFailed`][errors.AssetsPromptLoadFailed] — the
    caller declared the intent and got it wrong.
    """
    if not isinstance(item, str):
        # Already non-string — pass through; downstream validators
        # decide whether to accept it.
        return item

    if not is_three_segment(item):
        # Not shaped like a 3-seg catalog ref — treat as inline content.
        return item
    _bundle, segment, _name = parse_three_segment(item)
    if segment != "prompts":
        return item  # references some other asset type; not for us.

    entry = catalog.lookup(item, expected_type="prompt")
    if entry is None:
        raise AssetsPromptLoadFailed(
            ref=item,
            reason="prompt ref does not resolve to a catalogued prompt",
        )
    try:
        return Path(entry.path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AssetsPromptLoadFailed(ref=item, reason=str(exc)) from exc


def pre_resolve_model(
    partial: dict[str, Any],
    aliases: dict[str, str] | None,
) -> dict[str, Any]:
    """Normalize `partial['model']` into a canonical `{provider, model}`.

    `ModelSpec` accepts three input shapes:

    - Object: `{provider, model, parameters?, fallback?}` — pass-through.
    - Compact string: `"opencode/gpt-oss-120b"` — split on first `/`.
    - Alias string: `"smart"` — look up in `aliases` (one level only;
      aliases don't chain). Unknown alias raises
      [`ConfigUnknownAlias`][errors.ConfigUnknownAlias].

    `aliases` is the `model_aliases` section from `platform.yaml`
    (mapping alias to canonical string). `None` is treated as `{}` —
    a bare alias-shaped string then surfaces as
    [`ConfigUnknownAlias`][errors.ConfigUnknownAlias]. Nested
    `fallback` entries are recursively normalized so a
    `fallback: ["cheap"]` declaration also resolves cleanly.
    `partial['fallback_models']` (the agent-level chain) is
    normalized the same way when present; each entry accepts the
    same three input shapes.

    Args:
        partial: One layer of the assembly chain.
        aliases: The alias-to-canonical-string map from
            `platform.yaml`; `None` is treated as empty.

    Returns:
        A new dict; the input is not mutated.

    Raises:
        ConfigUnknownAlias: An alias string has no entry in `aliases`.
        ConfigInvalid: A compact string is malformed, or an alias
            target is itself alias-shaped (chains are forbidden).
    """
    if "model" not in partial and "fallback_models" not in partial:
        return partial
    aliases = aliases or {}
    new_partial = dict(partial)
    if "model" in partial:
        new_partial["model"] = _normalize_model_value(partial["model"], aliases)
    if "fallback_models" in partial:
        raw = partial["fallback_models"]
        if isinstance(raw, list):
            new_partial["fallback_models"] = [
                _normalize_model_value(entry, aliases) for entry in raw
            ]
        # Else: pass through; downstream Pydantic raises ConfigInvalid.
    return new_partial


def _normalize_model_value(
    value: object,
    aliases: dict[str, str],
) -> object:
    """Turn one model value into canonical `{provider, model, ...}` form."""
    if isinstance(value, str):
        return _expand_string_model(value, aliases)
    if not isinstance(value, dict):
        # Some other shape — pass through; downstream Pydantic will
        # raise the appropriate ConfigInvalid.
        return value
    out = dict(value)
    fallback = out.get("fallback")
    if isinstance(fallback, list):
        out["fallback"] = [_normalize_model_value(item, aliases) for item in fallback]
    return out


def _expand_string_model(
    raw: str,
    aliases: dict[str, str],
) -> dict[str, Any]:
    """Expand a compact `provider/model` string or an alias label."""
    if "/" in raw:
        provider, _, model = raw.partition("/")
        if not provider or not model:
            raise ConfigInvalid(
                validation_errors=[
                    {
                        "layer": 1,
                        "source": "agent.model",
                        "path": "/model",
                        "code": "malformed_compact_model",
                        "message": (
                            f"compact model string {raw!r} must be "
                            "'provider/model' with both segments populated"
                        ),
                    }
                ],
                where="assets.assembler.pre_resolve_model",
            )
        return {"provider": provider, "model": model}
    if raw not in aliases:
        raise ConfigUnknownAlias(
            alias=raw,
            available_aliases=sorted(aliases),
        )
    expanded = aliases[raw]
    if "/" not in expanded:
        # Alias target is itself an alias label — aliases don't chain.
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 1,
                    "source": "platform.model_aliases",
                    "path": f"/model_aliases/{raw}",
                    "code": "alias_chain_forbidden",
                    "message": (
                        f"alias {raw!r} resolves to {expanded!r} which "
                        "is not a 'provider/model' string; aliases don't chain"
                    ),
                }
            ],
            where="assets.assembler.pre_resolve_model",
        )
    provider, _, model = expanded.partition("/")
    return {"provider": provider, "model": model}

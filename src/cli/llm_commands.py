"""`kolsch connect / disconnect / providers / refresh` CLI surface.

Lives outside `cli.main` so the heavy interactive + filesystem
orchestration stays separate from the argparse skeleton.

Invariants:

- API key values never touch `platform.yaml`. The `api_key_secret`
  field carries the secret name; the value lives only in `.env`.
- `platform.yaml` writes are atomic — temp file + fsync + rename —
  so a partial write never leaves a corrupt file on disk.
- `.env` partial updates preserve every entry that isn't the secret
  this command is touching.
- A failed connect leaves both `platform.yaml` and `.env` unchanged
  (writes happen only after every validation + discovery succeeds).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

from errors import (
    LlmConnectorUnknown,
    LlmDiscoveryFailed,
    LlmValidationFailed,
)
from kolsch._yamlio import load
from llm import (
    Credentials,
    ProviderConfig,
    connected_metadata_sources,
    known_types,
    provider_config_from_yaml,
)
from llm import (
    get as registry_get,
)
from llm.protocol import is_metadata_source

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from llm.protocol import (
        MetadataSourceConnector,
        ModelEntry,
        PromptField,
        ProviderConnector,
    )


# ---------------------------------------------------------------------------
# Prompter — test-injection seam for interactive input
# ---------------------------------------------------------------------------


class Prompter(Protocol):
    """One-method protocol: read one prompt's value.

    Production wires [`StdinPrompter`][cli.llm_commands.StdinPrompter];
    tests pass a stub that returns canned answers.
    """

    def ask(self, field: PromptField) -> str:
        """Read the value for one prompt field and return it."""


class StdinPrompter:
    """Production prompter — reads from stdin.

    Uses `getpass` for secret fields so values don't echo to the
    terminal.
    """

    def ask(self, field: PromptField) -> str:
        """Read the value for one prompt field from stdin."""
        suffix = f" [{field.default}]" if field.default else ""
        label = f"{field.label}{suffix}: "
        if field.kind == "secret":
            value = getpass(f"Enter {field.label.lower()}: ")
        else:
            value = input(label)
        value = value.strip()
        if not value and field.default is not None:
            return field.default
        return value


@dataclass(frozen=True)
class _CommandIO:
    """The four collaborators every command takes.

    Bagged together so tests can swap them without 6-positional-arg
    signatures.
    """

    root: Path
    out: Any  # file-like; defaults to sys.stdout in main.py
    prompter: Prompter
    connector_factory: Callable[[str], ProviderConnector]


# ---------------------------------------------------------------------------
# Atomic platform.yaml + partial .env writers
# ---------------------------------------------------------------------------


def _load_platform_yaml(root: Path) -> dict[str, Any]:
    path = root / "platform.yaml"
    if not path.exists():
        return {}
    payload = load(path.read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        msg = f"platform.yaml top-level must be a mapping; got {type(payload).__name__}"
        raise TypeError(msg)
    return payload


def _write_platform_yaml_atomic(
    root: Path, change: Callable[[dict[str, Any]], bool]
) -> bool:
    """Apply `change` to `platform.yaml` under the rules lock; atomic write.

    bug-146: the commands below compute their change from a read taken
    before slow provider validation / discovery. Re-dumping *that* copy
    would drop any rule a chat "always" wrote meanwhile, so the change
    is re-applied to a fresh read taken under
    [`platform_yaml_lock`][assets.platform_config.platform_yaml_lock],
    via
    [`rewrite_platform_yaml_locked`][cli._platform_yaml_write.rewrite_platform_yaml_locked]
    (temp file → fsync → atomic rename; on failure the original file
    is untouched). `change` returns `False` when nothing changes.

    qol-133: this is the single write chokepoint shared by
    `cmd_connect`/`cmd_disconnect`/`cmd_refresh` — all three read
    `platform.yaml` as a plain dict via `_load_platform_yaml` (backed
    by `kolsch._yamlio.load`, which has no comment-preservation) and
    end up here to write it back. Uses
    `dump_preserving_trailing_comment_block` (not raw `dump()`) so a
    trailing, fully-commented scaffold block already on disk — e.g.
    the fresh-install `hooks:` example — survives a standalone
    `kolsch connect`/`disconnect`/`refresh` run against a workspace
    that still has it, not just a `kolsch setup` re-run. No-op when
    there's no such block (including first-ever write, when the file
    doesn't exist yet).
    """
    from cli._platform_yaml_write import rewrite_platform_yaml_locked  # noqa: PLC0415

    root.mkdir(parents=True, exist_ok=True)
    return rewrite_platform_yaml_locked(root, change)


def _load_dotenv_lines(env_path: Path) -> list[str]:
    if not env_path.exists():
        return []
    return env_path.read_text(encoding="utf-8").splitlines()


def _set_dotenv_entry(root: Path, key: str, value: str) -> None:
    """Set `key=value` in `.env`, preserving every other line.

    Comments + blank lines are preserved verbatim. An existing key is
    rewritten in place; a new key is appended. Value is quoted with
    double quotes when it contains whitespace or shell-special chars.
    """
    env_path = root / ".env"
    lines = _load_dotenv_lines(env_path)
    quoted = _quote_value(value)
    new_line = f"{key}={quoted}"
    rewritten = False
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        existing_key, _, _ = stripped.partition("=")
        if existing_key.strip() == key:
            out.append(new_line)
            rewritten = True
        else:
            out.append(raw)
    if not rewritten:
        out.append(new_line)
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _remove_dotenv_entry(root: Path, key: str) -> bool:
    """Remove `key=...` from `.env` if present.

    Returns True when something was removed.
    """
    env_path = root / ".env"
    if not env_path.exists():
        return False
    lines = _load_dotenv_lines(env_path)
    removed = False
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if "=" not in stripped or stripped.startswith("#"):
            out.append(raw)
            continue
        existing_key, _, _ = stripped.partition("=")
        if existing_key.strip() == key:
            removed = True
            continue
        out.append(raw)
    if removed:
        env_path.write_text(
            ("\n".join(out) + "\n") if out else "",
            encoding="utf-8",
        )
    return removed


def _quote_value(value: str) -> str:
    if value and all(c.isalnum() or c in "-_./" for c in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# connect <provider>
# ---------------------------------------------------------------------------


_PLANNED_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "openai", "bedrock", "llama"}
)


async def cmd_connect(args: argparse.Namespace, io: _CommandIO) -> int:
    """Run the interactive connect flow for one provider."""
    provider_name = args.provider
    try:
        connector = io.connector_factory(provider_name)
    except LlmConnectorUnknown:
        if provider_name in _PLANNED_PROVIDERS:
            io.out.write(
                f"The {provider_name!r} provider is planned for a future "
                f"patch. See docs/explanation/llm-providers.md for the "
                f"design sketch. If you'd like to contribute the "
                f"implementation, see src/llm/providers/README.md for "
                f"the pattern.\n",
            )
            return 1
        io.out.write(
            f"unknown provider: {provider_name!r}\n"
            f"known providers: {', '.join(known_types())}\n",
        )
        return 1

    responses = _gather_responses(connector, io)
    if responses is None:
        return 1
    base_url = responses.get("base_url")
    credentials = Credentials(values=dict(responses))

    if not await _run_validation(connector, credentials, base_url, io, provider_name):
        return 1

    entries = await _run_discovery(connector, credentials, base_url, io)
    if entries is None:
        return 1

    existing_payload = _load_platform_yaml(io.root)
    existing_providers = _provider_configs_from_payload(existing_payload)
    entries = await _maybe_augment(
        entries, existing_providers, connector, provider_name, io
    )

    new_entry = _build_provider_entry(connector, base_url, entries, provider_name)
    api_key_value = responses.get("api_key", "")
    if api_key_value:
        _set_dotenv_entry(io.root, new_entry["api_key_secret"], api_key_value)
        io.out.write(f"✓ wrote {new_entry['api_key_secret']} to .env\n")

    # spec-118 (M1): persist an operator-provided binary-path override so it is
    # honored at runtime (round-trips into `ProviderConfig.extras`, read by the
    # connector's `build_llm`). Guarded on the connector actually prompting for
    # it, so connectors without a `binary_path` field are unaffected.
    binary_path = responses.get("binary_path")
    if binary_path:
        new_entry["binary_path"] = binary_path

    def add_provider(payload: dict[str, Any]) -> bool:
        llm_providers = dict(payload.get("llm_providers") or {})
        llm_providers[provider_name] = new_entry
        payload["llm_providers"] = llm_providers
        return True

    _write_platform_yaml_atomic(io.root, add_provider)
    io.out.write("✓ wrote provider config to platform.yaml\n")
    return 0


def _gather_responses(
    connector: ProviderConnector,
    io: _CommandIO,
) -> dict[str, str] | None:
    """Walk the connector's `auth_prompt_spec` and collect responses.

    Returns `None` when a required field came back empty.
    """
    spec = connector.auth_prompt_spec()
    io.out.write(f"Connecting {spec.provider_label}...\n\n")
    io.out.write(spec.intro + "\n\n")
    responses: dict[str, str] = {}
    for field in spec.fields:
        value = io.prompter.ask(field)
        if not value and field.required:
            io.out.write(f"error: {field.label} is required\n")
            return None
        responses[field.name] = value
    return responses


async def _run_validation(
    connector: ProviderConnector,
    credentials: Credentials,
    base_url: str | None,
    io: _CommandIO,
    provider_name: str,
) -> bool:
    io.out.write("\nValidating credentials...\n")
    try:
        result = await connector.validate_credentials(credentials, base_url)
    except Exception as exc:  # noqa: BLE001 — surface as user-facing error
        io.out.write(f"error: validation crashed: {exc}\n")
        return False
    if not result.ok:
        io.out.write(
            f"error: credentials rejected: {result.reason or 'unknown reason'}\n"
        )
        # Construct (don't raise) so the catalog code is exercised in
        # tests and the failure path stays auditable.
        _ = LlmValidationFailed(
            provider=provider_name, reason=result.reason or "unknown"
        )
        return False
    io.out.write("✓ credentials valid\n")
    if result.metadata:
        for key, val in sorted(result.metadata.items()):
            io.out.write(f"  {key}: {val}\n")
    return True


async def _run_discovery(
    connector: ProviderConnector,
    credentials: Credentials,
    base_url: str | None,
    io: _CommandIO,
) -> list[ModelEntry] | None:
    io.out.write("\nDiscovering models...\n")
    try:
        entries = await connector.discover_models(credentials, base_url)
    except LlmDiscoveryFailed as exc:
        io.out.write(f"error: discovery failed: {exc.detail['reason']}\n")
        return None
    io.out.write(f"✓ found {len(entries)} models\n")
    return entries


async def _maybe_augment(
    entries: list[ModelEntry],
    existing_providers: dict[str, ProviderConfig],
    connector: ProviderConnector,
    provider_name: str,
    io: _CommandIO,
) -> list[ModelEntry]:
    sources = _augmentation_sources(
        existing_providers,
        skip_type=connector.type_name,
        connector_factory=io.connector_factory,
    )
    if sources and any(e.has_missing_fields() for e in entries):
        io.out.write("Augmenting from metadata sources...\n")
        entries = await _augment_entries(entries, sources, provider_name)
        io.out.write("✓ augmented\n")
    return entries


def _build_provider_entry(
    connector: ProviderConnector,
    base_url: str | None,
    entries: list[ModelEntry],
    provider_name: str,
) -> dict[str, Any]:
    new_entry: dict[str, Any] = {"type": connector.type_name}
    # Null-key connect (spec-118, general): only write `api_key_secret`
    # when the connector actually declares a `kind="secret"` field. A
    # subscription/keyless provider (e.g. `claude_code`) has none, so the
    # written entry omits the key entirely — `build_llm`'s resolve branch
    # is then skipped at runtime and no `LlmSecretMissing` is raised. Any
    # future keyless provider benefits; keyed providers are unchanged.
    if any(f.kind == "secret" for f in connector.auth_prompt_spec().fields):
        new_entry["api_key_secret"] = _secret_name_for(provider_name)
    if base_url:
        new_entry["base_url"] = base_url
    if is_metadata_source(connector):
        new_entry["serves_as_metadata_source"] = True
    new_entry["models"] = [_model_entry_to_yaml(e) for e in entries]
    return new_entry


# ===========================================================================
# disconnect <provider>
# ===========================================================================


async def cmd_disconnect(args: argparse.Namespace, io: _CommandIO) -> int:
    """Remove a provider from `platform.yaml` and erase its `.env` secret."""
    provider_name = args.provider
    payload = _load_platform_yaml(io.root)
    providers = payload.get("llm_providers") or {}
    if provider_name not in providers:
        io.out.write(f"provider {provider_name!r} is not connected\n")
        return 1
    existing = providers[provider_name]
    secret_name = existing.get("api_key_secret") if isinstance(existing, dict) else None

    def remove_provider(fresh: dict[str, Any]) -> bool:
        fresh_providers = fresh.get("llm_providers") or {}
        if provider_name not in fresh_providers:
            return False
        new_providers = {k: v for k, v in fresh_providers.items() if k != provider_name}
        if new_providers:
            fresh["llm_providers"] = new_providers
        else:
            fresh.pop("llm_providers", None)
        return True

    _write_platform_yaml_atomic(io.root, remove_provider)
    io.out.write(f"✓ removed {provider_name} from platform.yaml\n")

    if secret_name and _remove_dotenv_entry(io.root, secret_name):
        io.out.write(f"✓ removed {secret_name} from .env\n")
    return 0


# ===========================================================================
# `providers` — list registered connector types + connected providers
# ===========================================================================


async def cmd_providers(args: argparse.Namespace, io: _CommandIO) -> int:
    """Print registered connector types and currently connected providers."""
    _ = args
    payload = _load_platform_yaml(io.root)
    providers = payload.get("llm_providers") or {}
    io.out.write("Registered connector types:\n")
    for type_name in known_types():
        io.out.write(f"  - {type_name}\n")
    io.out.write("\nConnected providers (from platform.yaml):\n")
    if not providers:
        io.out.write("  (none)\n")
        return 0
    for name in sorted(providers):
        entry = providers[name]
        if not isinstance(entry, dict):
            io.out.write(f"  - {name}: (malformed entry)\n")
            continue
        type_name = entry.get("type", "?")
        models = entry.get("models") or []
        model_count = len(models) if isinstance(models, list) else 0
        flags = []
        if entry.get("serves_as_metadata_source"):
            flags.append("metadata-source")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        io.out.write(f"  - {name} (type={type_name}, models={model_count}){flag_str}\n")
    return 0


# ===========================================================================
# `models` — list discovered models per connected provider
# ===========================================================================


async def cmd_models(args: argparse.Namespace, io: _CommandIO) -> int:
    """List discovered models from platform.yaml's `llm_providers` block.

    Data source: each provider's pre-discovered `models` list (populated
    by `connect` / `refresh`). No live API calls.
    """
    payload = _load_platform_yaml(io.root)
    providers = payload.get("llm_providers") or {}
    aliases = payload.get("model_aliases") or {}

    requested: str | None = getattr(args, "provider", None)
    verbose: bool = bool(getattr(args, "verbose", False))
    filt: str | None = getattr(args, "filter", None)
    as_json: bool = bool(getattr(args, "json_output", False))

    # Validate the optional <provider> argument.
    if requested is not None:
        if requested not in known_types():
            io.out.write(
                f"unknown provider: {requested!r}\n"
                f"registered: {', '.join(known_types())}\n"
            )
            return 1
        if requested not in providers:
            io.out.write(
                f"provider {requested!r} is registered but not connected. "
                f"Run `kolsch connect {requested}`.\n"
            )
            return 1
        target_names: list[str] = [requested]
    else:
        target_names = sorted(providers)

    if not target_names:
        io.out.write("no providers connected — run `kolsch connect <provider>`.\n")
        io.out.write(f"registered: {', '.join(known_types())}\n")
        return 0

    if as_json:
        _emit_models_json(io, providers, target_names, aliases, filt)
        return 0

    return _emit_models_text(
        io,
        providers=providers,
        target_names=target_names,
        aliases=aliases,
        filt=filt,
        verbose=verbose,
    )


def _alias_lookup(aliases: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Build a reverse index `(provider, model) → alias`.

    Source is `platform.yaml`'s `model_aliases`. Aliases are
    deployment-shareable strings users can type into agent YAML; the
    `models` listing prefers them when shown.
    """
    out: dict[tuple[str, str], str] = {}
    for alias, raw in aliases.items():
        if not isinstance(raw, str) or "/" not in raw:
            continue
        provider, _, model = raw.partition("/")
        out.setdefault((provider, model), alias)
    return out


def _emit_models_text(
    io: _CommandIO,
    *,
    providers: dict[str, Any],
    target_names: list[str],
    aliases: dict[str, Any],
    filt: str | None,
    verbose: bool,
) -> int:
    alias_by_pair = _alias_lookup(aliases)
    rendered_any = False
    for name in target_names:
        entry = providers[name]
        if not isinstance(entry, dict):
            io.out.write(f"{name}: (malformed entry)\n")
            continue
        raw_models = entry.get("models") or []
        if not isinstance(raw_models, list):
            io.out.write(f"{name}: (malformed models field)\n")
            continue
        flag_str = ""
        if verbose and entry.get("serves_as_metadata_source"):
            flag_str = " [metadata-source]"
        # Filter in memory.
        filtered = [
            m
            for m in raw_models
            if isinstance(m, dict)
            and isinstance(m.get("id"), str)
            and (filt is None or filt.lower() in m["id"].lower())
        ]
        header = f"{name} ({len(filtered)} model"
        header += "" if len(filtered) == 1 else "s"
        header += f"){flag_str}\n"
        io.out.write(header)
        if not filtered:
            if filt is not None:
                io.out.write(f"  (no matches for {filt!r})\n")
            elif not raw_models:
                io.out.write(
                    f"  (no models discovered — run `kolsch refresh {name}`)\n"
                )
            else:
                io.out.write("  (no models)\n")
            io.out.write("\n")
            continue
        rendered_any = True
        for m in filtered:
            model_id = m["id"]
            alias = alias_by_pair.get((name, model_id))
            alias_str = f"   alias: {alias}" if alias else ""
            io.out.write(f"  {model_id}{alias_str}\n")
            if verbose:
                _emit_verbose_fields(io, m)
        io.out.write("\n")

    if rendered_any:
        io.out.write(
            "To use one in an agent:\n"
            "    model: { provider: <provider>, model: <model_id> }\n"
            "  or with an alias:\n"
            "    model: <alias>\n"
        )
        if filt is None:
            io.out.write(
                "\nHint: `kolsch models --filter <substring>` to narrow "
                "long catalogs.\n"
            )
    return 0


def _emit_verbose_fields(io: _CommandIO, m: dict[str, Any]) -> None:
    """Render `--verbose` extras for one model entry."""
    ctx = m.get("context_length")
    tools = m.get("supports_tools")
    stream = m.get("supports_streaming")
    pricing = m.get("pricing")
    ctx_str = str(ctx) if ctx is not None else "?"
    tools_str = "yes" if tools is True else "no" if tools is False else "?"
    stream_str = "yes" if stream is True else "no" if stream is False else "?"
    io.out.write(
        f"    context_length: {ctx_str}   tools: {tools_str}   "
        f"streaming: {stream_str}\n"
    )
    if isinstance(pricing, dict):
        ppm = pricing.get("prompt_per_million")
        cpm = pricing.get("completion_per_million")
        if ppm is not None or cpm is not None:
            io.out.write(f"    pricing: prompt=${ppm}/M  completion=${cpm}/M\n")


def _emit_models_json(
    io: _CommandIO,
    providers: dict[str, Any],
    target_names: list[str],
    aliases: dict[str, Any],
    filt: str | None,
) -> None:
    import json  # noqa: PLC0415

    alias_by_pair = _alias_lookup(aliases)
    out: dict[str, Any] = {}
    for name in target_names:
        entry = providers[name]
        if not isinstance(entry, dict):
            continue
        raw_models = entry.get("models") or []
        if not isinstance(raw_models, list):
            raw_models = []
        rendered: list[dict[str, Any]] = []
        for m in raw_models:
            if not isinstance(m, dict):
                continue
            model_id = m.get("id")
            if not isinstance(model_id, str):
                continue
            if filt is not None and filt.lower() not in model_id.lower():
                continue
            row = dict(m)
            alias = alias_by_pair.get((name, model_id))
            if alias is not None:
                row["alias"] = alias
            rendered.append(row)
        out[name] = {
            "models": rendered,
            "serves_as_metadata_source": bool(entry.get("serves_as_metadata_source")),
        }
    io.out.write(json.dumps(out, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# refresh [<provider>]
# ---------------------------------------------------------------------------


async def cmd_refresh(args: argparse.Namespace, io: _CommandIO) -> int:
    """Re-run discovery against connected providers and rewrite their model lists.

    Self-heal: refreshed entries are passed through :func:`_maybe_augment`
    (same helper ``cmd_connect`` calls) so a provider without
    ``context_length`` metadata picks up values from any connected
    :class:`MetadataSourceConnector`. Additionally, any provider block
    whose connector qualifies as a metadata source but lacks the
    ``serves_as_metadata_source: true`` marker gets it written back
    idempotently — closes the drift for workspaces connected before
    the marker was introduced.
    """
    payload = _load_platform_yaml(io.root)
    providers = payload.get("llm_providers") or {}
    if not providers:
        io.out.write("no connected providers to refresh\n")
        return 1
    secrets = _build_refresh_secrets(io.root, payload)
    targets = [args.provider] if args.provider else sorted(providers)
    for name in targets:
        if name not in providers:
            io.out.write(f"provider {name!r} is not connected; skipping\n")
            continue
        entry = providers[name]
        if not isinstance(entry, dict):
            io.out.write(f"provider {name!r} entry is malformed; skipping\n")
            continue
        try:
            connector = io.connector_factory(entry.get("type", ""))
        except LlmConnectorUnknown:
            io.out.write(
                f"provider {name!r} references unknown type "
                f"{entry.get('type')!r}; skipping\n"
            )
            continue

        # Resolve the API key from the platform's Secrets provider —
        # the same one the engine would use. We instantiate Secrets
        # directly (rather than spinning up the full runtime) so
        # `refresh` only requires the `secrets` section to be valid.
        secret_name = entry.get("api_key_secret")
        api_key: str | None = None
        if secret_name:
            api_key = await secrets.resolve(secret_name)
        credentials = Credentials(
            values={"api_key": api_key or "", "base_url": entry.get("base_url", "")}
        )
        try:
            entries = await connector.discover_models(
                credentials,
                entry.get("base_url"),
            )
        except LlmDiscoveryFailed as exc:
            io.out.write(f"error refreshing {name}: {exc.detail['reason']}\n")
            continue
        io.out.write(f"✓ {name}: {len(entries)} models\n")

        # Self-heal: augment refreshed entries from any connected
        # metadata source (typically OpenRouter). Uses the caller's
        # current payload as the source-of-truth for who else is
        # connected — same shape `cmd_connect` uses.
        existing_providers = _provider_configs_from_payload(payload)
        entries = await _maybe_augment(entries, existing_providers, connector, name, io)

        entry["models"] = [_model_entry_to_yaml(e) for e in entries]

    # Retroactive marker sweep. For any provider block whose connector
    # qualifies as a metadata source but lacks the flag, add it. This
    # heals workspaces connected before the marker was introduced and
    # is idempotent — re-runs write no diff.
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("serves_as_metadata_source"):
            continue
        try:
            connector = io.connector_factory(entry.get("type") or name)
        except LlmConnectorUnknown:
            continue
        if is_metadata_source(connector):
            entry["serves_as_metadata_source"] = True
            io.out.write(f"✓ {name}: marked as metadata source\n")

    providers_after = dict(providers)

    def apply_refresh(fresh: dict[str, Any]) -> bool:
        # Re-apply the refreshed blocks to the fresh copy; providers
        # connected or disconnected meanwhile are left as they are.
        fresh_providers = fresh.get("llm_providers")
        if not isinstance(fresh_providers, dict):
            return False
        for name, entry in providers_after.items():
            if name in fresh_providers:
                fresh_providers[name] = entry
        return True

    _write_platform_yaml_atomic(io.root, apply_refresh)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_refresh_secrets(root: Path, payload: dict[str, Any]) -> Any:
    """Instantiate a Secrets provider from platform.yaml's `secrets` section.

    Used by `refresh` so the command doesn't require a full runtime
    bootstrap. Defaults to the dotenv variant when no `secrets`
    section exists — matches first-run platform defaults.
    """
    from providers import make_secrets_provider  # noqa: PLC0415

    secrets_cfg = payload.get("secrets") or {
        "variant": "dotenv",
        "config": {"path": ".env"},
    }
    return make_secrets_provider(root=root, config=secrets_cfg)


def _secret_name_for(provider_name: str) -> str:
    """Convention: `<PROVIDER>_API_KEY` (uppercase, hyphens → underscores)."""
    sanitized = provider_name.upper().replace("-", "_").replace(".", "_")
    return f"{sanitized}_API_KEY"


def _provider_configs_from_payload(
    payload: dict[str, Any],
) -> dict[str, ProviderConfig]:
    """Build ProviderConfig objects from the raw `llm_providers` map."""
    out: dict[str, ProviderConfig] = {}
    for name, raw in (payload.get("llm_providers") or {}).items():
        if not isinstance(raw, dict):
            continue
        # bug-144: the ONE shared factory (also used by
        # `platform_runtime.runtime`) — applies the bug-055 `type:` default
        # and validates `request_policy`.
        out[name] = provider_config_from_yaml(name, raw)
    return out


def _augmentation_sources(
    existing_providers: dict[str, ProviderConfig],
    *,
    skip_type: str,
    connector_factory: Callable[[str], ProviderConnector],
) -> list[MetadataSourceConnector]:
    """Return connected metadata sources, excluding any whose type matches `skip_type`.

    Skipping by type ensures connecting OpenRouter doesn't try to
    augment itself from itself.
    """
    candidates = connected_metadata_sources(existing_providers)
    # `connected_metadata_sources` returns fresh instances from the
    # registry's get(); replace with factory-built instances so tests
    # injecting transports get hit.
    sources: list[MetadataSourceConnector] = []
    for instance in candidates:
        if instance.type_name == skip_type:
            continue
        try:
            replacement = connector_factory(instance.type_name)
        except LlmConnectorUnknown:
            continue
        if is_metadata_source(replacement):
            sources.append(replacement)  # type: ignore[arg-type]
    return sources


async def _augment_entries(
    entries: list[ModelEntry],
    sources: list[MetadataSourceConnector],
    provider_name: str,
) -> list[ModelEntry]:
    """Walk each source for each entry with missing fields; first hit wins."""
    from llm.protocol import ModelEntry  # noqa: PLC0415

    augmented: list[ModelEntry] = []
    for entry in entries:
        if not entry.has_missing_fields():
            augmented.append(entry)
            continue
        new_entry = entry
        for source in sources:
            metadata = await source.lookup_model_metadata(provider_name, entry.id)
            if metadata is None:
                continue
            new_entry = ModelEntry(
                id=entry.id,
                context_length=metadata.context_length,
                supports_tools=metadata.supports_tools,
                supports_streaming=metadata.supports_streaming,
                pricing=metadata.pricing,
            )
            break
        augmented.append(new_entry)
    return augmented


def _model_entry_to_yaml(entry: ModelEntry) -> dict[str, Any]:
    """Serialize a `ModelEntry` to the `platform.yaml` row shape."""
    out: dict[str, Any] = {"id": entry.id}
    if entry.context_length is not None:
        out["context_length"] = entry.context_length
    if entry.supports_tools is not None:
        out["supports_tools"] = entry.supports_tools
    if entry.supports_streaming is not None:
        out["supports_streaming"] = entry.supports_streaming
    if entry.pricing is not None:
        out["pricing"] = {
            "prompt_per_million": entry.pricing.prompt_per_million,
            "completion_per_million": entry.pricing.completion_per_million,
        }
    return out


def make_default_io(
    *,
    root: Path,
    out: TextIO | None = None,
) -> _CommandIO:
    """Build a production `_CommandIO` wired to stdin + the LLM registry.

    Combines [`StdinPrompter`][cli.llm_commands.StdinPrompter] with
    the registry's `get` as the `connector_factory`.
    """
    return _CommandIO(
        root=root,
        out=out if out is not None else sys.stdout,
        prompter=StdinPrompter(),
        connector_factory=registry_get,
    )

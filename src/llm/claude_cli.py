"""Shared `claude` CLI mechanics — env scrub, argv prefix, result extraction.

Extracted from `llm.providers.claude_code` (spec-118) by spec-121 so
BOTH consumers of the local `claude` binary share one source of truth:

- the **provider** connector
  ([`ClaudeCodeConnector`][llm.providers.claude_code.ClaudeCodeConnector],
  text-only `claude -p`, spec-118) — appends `--disallowedTools "*"`;
- the **framework** (`SubprocessAgentFramework` + `ClaudeCodeHarness`,
  spec-121 T3) — appends `--allowedTools <mcp wire names>` +
  `--mcp-config <inline json>` so Claude runs its own agentic loop
  against the T2 tool bridge.

The tool-mode flags are deliberately NOT unified here — they are the
one place the two callers differ, and each appends its own. Everything
in this module is mode-agnostic:

- [`SCRUBBED_ENV_VARS`][llm.claude_cli.SCRUBBED_ENV_VARS] /
  [`scrubbed_env`][llm.claude_cli.scrubbed_env] — strip every var that
  would redirect auth/billing/model off the subscription path.
- [`build_base_argv`][llm.claude_cli.build_base_argv] — the shared
  `claude -p` prefix (`--output-format json`, `--model`,
  `--permission-mode dontAsk`, `--setting-sources user`; NEVER
  `--bare` — see the deviation note on
  `_ClaudeCodeLLM._build_argv`: `--bare` is incompatible with
  subscription auth on the installed CLI).
- [`extract_result_text`][llm.claude_cli.extract_result_text] — pull
  the assistant text out of the `--output-format json` object,
  classifying `is_error` results via
  [`_ClaudeResultError`][llm.claude_cli._ClaudeResultError].
- [`STATIC_MODELS`][llm.claude_cli.STATIC_MODELS] — the stable model
  aliases + context windows (the CLI has no list-models endpoint).

Redaction rule carried over verbatim from spec-118: nothing in this
module ever embeds child stdout/stderr, argv, or prompt text in a
raised error.
"""

from __future__ import annotations

import os
from typing import Any

# The model ids the `claude` CLI accepts via `--model`, each with the
# context window compaction needs. Static by design (see the provider
# module docstring's static-catalog exception) — the CLI has NO
# list-models endpoint (`claude --help`: `--model` takes "an alias for
# the latest model … or the model's full name"; there is no `claude
# models` subcommand), so the set is curated here, not discovered.
#
# Two kinds of entry:
#   - the 3 stable ALIASES (opus/sonnet/haiku) — always valid under any
#     subscription, auto-track the latest of each tier; the recommended
#     default.
#   - current pinned versioned ids — for reproducibility when the
#     subscription entitles that model.
# Sourced from the Anthropic model list (claude-api reference,
# 2026-08-03); exclude retired ids and re-verify when updating.
#
# `context_length` is a CONSERVATIVE 200_000 on EVERY entry — NOT the
# Anthropic API's 1M. `claude_code` runs `claude -p` under SUBSCRIPTION
# auth, whose effective window is the standard 200k; over-stating it
# would overflow the real call, while under-stating only makes
# compaction trigger earlier (safe). A user on a larger-context tier can
# raise a specific entry by hand in platform.yaml.
STATIC_MODELS: tuple[tuple[str, int], ...] = (
    # aliases — recommended; auto-track the latest of each tier
    ("opus", 200_000),
    ("sonnet", 200_000),
    ("haiku", 200_000),
    # current pinned versioned ids (top of each tier); older still-active ids
    # (claude-opus-4-6/-4-5, claude-sonnet-4-5) are omitted — hand-add in
    # platform.yaml if a subscription entitles them.
    ("claude-opus-4-8", 200_000),
    ("claude-opus-4-7", 200_000),
    ("claude-sonnet-5", 200_000),
    ("claude-sonnet-4-6", 200_000),
    ("claude-haiku-4-5", 200_000),
    # most capable — needs subscription entitlement + 30-day data retention,
    # and prices above Opus; fails at runtime more readily than the other pins.
    ("claude-fable-5", 200_000),
)

# Every var that would redirect the child off the subscription onto a
# *billed* path or override the configured model. There is no "prefer
# subscription" flag — the only lever is to UNSET these. Kept as a
# frozenset so the scrub and its test assert against one source of truth.
SCRUBBED_ENV_VARS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    }
)


def scrubbed_env() -> dict[str, str]:
    """Return `os.environ` with every `SCRUBBED_ENV_VARS` entry removed."""
    return {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV_VARS}


def build_base_argv(*, binary: str, model: str) -> list[str]:
    """Assemble the shared `claude -p` argv prefix (mode-agnostic).

    Returns ONLY the flags common to the text-only provider path and
    the tool-bridge framework path:

    - `-p --output-format json` — one JSON object on stdout.
    - `--model <model>` — the alias or full model id.
    - `--permission-mode dontAsk` — a no-TTY child never blocks on a
      permission prompt.
    - `--setting-sources user` — with the caller-supplied neutral cwd
      this is the config-isolation posture (no stray `CLAUDE.md`,
      project/local settings, hooks, plugins, or workspace MCP
      servers). NEVER `--bare` — it forces API-key auth and is
      incompatible with subscription auth (spec-118's spike).

    Callers append their own tool-mode flags: the provider adds
    `--disallowedTools "*"` (text-only); the framework adds
    `--allowedTools <wire names>` + `--mcp-config <json>`. Do not
    unify those here.
    """
    return [
        binary,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "user",
    ]


def extract_result_text(payload: dict[str, Any]) -> str:
    """Extract the assistant text from a `claude -p --output-format json` object.

    The CLI returns ONE JSON object with the completed assistant text
    in the top-level `result` string (plus `subtype`, `is_error`, …).
    This is the fixture-locked extraction contract (spec-118 Test 1).

    Args:
        payload: The parsed `--output-format json` object.

    Returns:
        The `result` string.

    Raises:
        _ClaudeResultError: `is_error` is truthy, or `result` is missing
            / not a string. Carries a generic, secret-free reason — the
            raw payload is NOT embedded.
    """
    if payload.get("is_error"):
        # A not-logged-in / rate-limit / upstream hiccup lands here. The
        # `result` field carries a human string ("Not logged in · …")
        # that we classify by shape but never persist verbatim.
        subtype = payload.get("subtype")
        raise _ClaudeResultError(
            reason="the CLI reported an error result",
            is_error=True,
            not_logged_in=_looks_not_logged_in(payload),
            subtype=str(subtype) if subtype is not None else None,
        )
    result = payload.get("result")
    if not isinstance(result, str):
        raise _ClaudeResultError(
            reason="the CLI response had no `result` string",
            is_error=False,
            not_logged_in=False,
            subtype=None,
        )
    return result


def _looks_not_logged_in(payload: dict[str, Any]) -> bool:
    """Best-effort: does this error result mean 'run claude auth login'?

    The CLI reports a logged-out subscription as an `is_error` result
    whose `result` text mentions login (e.g. "Not logged in · Please
    run /login"). We inspect the string ONLY to pick Fatal-auth vs
    Recoverable-server classification; the string never leaves this
    module.
    """
    result = payload.get("result")
    if not isinstance(result, str):
        return False
    lowered = result.lower()
    return "not logged in" in lowered or "/login" in lowered or "please run" in lowered


class _ClaudeResultError(Exception):
    """Internal signal that the JSON object did not yield usable text.

    Never crosses a public boundary — each caller catches it and
    re-raises a catalogued `model.*` `PlatformError`. Carries only
    booleans + a generic reason, no raw payload / prompt / stdout.
    """

    def __init__(
        self,
        *,
        reason: str,
        is_error: bool,
        not_logged_in: bool,
        subtype: str | None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.is_error = is_error
        self.not_logged_in = not_logged_in
        self.subtype = subtype

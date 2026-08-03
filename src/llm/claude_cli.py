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

# The `claude` model aliases the CLI accepts, each with the context
# window compaction needs. Static by design (see the provider module
# docstring's static-catalog exception) — the CLI has no list-models
# endpoint. 200k is the stable published window for the current
# Sonnet/Opus/Haiku generation the aliases resolve to; the
# conservative published figure is quoted rather than a per-model max
# so compaction never over-fills.
STATIC_MODELS: tuple[tuple[str, int], ...] = (
    ("sonnet", 200_000),
    ("opus", 200_000),
    ("haiku", 200_000),
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

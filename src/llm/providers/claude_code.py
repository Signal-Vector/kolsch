"""Native `claude_code` provider — run an agent on Claude via the local CLI.

This connector lets an operator point a kolsch agent at **Claude via
their Claude subscription** through the locally-installed `claude` CLI
(`claude -p`), with **no external proxy daemon**. It is the in-repo
version of the pi.dev / OpenClaw "Claude Code as a model" pattern: where
those wrap an external `claude-max-api-proxy`, this folds the wrapper
into kolsch as a first-class [`ProviderConnector`][llm.protocol.ProviderConnector].
Auth rides the subscription (`claude auth login`), not an API key.

Because it plugs into the standard provider seam, a `claude_code` agent
gets normal `model.request` / `model.response` / `agent.message` events,
fallback/retry, and compaction — it behaves like any other provider.

**v1 is text-generation only.**
[`build_llm`][llm.providers.claude_code.ClaudeCodeConnector.build_llm]
returns a [`_ClaudeCodeLLM`][llm.providers.claude_code._ClaudeCodeLLM]
whose *synchronous* `.call` renders messages → a prompt, spawns
`claude -p --output-format json`, reads the assistant text from the
top-level `.result` string, and **returns a bare `str`**. The existing
adapter (`make_llm_callable`, `src/engine/_crewai_adapter.py`) maps a
`str` return to `LLMResponse(content=str, tool_calls=None)` with zero
engine / framework / adapter changes. **Kolsch-orchestrated tool-calling
is out of scope for v1** — an agent on `claude_code` placed on a
tool-using workflow node gets no kolsch tools and may emit tool calls as
prose. `.call` therefore **ignores** any forwarded `tools` (the adapter
forwards them unconditionally; `supports_function_calling()=False` gates
only CrewAI's own executor, not the kolsch seam).

**Subscription / ToS caveat — eyes open (do not soft-pedal).** Since
Anthropic's 2026-06-15 update, `claude -p` / Agent-SDK / third-party
usage draws from the signed-in subscription's usage limits. This is
technical compatibility, **not** an officially sanctioned path: the
Agent SDK docs state Anthropic *"does not allow third party developers
to offer claude.ai login … for their products."* Subscription use
outside Claude Code has been throttled/blocked before. See the how-to.

**Static catalog exception.**
[`discover_models`][llm.providers.claude_code.ClaudeCodeConnector.discover_models]
returns a *static* alias list (`sonnet`/`opus`/`haiku`), departing from
`src/llm/providers/README.md`'s "don't ship a static catalog" guidance.
Justified: the `claude` CLI exposes no list-models endpoint, the aliases
are stable, and compaction needs a `context_length` — so each entry
carries one. This is the deliberate exception the README allows a
provider to state.

**Subprocess hygiene.**

- *Env scrub.* The child env is stripped of every var that would
  redirect auth/billing/model to a *billed* path or override the model —
  see `_SCRUBBED_ENV_VARS`. There is no "prefer subscription" flag; the
  only way to force the CLI onto subscription auth is to unset these.
- *Config isolation.* The child runs in an explicit **neutral empty
  cwd** and with `--setting-sources user` so it never auto-loads a stray
  `CLAUDE.md`, `.claude/settings.json`, project/local settings, hooks,
  plugins, or MCP servers from the invoking workspace (a correctness-
  pollution AND instruction-injection vector). See the `--bare`
  deviation note on `_ClaudeCodeLLM._build_argv`.
- *No headless hang.* `--disallowedTools "*"` + `--permission-mode
  dontAsk` so a no-TTY child never blocks on a permission prompt.
- *Timeout + reap.* The subprocess timeout sits **below** the adapter's
  `asyncio.wait_for` wrap so the connector's own Recoverable
  `model.timeout` classifies rather than the adapter's `ModelTimeout`;
  the child is reaped (SIGTERM/kill) on the timeout path.

**Redaction.** The failure paths never put the child's stdout/stderr,
the argv, or the rendered prompt into a raised error, an event, or any
persistent file — kolsch's "no secrets in the log" rule. Error surfaces
carry only the typed code plus a generic message.

**Error mapping.** Reuses the catalogued `model.*` family so the
engine's fallback ladder engages unchanged:

- timeout → [`ModelTimeout`][errors.ModelTimeout] (`model.timeout`,
  Recoverable).
- non-zero exit / `is_error` upstream hiccup →
  [`ModelServerError`][errors.ModelServerError] (`model.server_error`,
  Recoverable).
- a not-logged-in `is_error` result →
  [`ModelAuthenticationFailed`][errors.ModelAuthenticationFailed]
  (`model.authentication_failed`, Fatal).
- missing binary → [`ModelBinaryUnavailable`][errors.ModelBinaryUnavailable]
  (`model.binary_unavailable`, Fatal — the one net-new code).

**Usage / cost.** Subscription auth returns no per-token usage kolsch
records. `model.response` usage is empty for `claude_code`; the cost UI
shows "N/A (subscription)" rather than blank/zero garbage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from crewai.llms.base_llm import BaseLLM

from errors import (
    ModelAuthenticationFailed,
    ModelBinaryUnavailable,
    ModelServerError,
    ModelTimeout,
)
from llm.protocol import (
    AuthPromptSpec,
    Credentials,
    ModelEntry,
    PromptField,
    ProviderConfig,
    ValidationResult,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import crewai


# The `claude` model aliases the CLI accepts, each with the context
# window compaction needs. Static by design (see module docstring) —
# the CLI has no list-models endpoint. 200k is the stable published
# window for the current Sonnet/Opus/Haiku generation the aliases
# resolve to; the connector deliberately quotes the conservative
# published figure rather than a per-model max so compaction never
# over-fills.
_STATIC_MODELS: tuple[tuple[str, int], ...] = (
    ("sonnet", 200_000),
    ("opus", 200_000),
    ("haiku", 200_000),
)

# Every var that would redirect the child off the subscription onto a
# *billed* path or override the configured model. There is no "prefer
# subscription" flag — the only lever is to UNSET these. Kept as a
# frozenset so the scrub and its test assert against one source of truth.
_SCRUBBED_ENV_VARS: frozenset[str] = frozenset(
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

# The connector's own subprocess timeout. Deliberately BELOW the
# adapter's `KOLSCH_LLM_CALL_TIMEOUT_SECONDS` (default 210s) so the
# connector's own Recoverable `model.timeout` classifies first, rather
# than the adapter's outer `asyncio.wait_for` raising its own
# `ModelTimeout` after killing the worker thread (which would NOT reap
# the child). 180s leaves ~30s of headroom.
_DEFAULT_TIMEOUT_SECONDS = 180.0


class _CompletedProcess(Protocol):
    """The subset of `subprocess.CompletedProcess` the connector reads.

    A Protocol so tests can inject a plain stub without importing the
    stdlib class.
    """

    returncode: int
    stdout: str


class _ProcessRunner(Protocol):
    """The injected spawn seam — mirrors `subprocess.run(..., env=…)`.

    Production wires [`_default_runner`][llm.providers.claude_code._default_runner]
    (a thin `subprocess.run` wrapper that reaps on timeout). Tests pass
    a stub capturing `argv` / `env` / `cwd` and returning a canned
    `_CompletedProcess`, so the gate never shells a live `claude`.

    Raising `subprocess.TimeoutExpired` signals the timeout path (the
    runner is responsible for reaping the child before it raises);
    raising `FileNotFoundError` signals a missing binary.
    """

    def __call__(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        timeout: float,
    ) -> _CompletedProcess:
        """Run `argv` with the given env/cwd/timeout and return the result."""
        ...


def _default_runner(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    timeout: float,
) -> _CompletedProcess:
    """Production runner — blocking `subprocess.run`, reaping on timeout.

    `subprocess.run(timeout=…)` already SIGKILLs and waits (reaps) the
    child before re-raising `TimeoutExpired`, so the "reap on every
    path" requirement is satisfied by the stdlib here; the docstring
    pins the guarantee so a future refactor to `Popen` preserves it.
    Stderr is merged into stdout only so the JSON object (on stdout)
    isn't split from any diagnostic — the caller reads `stdout` and
    NEVER logs it.
    """
    return subprocess.run(  # noqa: S603 — argv is connector-built, never shell
        argv,
        env=dict(env),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _scrubbed_env() -> dict[str, str]:
    """Return `os.environ` with every `_SCRUBBED_ENV_VARS` entry removed."""
    return {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV_VARS}


def extract_result_text(payload: dict[str, Any]) -> str:
    """Extract the assistant text from a `claude -p --output-format json` object.

    The CLI returns ONE JSON object with the completed assistant text
    in the top-level `result` string (plus `subtype`, `is_error`, …).
    This is the fixture-locked extraction contract (Test 1).

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

    Never crosses a public boundary — `_ClaudeCodeLLM.call` catches it
    and re-raises a catalogued `model.*` `PlatformError`. Carries only
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


def _render_prompt(messages: list[dict[str, Any]]) -> tuple[str | None, str]:
    """Render engine messages into (system_prompt, transcript_body).

    The system message (if any) becomes the `--append-system-prompt`
    value; the remaining turns render into the `-p` body as a readable
    transcript. Multi-turn continuity is by re-rendering the FULL
    history each call (kolsch already replays history per exchange) —
    NOT `--resume`/`--continue`, which need same-cwd session scoping
    that conflicts with the neutral cwd. This means no server-side
    context caching; cost/latency grow with conversation length (see
    the how-to).
    """
    system_parts: list[str] = []
    body_lines: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user"))
        content = _stringify_content(msg.get("content"))
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        label = {"assistant": "Assistant", "tool": "Tool"}.get(role, "User")
        body_lines.append(f"{label}: {content}")
    system_prompt = "\n\n".join(system_parts) if system_parts else None
    body = "\n\n".join(body_lines) if body_lines else ""
    return system_prompt, body


def _stringify_content(content: object) -> str:
    """Coerce a message `content` (str or content-block list) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


class _ClaudeCodeLLM(BaseLLM):
    """`BaseLLM` subclass that shells `claude -p` and returns the reply text.

    Mirrors `_CrewAILLM` (`src/engine/crewai_framework.py`): a Pydantic-v2
    `BaseLLM` subclass, engine-internal (leading underscore). The
    synchronous `.call` runs inside the adapter's `asyncio.to_thread`, so
    a blocking `subprocess.run` (via the injected runner) is correct — no
    async subprocess here.

    `model` is a required, validated `BaseLLM` field (the base raises
    "Model name is required" if empty), so callers pass `model=model_id`
    at construction.
    """

    model_config: ClassVar[Any] = {"arbitrary_types_allowed": True}

    llm_type: str = "claude_code"
    provider: str = "claude_code"

    # Injected spawn seam + tuning. Stored as arbitrary-typed model
    # fields (model_config opens that door) so Pydantic accepts the
    # callable and doesn't try to validate it into a schema.
    runner: Any = _default_runner
    binary_path: str = "claude"
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def supports_function_calling(self) -> bool:
        """False — v1 is text-only; kolsch tool-calling is out of scope.

        Note this does NOT suppress `tools` forwarding on the kolsch
        seam (the adapter forwards unconditionally); `.call` ignores
        them. False documents intent and gates CrewAI's own executor.
        """
        return False

    def call(  # type: ignore[override]
        self,
        messages: str | list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 — ignored (v1 text-only)
        callbacks: list[Any] | None = None,  # noqa: ARG002
        available_functions: dict[str, Any] | None = None,  # noqa: ARG002
        from_task: Any | None = None,  # noqa: ARG002
        from_agent: Any | None = None,  # noqa: ARG002
        response_model: Any | None = None,  # noqa: ARG002
    ) -> str:
        """Render messages → `claude -p`, return the `.result` text.

        Synchronous by contract (runs under the adapter's `to_thread`).
        Ignores `tools` — v1 is text-only. Raises catalogued `model.*`
        errors on failure, with NO child output / argv / prompt in the
        message.
        """
        msgs: list[dict[str, Any]]
        msgs = (
            [{"role": "user", "content": messages}]
            if isinstance(messages, str)
            else list(messages)
        )
        system_prompt, body = _render_prompt(msgs)
        with tempfile.TemporaryDirectory(prefix="kolsch-claude-") as neutral_cwd:
            argv = self._build_argv(system_prompt, body)
            payload = self._run(argv, neutral_cwd)
        return extract_result_text(payload)

    # ---- Internals ---------------------------------------------------------

    def _build_argv(self, system_prompt: str | None, body: str) -> list[str]:
        """Assemble the `claude` argv.

        DEVIATION FROM SPEC (spike-confirmed): the spec recommends
        `--bare`, but against the installed `claude` 2.1.x `--bare`
        forces auth to `ANTHROPIC_API_KEY`/apiKeyHelper and *never reads
        OAuth/keychain* — with the API key scrubbed (as it must be for
        the subscription path) `--bare` returns "Not logged in". `--bare`
        is therefore INCOMPATIBLE with subscription auth. The spec's
        actual goal for `--bare` (AC6: the child cannot pick up an
        arbitrary cwd's `CLAUDE.md` / `.claude`) is met instead by the
        explicit neutral empty cwd (no `CLAUDE.md` to discover) plus
        `--setting-sources user` (project/local settings, hooks,
        plugins, and MCP servers are not loaded). Verified in the spike:
        this combination isolates config AND keeps subscription auth.
        """
        argv = [
            self.binary_path,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--disallowedTools",
            "*",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "user",
        ]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        argv.append(body)
        return argv

    def _run(self, argv: list[str], neutral_cwd: str) -> dict[str, Any]:
        """Spawn `claude` via the injected runner and parse its JSON.

        Maps every failure to a catalogued `model.*` error. No child
        stdout/stderr, argv, or the rendered prompt is placed on any
        raised error.
        """
        try:
            completed = self.runner(
                argv,
                env=_scrubbed_env(),
                cwd=neutral_cwd,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError:
            raise ModelBinaryUnavailable(
                provider=self.provider,
                model=self.model,
                reason="`claude` binary not found on PATH",
            ) from None
        except subprocess.TimeoutExpired:
            # The runner reaps the child before raising (stdlib
            # `subprocess.run(timeout=…)` SIGKILLs + waits). Classify as
            # Recoverable so fallback/retry engages. `from None` is
            # load-bearing: `subprocess.TimeoutExpired.cmd` is the full argv
            # (rendered prompt + `--append-system-prompt`), so chaining it as
            # `__cause__` would serialize the prompt into the durable JSONL
            # log via `PlatformError.to_dict()`. Drop the cause entirely.
            raise ModelTimeout(
                provider=self.provider,
                model=self.model,
                timeout_seconds=self.timeout_seconds,
            ) from None

        if completed.returncode != 0:
            # Non-zero exit — a CLI crash / upstream hiccup. Recoverable.
            # stdout is deliberately NOT read into the error.
            raise ModelServerError(
                provider=self.provider,
                model=self.model,
                status_code=502,
            )

        try:
            payload = json.loads(completed.stdout)
        except (ValueError, TypeError):
            # Unparseable JSON — treat as an upstream hiccup, Recoverable.
            # The raw stdout is NOT embedded; `from None` keeps the raw doc
            # (which may echo the prompt) out of the serialized cause chain.
            raise ModelServerError(
                provider=self.provider,
                model=self.model,
                status_code=502,
            ) from None
        if not isinstance(payload, dict):
            raise ModelServerError(
                provider=self.provider,
                model=self.model,
                status_code=502,
            )

        try:
            extract_result_text(payload)
        except _ClaudeResultError as exc:
            # `from None` — the classified error carries only typed fields;
            # never chain the result text (which may echo prompt content).
            raise self._classify_result_error(exc) from None
        return payload

    def _classify_result_error(
        self, exc: _ClaudeResultError
    ) -> ModelAuthenticationFailed | ModelServerError:
        """Map a `_ClaudeResultError` to the right catalogued `model.*`.

        Not-logged-in → Fatal `model.authentication_failed` (the same
        subscription state fails everywhere; the chain won't help).
        Anything else → Recoverable `model.server_error`.
        """
        if exc.not_logged_in:
            return ModelAuthenticationFailed(
                provider=self.provider,
                model=self.model,
                status_code=401,
            )
        return ModelServerError(
            provider=self.provider,
            model=self.model,
            status_code=502,
        )


class ClaudeCodeConnector:
    """The native `claude_code` provider — subscription auth via `claude -p`.

    Stateless. Construct with `runner=<stub>` in tests to bypass a live
    `claude`; production uses the registry's no-argument `cls()` path
    (defaulting to [`_default_runner`][llm.providers.claude_code._default_runner]).

    Attributes:
        type_name: The registry key — `"claude_code"`.
    """

    type_name: ClassVar[str] = "claude_code"

    def __init__(self, *, runner: _ProcessRunner | None = None) -> None:
        """Construct with an optional injected process-runner for tests."""
        self._runner: _ProcessRunner = runner or _default_runner

    # ---- Protocol surface --------------------------------------------------

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Return the prompts: a login note + optional binary-path override.

        NO `kind="secret"` field — subscription auth via `claude auth
        login`, not an API key. The keyless connect fix
        (`_build_provider_entry`) keys off exactly this absence, so a
        `claude_code` entry is written with no `api_key_secret`.
        """
        return AuthPromptSpec(
            provider_label="Claude Code (subscription)",
            intro=(
                "Runs a kolsch agent on Claude via your Claude subscription "
                "through the local `claude` CLI — no API key. Log in first "
                "with `claude auth login`. Note: subscription use outside "
                "Claude Code is technical compatibility, not an officially "
                "sanctioned path (see docs/how-to/connect-a-provider.md)."
            ),
            fields=[
                PromptField(
                    name="binary_path",
                    label="Path to the `claude` binary (blank = search PATH)",
                    kind="string",
                    default="claude",
                    required=False,
                ),
            ],
        )

    async def validate_credentials(
        self,
        credentials: Credentials,
        base_url: str | None,  # noqa: ARG002 — no base URL for a local CLI
    ) -> ValidationResult:
        """Confirm the `claude` binary is installed and logged in.

        A tiny live `claude -p` probe (one word) via the runner: a
        `subtype == "success"`, non-error `result` means the subscription
        is authenticated. A missing binary or an `is_error` result →
        `ok=False` with a generic reason (no child output embedded).
        """
        binary = credentials.values.get("binary_path") or "claude"
        # `shutil.which` resolves both a bare name (PATH search) and an
        # explicit path (returns it iff it is an executable file), so it
        # is the single check for "is the binary runnable".
        if shutil.which(binary) is None:
            return ValidationResult(
                ok=False,
                reason=(
                    f"`{binary}` not found — install Claude Code and ensure "
                    f"it is on PATH."
                ),
            )
        llm = _ClaudeCodeLLM(
            model="sonnet",
            runner=self._runner,
            binary_path=binary,
            timeout_seconds=60.0,
        )
        try:
            text = llm.call("Reply with exactly one word: ok")
        except ModelBinaryUnavailable:
            return ValidationResult(
                ok=False,
                reason="`claude` binary not found on PATH.",
            )
        except ModelAuthenticationFailed:
            return ValidationResult(
                ok=False,
                reason="`claude` is not logged in — run `claude auth login`.",
            )
        except ModelTimeout:
            return ValidationResult(
                ok=False,
                reason="`claude` probe timed out.",
            )
        except ModelServerError:
            return ValidationResult(
                ok=False,
                reason="`claude` probe failed.",
            )
        return ValidationResult(
            ok=True,
            metadata={"probe_reply_chars": len(text)},
        )

    async def discover_models(
        self,
        credentials: Credentials,  # noqa: ARG002 — static catalog, no live call
        base_url: str | None,  # noqa: ARG002 — no base URL for a local CLI
    ) -> list[ModelEntry]:
        """Return the static supported Claude aliases with context windows.

        Static by design (see the module docstring's static-catalog
        exception): the `claude` CLI exposes no list-models endpoint, the
        aliases are stable, and each entry carries a `context_length` so
        compaction never hits the `no_context_length` skip.
        """
        return [
            ModelEntry(id=alias, context_length=window)
            for alias, window in _STATIC_MODELS
        ]

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,  # noqa: ARG002 — subscription auth, no key
        parameters: Mapping[str, Any] | None = None,  # noqa: ARG002 — v1 forwards none
    ) -> crewai.LLM:
        """Construct the `_ClaudeCodeLLM` for the named model.

        Returns `cast("crewai.LLM", _ClaudeCodeLLM(...))`: `_ClaudeCodeLLM`
        is a `BaseLLM` subclass (a *supertype* of `crewai.LLM`, which
        also subclasses `BaseLLM`), so the cast keeps the `-> crewai.LLM`
        contract `mypy --strict`-clean. The adapter only calls `.call`,
        which `_ClaudeCodeLLM` implements.

        The subprocess timeout is taken from
        `provider_config.timeout_seconds` when set (so an operator can
        tune it below the adapter wrap), else the connector default.
        `parameters` is intentionally not forwarded — the `claude` CLI
        exposes no per-call temperature/top-p knob on this path.
        """
        timeout = (
            float(provider_config.timeout_seconds)
            if provider_config.timeout_seconds is not None
            else _DEFAULT_TIMEOUT_SECONDS
        )
        binary_path = str(provider_config.extras.get("binary_path") or "claude")
        return cast(
            "crewai.LLM",
            _ClaudeCodeLLM(
                model=model_id,
                runner=self._runner,
                binary_path=binary_path,
                timeout_seconds=timeout,
            ),
        )

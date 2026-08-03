"""`SubprocessAgentFramework` — an external harness runs the agentic loop (spec-121 T3).

A [`Framework`][engine.framework.Framework] implementation that does
NOT drive the engine's LLM/tool seams. Instead it spawns an external
agent harness (v1: the `claude` CLI, described by
[`ClaudeCodeHarness`][engine.subprocess_framework.ClaudeCodeHarness]),
points it at the engine-owned per-exchange
`ToolBridgeServer` (spec-120) via an inline `--mcp-config`, and lets
the harness run its own multi-turn tool-calling loop. Every
`tools/call` the harness makes routes back through the bridge into
real dispatch/authz/sandbox with `tool.called`/`tool.result` events —
JSONL stays ground truth. The harness's final `.result` text is
returned and the engine materializes it into `agent.message` as usual.

Selection is AUTO and engine-owned (`_Exchange._run_framework`): a
`claude_code` agent with ≥1 registered tool lands here; a 0-tool
`claude_code` agent stays on the spec-118 text-only provider path.
This class never self-selects.

Load-bearing invariants (the spec's hazard list):

- **Async spawn only.** `asyncio.create_subprocess_exec` — a blocking
  spawn (`subprocess.run`, even under `to_thread`) wedges the loop:
  `claude` blocks awaiting a `tools/call` reply that only this
  process's event loop can serve. This module contains the engine's
  ONLY `create_subprocess_exec` call.
- **No engine lock held across the spawn.** This class never sees the
  per-thread emit lock; the bridge sink acquires it per-emit.
- **Cancel kills the tree.** `proc.wait`-equivalent (`communicate`) is
  raced against `cancel_token.wait_cancelled()`; on cancel the child
  is `kill()`ed and reaped. `claude` owns the `kolsch mcp bridge`
  proxy child — killing `claude` closes the proxy's stdio, and the
  engine's spawn-scoped bridge `close()` drops the socket, so the
  proxy's read loops exit; no orphan survives.
- **spec-118 posture preserved.** Neutral empty cwd +
  `--setting-sources user` + NEVER `--bare`; env via
  [`scrubbed_env`][llm.claude_cli.scrubbed_env]; the bridge token
  crosses only in the *proxy server entry's* `env` inside the
  `--mcp-config` JSON (argv is same-uid-visible, matching the 0700
  socket-dir posture — the token never touches disk or the event log).
- **Redaction.** No child stdout/stderr, argv, or prompt text is ever
  embedded in a raised error (same rule as the provider).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from errors import (
    ModelAuthenticationFailed,
    ModelBinaryUnavailable,
    ModelServerError,
    PlatformError,
    PlatformInternalError,
)
from llm.claude_cli import (
    STATIC_MODELS,
    _ClaudeResultError,
    build_base_argv,
    extract_result_text,
    scrubbed_env,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from engine.framework import LLMHandle, Message, ToolHandle


_CLAUDE_MODEL_ALIASES: tuple[str, ...] = tuple(alias for alias, _ in STATIC_MODELS)


class _HarnessProcess(Protocol):
    """The slice of `asyncio.subprocess.Process` this framework drives.

    A Protocol so tests can inject a stub process (via
    [`_Spawner`][engine.subprocess_framework._Spawner]) without a live
    `claude` — the gate never shells the real binary.
    """

    @property
    def returncode(self) -> int | None:
        """Exit status once the child has been reaped, else `None`."""
        ...

    def kill(self) -> None:
        """SIGKILL the child (idempotence NOT required — guarded here)."""
        ...

    async def communicate(self) -> tuple[bytes, bytes]:
        """Wait for exit and return `(stdout, stderr)` — the reap."""
        ...


class _Spawner(Protocol):
    """The injected async-spawn seam — mirrors `create_subprocess_exec`.

    Production wires
    [`_default_spawner`][engine.subprocess_framework._default_spawner];
    tests pass a stub capturing `argv`/`cwd`/`env` and returning a
    canned [`_HarnessProcess`][engine.subprocess_framework._HarnessProcess].
    Raising `FileNotFoundError` signals a missing binary.
    """

    async def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: Mapping[str, str],
    ) -> _HarnessProcess:
        """Spawn `argv` in `cwd` with `env` and return the process handle."""
        ...


async def _default_spawner(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
) -> _HarnessProcess:
    """Production spawner — `asyncio.create_subprocess_exec`, never blocking.

    NOT `to_thread`/`subprocess.run`: the child calls back into THIS
    process (bridge `tools/call`) before exiting, so a spawn that
    blocks the loop deadlocks (spec-121 hazard #2). stdin is
    `DEVNULL` (the prompt travels on argv), stdout/stderr are piped so
    `communicate()` both reaps and collects without pipe-full
    deadlock.
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=dict(env),
    )


@dataclass(frozen=True)
class ClaudeCodeHarness:
    """The per-harness descriptor: everything `claude`-specific (spec-121).

    `SubprocessAgentFramework` is generic choreography (bridge →
    argv → spawn → race → extract); this descriptor carries the bits
    a future Codex/Gemini harness would swap out — the binary and
    invocation ([`build_base_argv`][llm.claude_cli.build_base_argv] +
    the tool-mode flags), the MCP proxy command (`-m cli mcp bridge` —
    the module entry point is `cli`, NOT `kolsch`), the result
    extraction, the env scrub, and the model set. Deliberately small;
    NO registry — a second harness is a new descriptor, and selection
    machinery is deferred until one exists.
    """

    binary_path: str = "claude"
    server_name: str = "kolsch"
    provider_name: str = "claude_code"
    default_model: str = "sonnet"
    models: tuple[str, ...] = _CLAUDE_MODEL_ALIASES

    def mcp_config(self, *, socket_path: str, token: str) -> str:
        """The inline `--mcp-config` JSON string (never written to disk).

        The server entry spawns kolsch's own stdio proxy
        (`sys.executable -m cli mcp bridge`, the same `-m cli` form
        every in-repo spawn uses — `python -m kolsch` has no
        `__main__`). The bridge socket + one-binding token ride the
        entry's `env` so the token stays out of any file and out of
        the harness's own argv.
        """
        return json.dumps(
            {
                "mcpServers": {
                    self.server_name: {
                        "command": sys.executable,
                        "args": ["-m", "cli", "mcp", "bridge"],
                        "env": {
                            "KOLSCH_MCP_SOCKET": socket_path,
                            "KOLSCH_MCP_TOKEN": token,
                        },
                    }
                }
            },
            separators=(",", ":"),
        )

    def build_argv(
        self,
        *,
        model: str,
        mcp_config: str,
        allowed_tools: Sequence[str],
        system_prompt: str | None,
        body: str,
    ) -> list[str]:
        """The full `claude` argv for one tool-bridge exchange.

        [`build_base_argv`][llm.claude_cli.build_base_argv] supplies
        the shared prefix (`-p --output-format json --model …
        --permission-mode dontAsk --setting-sources user`, never
        `--bare`); this appends the framework's tool-mode flags —
        `--mcp-config <inline json>` + `--allowedTools <wire names>`
        (comma-joined `mcp__<server>__<bare>`, pre-approved so
        `dontAsk` denies everything else without blocking).
        """
        argv = build_base_argv(binary=self.binary_path, model=model)
        argv += [
            "--mcp-config",
            mcp_config,
            "--allowedTools",
            ",".join(allowed_tools),
        ]
        appended = self._compose_system_prompt(system_prompt, allowed_tools)
        if appended:
            argv += ["--append-system-prompt", appended]
        argv.append(body)
        return argv

    def _compose_system_prompt(
        self,
        system_prompt: str | None,
        allowed_tools: Sequence[str],
    ) -> str | None:
        """Agent system prompt + the harness's tool-grounding preamble.

        Live-run finding (spec-121): the installed harness may DEFER
        MCP tools behind its tool-search capability (user settings
        ride in via `--setting-sources user`), so the bridge tools are
        sometimes absent from the model's upfront tool list — without
        grounding, the model concludes the agent prompt's tool talk is
        injected content, substitutes its own builtins, and never
        touches the bridge (observed live, nondeterministic). Naming
        the exact `mcp__<server>__<bare>` names and instructing the
        model to load-then-call them makes the tool path reliable.
        """
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt)
        if allowed_tools:
            names = ", ".join(allowed_tools)
            parts.append(
                f"Your platform tools for this run are provided by the "
                f"`{self.server_name}` MCP server: {names}. These ARE part "
                "of your real toolset — call them by exactly those names. "
                "If they are not in your visible tool list, they are "
                "deferred: discover and load them first (e.g. with your "
                "tool-search capability), then call them. Do not "
                "substitute your own built-in tools for them."
            )
        return "\n\n".join(parts) if parts else None

    def env(self) -> dict[str, str]:
        """The child env — [`scrubbed_env`][llm.claude_cli.scrubbed_env]."""
        return scrubbed_env()

    def extract_text(self, payload: dict[str, Any]) -> str:
        """Extract the final assistant text from the parsed JSON object.

        Raises:
            _ClaudeResultError: classified by the caller via
                [`classify_result_error`][engine.subprocess_framework.ClaudeCodeHarness.classify_result_error].
        """
        return extract_result_text(payload)

    def classify_result_error(
        self,
        exc: _ClaudeResultError,
        *,
        model: str,
    ) -> PlatformError:
        """spec-118's mapping, reused: not-logged-in → Fatal auth, else 502.

        A logged-out subscription fails identically everywhere —
        Fatal `model.authentication_failed` (the fallback chain won't
        help). Anything else is a Recoverable `model.server_error`.
        """
        if exc.not_logged_in:
            return ModelAuthenticationFailed(
                provider=self.provider_name,
                model=model,
                status_code=401,
            )
        return ModelServerError(
            provider=self.provider_name,
            model=model,
            status_code=502,
        )


def _render_body(history: Sequence[Message], user_message: str) -> str:
    """Render prior turns + the new user message into the `-p` body.

    Mirrors the provider's transcript idiom (labels, full-history
    replay per exchange — no `--resume`, which needs same-cwd session
    scoping the neutral cwd forbids). With no history the body is the
    bare user message.
    """
    if not history:
        return user_message
    lines: list[str] = []
    for msg in history:
        label = {"assistant": "Assistant", "tool": "Tool"}.get(msg.role, "User")
        lines.append(f"{label}: {msg.content}")
    lines.append(f"User: {user_message}")
    return "\n\n".join(lines)


class SubprocessAgentFramework:
    """Run one exchange by delegating the agentic loop to a subprocess harness.

    Implements [`Framework`][engine.framework.Framework]. Consumes the
    engine-provided `tool_bridge` (`config_fragment()` for the
    socket/token, `wire_tool_names()` for the filtered
    `--allowedTools` set), spawns the harness via the injected async
    spawner, races completion against the cancel token, and returns
    the harness's final text. The `llm` seam is used ONLY to read the
    exchange's primary `ModelSpec` (for `--model`) — the harness
    talks to the model itself; `tools` handles are unused because
    tool calls arrive over the bridge, not the seam.
    """

    def __init__(
        self,
        *,
        descriptor: ClaudeCodeHarness,
        spawner: _Spawner | None = None,
    ) -> None:
        """Construct with a harness descriptor + optional spawn stub (tests)."""
        self._descriptor = descriptor
        self._spawner: _Spawner = spawner if spawner is not None else _default_spawner

    async def run_exchange(
        self,
        *,
        system_prompt: str,
        user_message: str,
        history: Sequence[Message] = (),
        llm: LLMHandle,
        tools: Sequence[ToolHandle],  # noqa: ARG002 — calls arrive over the bridge, not the seam
        max_iterations: int | None = None,  # noqa: ARG002 — the harness owns its loop
        cancellation: Callable[[], bool] | None = None,  # noqa: ARG002 — raced on cancel_token instead
        cancel_token: Any = None,
        tool_bridge: Any = None,
    ) -> str:
        """Spawn the harness against the bridge and return its final text.

        Raises:
            PlatformInternalError: `tool_bridge is None` — the engine
                selected this framework without building a bridge (a
                wiring bug, not an operator error).
            ModelBinaryUnavailable: the harness binary is missing.
            ModelServerError: non-zero exit / unparseable output
                (Recoverable — the engine's retry loop re-spawns
                against the SAME still-open bridge).
            ModelAuthenticationFailed: the harness reports a
                logged-out subscription (Fatal).
            asyncio.CancelledError: the cancel token fired; the child
                was killed and reaped first. The engine converts this
                to `ExchangeCancelled` (its standard cancel surface).
        """
        if tool_bridge is None:
            raise PlatformInternalError(
                original_type="ToolBridgeMissing",
                args=[
                    "SubprocessAgentFramework.run_exchange called without "
                    "an engine-provided tool_bridge"
                ],
                where="engine.subprocess_framework.run_exchange",
            )

        fragment: dict[str, str] = tool_bridge.config_fragment()
        allowed_tools: list[str] = list(tool_bridge.wire_tool_names())
        model = self._resolve_model(llm)
        argv = self._descriptor.build_argv(
            model=model,
            mcp_config=self._descriptor.mcp_config(
                socket_path=fragment["socket_path"],
                token=fragment["token"],
            ),
            allowed_tools=allowed_tools,
            system_prompt=system_prompt or None,
            body=_render_body(history, user_message),
        )

        # Neutral empty cwd (spec-118 posture): nothing for the harness
        # to auto-load — no stray CLAUDE.md / .claude from the invoking
        # workspace.
        with tempfile.TemporaryDirectory(prefix="kolsch-claude-") as neutral_cwd:
            try:
                proc = await self._spawner(
                    argv,
                    cwd=neutral_cwd,
                    env=self._descriptor.env(),
                )
            except FileNotFoundError:
                raise ModelBinaryUnavailable(
                    provider=self._descriptor.provider_name,
                    model=model,
                    reason=f"`{self._descriptor.binary_path}` binary not found",
                ) from None
            stdout = await self._communicate_racing_cancel(proc, cancel_token)

        if proc.returncode != 0:
            # Harness crash / upstream hiccup — Recoverable; stdout is
            # deliberately NOT read into the error (redaction rule).
            raise ModelServerError(
                provider=self._descriptor.provider_name,
                model=model,
                status_code=502,
            )
        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except ValueError:
            # `from None` — the raw doc may echo prompt content; never
            # chain it into the serialized cause.
            raise ModelServerError(
                provider=self._descriptor.provider_name,
                model=model,
                status_code=502,
            ) from None
        if not isinstance(payload, dict):
            raise ModelServerError(
                provider=self._descriptor.provider_name,
                model=model,
                status_code=502,
            )
        try:
            return self._descriptor.extract_text(payload)
        except _ClaudeResultError as exc:
            # `from None` — the classified error carries typed fields
            # only; the result text may echo prompt content.
            raise self._descriptor.classify_result_error(exc, model=model) from None

    # ---- Internals ---------------------------------------------------------

    def _resolve_model(self, llm: LLMHandle) -> str:
        """Read the exchange's model id off the engine's LLM handle.

        The engine always passes `FallbackLLMHandle`, which exposes
        `primary_spec` (spec-121). Read duck-typed so a bare test
        stub without the attribute falls back to the descriptor's
        default alias instead of crashing.
        """
        spec = getattr(llm, "primary_spec", None)
        model = getattr(spec, "model", None)
        if isinstance(model, str) and model:
            return model
        return self._descriptor.default_model

    async def _communicate_racing_cancel(
        self,
        proc: _HarnessProcess,
        cancel_token: Any,
    ) -> bytes:
        """Await the child's exit, racing the cancel token; return stdout.

        `communicate()` (not `wait()`) so a chatty child can never
        deadlock on a full stdout pipe. On cancel — the token firing
        OR an ambient task-cancel — the child is `kill()`ed and then
        reaped via the same `communicate()` task, so no zombie and no
        orphaned `kolsch mcp bridge` (the proxy is the child's child;
        it exits when its stdio drops and the bridge socket closes).
        """
        comm_task = asyncio.ensure_future(proc.communicate())
        if cancel_token is None:
            try:
                stdout, _stderr = await comm_task
            except asyncio.CancelledError:
                await self._kill_and_reap(proc, comm_task)
                raise
            return stdout

        cancel_task = asyncio.ensure_future(cancel_token.wait_cancelled())
        try:
            done, _pending = await asyncio.wait(
                {comm_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            # Ambient task-cancel (e.g. a workflow node timeout) —
            # kill the tree, reap, propagate cleanly.
            cancel_task.cancel()
            await self._kill_and_reap(proc, comm_task)
            raise

        if comm_task in done:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cancel_task
            stdout, _stderr = comm_task.result()
            return stdout

        # The cancel token fired first — kill `claude` (which owns the
        # `kolsch mcp bridge` child), reap, and surface the standard
        # cancel signal the engine translates to ExchangeCancelled.
        await self._kill_and_reap(proc, comm_task)
        raise asyncio.CancelledError

    @staticmethod
    async def _kill_and_reap(
        proc: _HarnessProcess,
        comm_task: asyncio.Future[tuple[bytes, bytes]],
    ) -> None:
        """SIGKILL the child and drain the in-flight `communicate()`.

        `kill()` may race a natural exit (`ProcessLookupError`) —
        suppressed. The reap awaits the SHIELDED communicate task so
        an ambient cancel on this frame can't abandon the zombie; the
        drain itself absorbs any exception (the caller is already on
        an error path).
        """
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(comm_task)

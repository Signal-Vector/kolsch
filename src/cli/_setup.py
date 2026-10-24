"""`kolsch setup` — first-run flow.

Six idempotent steps (A-F). Each step has one question: "is this
already done?" → skip. "do I need to ask?" → prompt (or read env
for `--non-interactive`). "ready to write?" → write atomically.

Step composition rules:

- Step C must call the existing
  [`cmd_connect`][cli.llm_commands.cmd_connect]; setup does not
  re-implement the connect flow.
- Step E must call the existing `cli.main._run_agent_once` (the
  run-once seam behind `kolsch run`); setup does not re-implement
  agent execution.
- Setup writes nothing outside the resolved workspace (the `.env`
  secret-value write is the only exception).
- Each step is interruptible at any prompt; the next setup run
  resumes from the same content-derived state, not a state flag.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

from assets.platform_config import default_platform_payload as _stock_defaults
from cli._run_failure import one_line as _one_line
from kolsch._yamlio import dump, dump_preserving_trailing_comment_block, load

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cli.llm_commands import _CommandIO


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


_EXIT_OK = 0
_EXIT_PARTIAL = 4  # Partial setup completion.


# ---------------------------------------------------------------------------
# Step-result token (returned by each step)
# ---------------------------------------------------------------------------


_PROCEEDED = "proceeded"
_SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Step E result — richer than the string tokens above. Carries the
# outcome enum + an optional failure code so the orchestrator can
# pick the right banner + a classifier-derived hint.
# ---------------------------------------------------------------------------


class _StepEOutcome(StrEnum):
    """How the test exchange ended — drives the banner + exit code.

    Distinct from the `proceeded` / `skipped` / `partial` tokens
    the other steps return because Step E's outcome has
    finer-grained user-facing behaviour.
    """

    PASSED = "pass"
    SKIPPED = "skipped"
    FAILED = "fail"


@dataclass(frozen=True)
class _StepEResult:
    """The richer return type from Step E.

    On `FAILED`, `session_id` / `error` / `preflight` are copied from
    the [`_TestRunResult`][cli._setup._TestRunResult] and
    `failure_code` is `error["type"]` when it is a string (the
    classifier key) — or, for a pre-flight failure, the pre-flight
    `PlatformError` code when there was one.
    """

    outcome: _StepEOutcome
    failure_code: str | None = None
    session_id: str | None = None
    error: dict[str, Any] | None = None
    preflight: str | None = None


@dataclass(frozen=True)
class _StepSummary:
    """What steps B and D actually did — feeds the failure banner.

    `workspace_created` is Step B's `_PROCEEDED` (skeleton written this
    run) vs `_SKIPPED` (already initialized); `agent_created` is Step
    D's `_PROCEEDED` (helper materialized this run) vs `_SKIPPED`
    (already present — a declined helper never reaches the failure
    banner because Step E then skips).
    """

    workspace_created: bool
    agent_created: bool


@dataclass(frozen=True)
class _TestRunResult:
    """What `_SetupIO.run_handler` returns for Step E's test exchange.

    - `rc` — `kolsch run`'s exit code (`0` success, non-zero failure).
    - `session_id` — the session the test run created, when it got
      that far; the banner points the user at it.
    - `error` — the durable error envelope
      (`thread.exchange_failed` `data["error"]`: `type`, `message`,
      `where`, `detail`, `cause`) when the exchange ended Fatal;
      `None` on success or a failure with no durable event.
    - `preflight` — one line saying why the run stopped before any
      exchange started (e.g. the agent config did not resolve); `None`
      when the exchange ran. `preflight_code` is that failure's
      `PlatformError` code, when it had one.
    """

    rc: int
    session_id: str | None = None
    error: dict[str, Any] | None = None
    preflight: str | None = None
    preflight_code: str | None = None


# ---------------------------------------------------------------------------
# Prompter Protocol — test-injection seam
# ---------------------------------------------------------------------------


class SetupPrompter(Protocol):
    """Three methods cover every prompt shape in the setup flow."""

    def choose(self, question: str, options: list[str], default: str) -> str:
        """Pick one of `options` (single-letter answers OK).

        Returns the chosen option string (lowercased). Default
        fires on empty input.
        """

    def text(self, question: str, default: str = "") -> str:
        """Free-form text. Empty input returns `default`."""

    def confirm(self, question: str, *, default: bool = True) -> bool:
        """Yes/no. Returns `default` on empty input."""


class StdinSetupPrompter:
    """Production prompter — uses stdin/stdout, lowercases all answers."""

    def __init__(self, out: TextIO) -> None:
        self._out = out

    def choose(self, question: str, options: list[str], default: str) -> str:
        """Prompt the user to pick one of `options`."""
        self._out.write(question + "\n")
        for opt in options:
            self._out.write(f"  {opt}\n")
        while True:
            raw = input("> ").strip().lower()
            if not raw:
                return default.lower()
            valid = {opt.split()[0].lower().lstrip("[").rstrip("]") for opt in options}
            if raw in valid:
                return raw
            self._out.write(f"  (please pick one of {options})\n")

    def text(self, question: str, default: str = "") -> str:
        """Prompt for free-form text; empty input returns `default`."""
        suffix = f" [{default}]" if default else ""
        raw = input(f"{question}{suffix}: ").strip()
        return raw or default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        """Prompt for yes/no; empty input returns `default`."""
        suffix = " [Y/n]" if default else " [y/N]"
        raw = input(f"{question}{suffix} ").strip().lower()
        if not raw:
            return default
        return raw.startswith("y")


class NonInteractivePrompter:
    """Returns defaults for everything; no I/O. Used by `--non-interactive`."""

    def choose(self, question: str, options: list[str], default: str) -> str:
        """Return `default` lowercased."""
        _ = question, options
        return default.lower()

    def text(self, question: str, default: str = "") -> str:
        """Return `default`."""
        _ = question
        return default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        """Return `default`."""
        _ = question
        return default


# ---------------------------------------------------------------------------
# Connect-IO and run-handler types — wired to the existing commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SetupIO:
    """Setup's collaborators.

    Tests inject a custom instance to mock HTTP, capture output,
    and script prompts.
    """

    root: Path
    out: TextIO
    err: TextIO
    prompter: SetupPrompter
    # Factory that produces the _CommandIO the existing `cmd_connect`
    # needs. Setup creates one per provider it wants to connect.
    # The factory receives the provider name + an optional API key
    # override (used by --non-interactive's env-driven path).
    connect_io_factory: Callable[[Path, str, str | None], _CommandIO]
    # Run-handler — wraps `cli.main._run_agent_once`. Used by step E
    # to verify the install with a real exchange. Returns a
    # `_TestRunResult` carrying the exit code, the test run's session
    # id, and the durable `thread.exchange_failed` error envelope (when
    # the exchange ended Fatal) so the failure banner renders from the
    # event log rather than from exception text.
    run_handler: Callable[[argparse.Namespace], Awaitable[_TestRunResult]]


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


async def run_setup(args: argparse.Namespace, io: _SetupIO) -> int:
    """Walk the six setup steps. Returns exit code (0 ok, 4 partial).

    Step F is gated on Step E's outcome — the success banner only
    fires when the test exchange actually passed (or was actively
    skipped). A failed test produces a partial-completion banner
    that names the failed step, the workspace path, and a
    classifier-driven hint derived from the failure code, not a
    hardcoded "check provider credentials" string.
    """
    # qol-093 — `--sync-bundle` runs a completely different flow:
    # skip A/B/C/D/E/F, dispatch to the drift-repair pass. Returns
    # 0 clean / non-zero on any skipped drift item.
    if bool(getattr(args, "sync_bundle", False)):
        from cli._bundle_sync import sync_kolsch_bundle  # noqa: PLC0415

        return await sync_kolsch_bundle(
            io,
            dry_run=bool(getattr(args, "dry_run", False)),
            yes=bool(getattr(args, "yes", False)),
            no=bool(getattr(args, "no", False)),
        )

    non_interactive: bool = bool(getattr(args, "non_interactive", False))
    if non_interactive:
        io = _SetupIO(
            root=io.root,
            out=io.out,
            err=io.err,
            prompter=NonInteractivePrompter(),
            connect_io_factory=io.connect_io_factory,
            run_handler=io.run_handler,
        )

    io.out.write("Welcome to Kolsch.\n\n")

    partial = False
    step_e_result: _StepEResult | None = None
    # Each step's result token, keyed by step letter — the failure
    # banner reports what B and D actually did on this run.
    step_results: dict[str, str] = {}

    for step_key, step_name, step_fn in (
        ("A", "A workspace location", _step_a_workspace_location),
        ("B", "B workspace skeleton", _step_b_skeleton),
        ("C", "C LLM provider", _step_c_provider),
        ("D", "D starter agent", _step_d_starter_agent),
    ):
        try:
            result = await step_fn(io, non_interactive=non_interactive)
        except KeyboardInterrupt:
            io.err.write(f"\nsetup aborted at step {step_name}\n")
            return _EXIT_PARTIAL
        step_results[step_key] = result
        if result == "partial":
            partial = True

    # Step E is handled separately because its outcome drives the
    # banner + exit code.
    try:
        step_e_result = await _step_e_test_run(io, non_interactive=non_interactive)
    except KeyboardInterrupt:
        io.err.write("\nsetup aborted at step E test run\n")
        return _EXIT_PARTIAL

    # Branch on Step E's outcome. A FAILED test run prints the
    # partial-completion banner and returns 4, regardless of whether
    # earlier steps reported partial.
    if step_e_result.outcome is _StepEOutcome.FAILED:
        summary = _StepSummary(
            workspace_created=step_results.get("B") == _PROCEEDED,
            agent_created=step_results.get("D") == _PROCEEDED,
        )
        _step_f_banner_failure(io, step_e_result, summary)
        return _EXIT_PARTIAL

    # Success path — passed OR actively skipped both render a "ready"
    # banner. The skipped variant suffixes "(test skipped)" so the
    # user knows nothing was actually exchanged.
    _step_f_banner_success(
        io, test_skipped=step_e_result.outcome is _StepEOutcome.SKIPPED
    )

    # A non-Step-E partial (e.g., Step C connect failure) still
    # surfaces as exit 4 even when Step E itself ran cleanly.
    if partial:
        return _EXIT_PARTIAL
    return _EXIT_OK


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def _classify_failure(failure_code: str | None) -> str:
    """Map a failed-event error code to a user-facing actionable hint.

    Single source of truth — extend the table as more `model.*`
    codes or new failure modes appear. Unknown codes (including
    `None`) fall through to the generic hint.

    Mapping:

    - `model.server_error` / 5xx → "the model returned a 5xx;
      usually transient. Try again, or pick a different model by
      editing the agent's `model:` field (or `kolsch permissions`)."
    - `model.authentication_failed` / 401/403 → "credentials
      failed; re-run `kolsch connect <provider>`."
    - `llm.provider_not_configured` → "the agent's provider isn't
      connected; run `kolsch connect <provider>`."
    - `model.timeout` → "the model took too long to respond. Try
      again or pick a faster model."
    - `model.rate_limited` → "wait a few seconds and re-run, or
      pick a less-trafficked model."
    - `model.context_overflow` → "pick a model with a larger
      context, or shorten the input."
    - `model.bad_request` → "the model rejected the request as
      malformed"; points at the failed session's events.
    - Default (any other code) → "the test exchange failed; inspect
      the failed session's events (see Next steps below), then
      re-run setup or `kolsch run` directly."

    No hint names a placeholder session id — the failure banner
    prints the real `kolsch sessions events <id>` command under
    "Next steps".
    """
    table = {
        "model.server_error": (
            "the model returned a 5xx; this is usually transient. "
            "Try again, or pick a different model by editing the "
            "agent's `model:` field (or via `kolsch permissions`)."
        ),
        "model.authentication_failed": (
            "credentials failed; re-run `kolsch connect <provider>` to refresh the key."
        ),
        "llm.provider_not_configured": (
            "the agent's provider isn't connected; "
            "run `kolsch connect <provider>` first."
        ),
        "model.timeout": (
            "the model took too long to respond. Try again or pick a faster model."
        ),
        "model.rate_limited": (
            "the model is rate-limited; wait a few seconds and re-run, "
            "or pick a less-trafficked model."
        ),
        "model.context_overflow": (
            "the request exceeded the model's context window. "
            "Pick a model with a larger context, or shorten the input."
        ),
        "model.bad_request": (
            "the model rejected the request as malformed. "
            "Inspect the failed session's events (see Next steps below) "
            "for the response detail."
        ),
    }
    return table.get(
        failure_code or "",
        "the test exchange failed; inspect the failed session's events "
        "(see Next steps below), then re-run setup or `kolsch run` directly.",
    )


# ---------------------------------------------------------------------------
# Step A — confirm workspace location
# ---------------------------------------------------------------------------


async def _step_a_workspace_location(io: _SetupIO, *, non_interactive: bool) -> str:
    io.out.write(f"Workspace location: {io.root}\n")
    if non_interactive:
        return _SKIPPED
    # Interactive: confirm or change. We don't actually re-resolve — the
    # CLI already did that. The user changing the path mid-setup would
    # mean re-running with --root or KOLSCH_WORKSPACE; surface that as
    # a hint and accept the current path.
    io.out.write(
        "  Press Enter to use this path, or re-run with --root or "
        "KOLSCH_WORKSPACE to change it.\n"
    )
    return _PROCEEDED


# ---------------------------------------------------------------------------
# Step B — directory skeleton + default platform.yaml + .env
# ---------------------------------------------------------------------------


_WORKSPACE_DIRS: tuple[str, ...] = (
    "bundles",
    "sessions",
    "content",
    "logs",
    "data",
    ".platform",
)


# qol-133 step 1 — a fully-commented, inert worked example appended
# (as raw text, NOT as data) after a fresh workspace's
# `dump(_setup_platform_payload())` output. `dump()` on a plain dict
# cannot carry comments (ruamel.yaml's comment API only works on its
# own CommentedMap/CommentedSeq types), so the example lives here as
# a literal string rather than inside `_setup_platform_payload()`.
# Every line starts with `#` — confirmed by a round-trip through
# `kolsch._yamlio.load()` in tests, which must see no `hooks` key.
_HOOKS_EXAMPLE_BLOCK = """\
# Example: notify yourself when kolsch is about to ask for approval.
# Uncomment and edit `command` for your platform (macOS shown below;
# Linux: notify-send, or any shell command you'd run by hand).
# See docs/how-to/event-hooks.md for the full guide.
#
# hooks:
#   - event: "kolsch.prompt.requested"
#     matcher: "*"
#     command: "terminal-notifier -title kolsch -message 'waiting on your approval'"
#     timeout_seconds: 10
"""


async def _step_b_skeleton(io: _SetupIO, *, non_interactive: bool) -> str:
    yaml_path = io.root / "platform.yaml"
    env_path = io.root / ".env"

    already = yaml_path.is_file()
    if already:
        io.out.write(f"✓ workspace already initialized at {io.root}\n")
        # When re-run against a workspace whose platform.yaml still
        # carries the legacy allow-all sentinel, offer to migrate
        # to the canonical defaults.
        _maybe_migrate_authz_rules(io, yaml_path, non_interactive=non_interactive)
        # Drop in canonical kolsch skills that weren't in the
        # packaged template at the time the user first ran setup.
        # Additive only; never overwrites existing files. Runs
        # BEFORE the validation hook so the new skills are in the
        # catalog when validation walks it.
        _maybe_install_missing_kolsch_skills(io)
        # Migrate platform.yaml's authz rules from the legacy
        # `{id, who, what, effect}` regex shape to the new
        # `{what, pattern, effect}` glob shape. Runs AFTER the
        # kolsch-skills drop-in so the catalog walk has every tool
        # the migrator's `tool.<name>` heuristic needs to resolve
        # to `<bundle>/<name>`.
        await _maybe_migrate_authz_rules_to_qol_schema(io)
        # Guarantee the pre-flight `agent.execute.*` allow rule so
        # Step E's helper-agent smoke test doesn't fail with a
        # confusing agent.unauthorized error on workspaces that
        # hand-narrowed the rule set. Silent no-op when coverage is
        # already present. Must run BEFORE Step E.
        _maybe_ensure_agent_execute_allow(io)
        # Make `list_files(path=".")` work in the workspace root.
        # Adds `.` to `file_access.allowed_paths` if missing so the
        # natural workspace-inspection call doesn't default-deny.
        _maybe_add_workspace_root_to_file_access(io)
        # Advisory skill validation. Warning-only — never blocks
        # setup so a user mid-editing a SKILL.md doesn't get stuck.
        await _maybe_warn_invalid_skills(io)
        # qol-133 step 2 — read-only, one-time advisory pointing an
        # existing workspace without `hooks:` at the docs. Never
        # mutates platform.yaml; never blocks setup.
        _maybe_notify_hooks_available(io)
        return _SKIPPED

    io.root.mkdir(parents=True, exist_ok=True)
    io.out.write(f"Creating workspace at {io.root}\n")
    for name in _WORKSPACE_DIRS:
        (io.root / name).mkdir(parents=True, exist_ok=True)
        io.out.write(f"  ✓ {name}/\n")

    yaml_path.write_text(
        dump(_setup_platform_payload()) + "\n" + _HOOKS_EXAMPLE_BLOCK,
        encoding="utf-8",
    )
    io.out.write("  ✓ platform.yaml\n")

    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")
        io.out.write("  ✓ .env\n")
    return _PROCEEDED


# ---------------------------------------------------------------------------
# Step C — LLM provider (calls existing cmd_connect)
# ---------------------------------------------------------------------------


async def _step_c_provider(io: _SetupIO, *, non_interactive: bool) -> str:
    yaml_path = io.root / "platform.yaml"
    payload = _read_yaml(yaml_path)
    connected = list((payload.get("llm_providers") or {}).keys())
    if connected:
        io.out.write(
            f"✓ provider(s) already connected: {', '.join(sorted(connected))}\n"
        )
        return _SKIPPED

    if non_interactive:
        return await _step_c_non_interactive(io)
    return await _step_c_interactive(io)


async def _step_c_interactive(io: _SetupIO) -> str:
    choice = io.prompter.choose(
        "LLM provider — at least one is needed to run real agents.",
        [
            "[r] openrouter — recommended (one key → most models + metadata)",
            "[c] opencode",
            "[s] skip — I'll connect a provider later",
        ],
        default="s",
    )
    provider = {"r": "openrouter", "c": "opencode"}.get(choice)
    if provider is None:
        io.out.write("  → skipping; the starter agent will use the mock LLM.\n")
        return _SKIPPED
    connect_io = io.connect_io_factory(io.root, provider, None)
    from cli.llm_commands import cmd_connect  # noqa: PLC0415

    rc = await cmd_connect(
        argparse.Namespace(provider=provider, root=io.root), connect_io
    )
    if rc != 0:
        io.err.write(f"  → connect {provider} failed (rc={rc})\n")
        return "partial"
    return _PROCEEDED


async def _step_c_non_interactive(io: _SetupIO) -> str:
    provider = os.environ.get("KOLSCH_PROVIDER", "").strip()
    key = os.environ.get("KOLSCH_PROVIDER_KEY", "").strip()
    if not provider or not key:
        io.out.write(
            "  (no KOLSCH_PROVIDER / KOLSCH_PROVIDER_KEY env vars; "
            "starter agent will use the mock LLM)\n"
        )
        return _SKIPPED
    connect_io = io.connect_io_factory(io.root, provider, key)
    from cli.llm_commands import cmd_connect  # noqa: PLC0415

    rc = await cmd_connect(
        argparse.Namespace(provider=provider, root=io.root), connect_io
    )
    if rc != 0:
        io.err.write(f"  → connect {provider} from env vars failed (rc={rc})\n")
        return "partial"
    io.out.write(f"  ✓ connected {provider} from environment\n")
    return _PROCEEDED


# ---------------------------------------------------------------------------
# Step D — starter agent
# ---------------------------------------------------------------------------


_STARTER_BUNDLE = "kolsch"
_STARTER_AGENT = "helper"

# The packaged template the bundle is materialized from: ship the
# four tool files as a copyable template under src/, copy at setup
# time.
_STARTER_BUNDLE_TEMPLATE_DIR = Path(__file__).parent / "_starter_bundle" / "kolsch"


async def _step_d_starter_agent(io: _SetupIO, *, non_interactive: bool) -> str:
    """Materialize the kolsch bundle from the packaged template.

    Copies the packaged template at
    `src/cli/_starter_bundle/kolsch/` (bundle.yaml +
    agents/helper.yaml + the four tool `.py` files) into
    `<workspace>/bundles/kolsch/` verbatim. The chosen model lands
    in `platform.yaml`'s `defaults.model` block — the helper's
    `model:` field is intentionally absent so the kernel's
    defaults composition (the assembler's partial order + the
    resolver's RFC 7386 merge) supplies the value.
    """
    bundle_dir = io.root / "bundles" / _STARTER_BUNDLE
    agent_path = bundle_dir / "agents" / f"{_STARTER_AGENT}.yaml"

    if agent_path.exists():
        io.out.write(f"✓ starter agent already at {agent_path}\n")
        return _SKIPPED

    model = _pick_starter_model(io)
    if not non_interactive:
        confirm = io.prompter.confirm(
            f"Create starter agent at {agent_path}?", default=True
        )
        if not confirm:
            io.out.write("  → skipped starter agent\n")
            return _SKIPPED

    _copy_kolsch_bundle(io.root)
    _write_defaults_model(io.root / "platform.yaml", model)
    io.out.write(f"  ✓ materialized bundle at {bundle_dir.relative_to(io.root)}/\n")
    io.out.write(f"  ✓ wrote starter agent at {agent_path.relative_to(io.root)}\n")
    _announce_picked_model(io, model)
    return _PROCEEDED


def _copy_kolsch_bundle(root: Path) -> None:
    """Copy the packaged kolsch bundle template into the workspace.

    The `tools/*.py` files are copied verbatim — they're standalone
    Python that the asset catalog discovers + the Tools subsystem
    registers per the standard bundle-tool path. The helper agent
    file is also a verbatim copy; the model value lives in
    `platform.yaml`'s `defaults.model`, not the agent file.

    On the install path (workspace absent), also writes
    `.kolsch/bundle-baseline.json` (qol-089) recording the sha256 of
    every shipped file — the oracle a later `kolsch doctor` uses to
    classify drift. If the workspace bundle already exists, this is
    still idempotent: no copy, no baseline overwrite. Drift *repair*
    is the concern of qol-093 (`kolsch setup --sync-bundle`); this
    function is the install-only path.
    """
    target = root / "bundles" / _STARTER_BUNDLE
    if target.exists():
        # Idempotent — leave existing bundle alone. Drift detection
        # lives at `kolsch doctor`; drift repair at
        # `kolsch setup --sync-bundle` (qol-093).
        return

    if not _STARTER_BUNDLE_TEMPLATE_DIR.exists():
        msg = (
            "packaged kolsch bundle template missing at "
            f"{_STARTER_BUNDLE_TEMPLATE_DIR}; installation may be corrupted"
        )
        raise RuntimeError(msg)

    shutil.copytree(_STARTER_BUNDLE_TEMPLATE_DIR, target)
    # qol-089 — record what we just shipped so `kolsch doctor` can
    # detect drift later. Import lazily so `cli.main` doesn't pay the
    # module-load cost on every subcommand.
    from cli._bundle_baseline import write_baseline  # noqa: PLC0415

    write_baseline(root, bundle_dir=target)


def _write_defaults_model(yaml_path: Path, model: str | dict[str, str]) -> None:
    """Write the picked model into `platform.yaml`'s `defaults.model`.

    Setup writes the chosen model to the workspace's
    `defaults.model` so the kernel's defaults composition supplies
    the agent's resolved `model:` field. Alias strings are written
    verbatim; canonical `{provider, model}` dicts are written
    as-is.

    qol-133: uses `dump_preserving_trailing_comment_block` (not raw
    `dump()`) so a trailing, fully-commented scaffold block — e.g.
    the fresh-install `hooks:` example — already on disk survives
    this rewrite. A no-op on a workspace with no such block.
    """
    previous_text = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
    payload = _read_yaml(yaml_path)
    defaults = payload.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    defaults["model"] = model
    payload["defaults"] = defaults
    new_text = dump_preserving_trailing_comment_block(previous_text, payload)
    yaml_path.write_text(new_text, encoding="utf-8")


def _announce_picked_model(io: _SetupIO, model: str | dict[str, str]) -> None:
    """Tell the user (clearly) which model the starter uses + why.

    Three cases:

    - Mock fallback: announce loudly that no usable provider was
      available; the user needs to connect one to get real answers.
    - Picked from a connected provider: name the provider + model.
    - Picked alias: name the alias + what it resolves to.
    """
    if isinstance(model, dict) and model.get("provider") == "mock":
        io.out.write(
            "  ⚠ no usable provider was connected — the starter agent "
            "uses the built-in mock LLM (echoes input back). Run "
            "`kolsch connect <provider>` to swap in real answers.\n"
        )
        return
    if isinstance(model, dict):
        io.out.write(f"  → starter agent uses {model['provider']}/{model['model']}\n")
        return
    # Alias string.
    io.out.write(f"  → starter agent uses alias {model!r}\n")


_ALIAS_PRIORITY: tuple[str, ...] = ("opus", "smart", "cheap")
"""Deployment-alias preference order. `opus` first because it's the
historic recommendation; `smart` and `cheap` are the two other
documented aliases. Aliases outside this list fall through to
alphabetical."""

_PROVIDER_PRIORITY: tuple[str, ...] = ("openrouter", "opencode")
"""Provider preference order for the raw-discovery fallback when no
alias is present. OpenRouter first because it serves as the metadata
source and is the recommended primary provider per the first-agent
tutorial."""


def _pick_starter_model(io: _SetupIO) -> str | dict[str, str]:
    """Choose the model field value for the starter agent.

    Prefers a deployment alias from `platform.yaml`'s
    `model_aliases` over a raw discovered model id whenever any
    alias exists. When the user has OpenRouter aliases like `opus`
    + `smart` and also has OpenCode connected, the user's curated
    aliases win over the first-discovered raw OpenCode model.

    Tie-breaker order:

    1. Aliases. Within the aliases dict:
       a. `opus` if present (historic recommendation).
       b. `smart` if present (second documented alias).
       c. `cheap` if present (third documented alias).
       d. First alphabetical alias otherwise.
    2. Raw `{provider, model}` from discovered providers, when no
       alias exists. Within discovered providers:
       a. `openrouter` first (the recommended primary; metadata
          source).
       b. `opencode` second (the secondary documented provider).
       c. Other providers alphabetically.
       Within each provider, the first model id from its `models`
       list.
    3. `{"provider": "mock", "model": "mock-model"}` if no provider
       yielded a model.
    """
    payload = _read_yaml(io.root / "platform.yaml")
    providers_raw = payload.get("llm_providers")
    providers: dict[str, dict[str, Any]] = {}
    if isinstance(providers_raw, dict):
        for name, entry in providers_raw.items():
            if isinstance(entry, dict):
                providers[name] = entry
    aliases_raw = payload.get("model_aliases")
    aliases: dict[str, str] = aliases_raw if isinstance(aliases_raw, dict) else {}

    # 1) Aliases win whenever any exist. Apply the documented
    # tie-breaker order.
    if aliases:
        for preferred in _ALIAS_PRIORITY:
            if preferred in aliases:
                return preferred
        # Fall through to alphabetical for any alias outside the
        # documented set.
        return sorted(aliases.keys())[0]

    # 2) Raw discovery fallback. Order providers by documented
    # priority, then alphabetically for unknown providers.
    def _provider_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        name = item[0]
        try:
            rank = _PROVIDER_PRIORITY.index(name)
        except ValueError:
            rank = len(_PROVIDER_PRIORITY)
        return (rank, name)

    for name, entry in sorted(providers.items(), key=_provider_key):
        model_id = _first_model_id(entry)
        if model_id is None:
            continue
        return {"provider": name, "model": model_id}

    # 3) Nothing connected — fall back to the mock LLM so the starter
    # agent is still runnable.
    return {"provider": "mock", "model": "mock-model"}


def _first_model_id(entry: dict[str, Any]) -> str | None:
    """Return the first non-empty model id from a provider's `models` list.

    Returns `None` if the provider has none.
    """
    raw_models = entry.get("models")
    if not isinstance(raw_models, list):
        return None
    for m in raw_models:
        if isinstance(m, dict):
            mid = m.get("id")
            if isinstance(mid, str) and mid:
                return mid
    return None


# ---------------------------------------------------------------------------
# Step E — test run
# ---------------------------------------------------------------------------


async def _step_e_test_run(io: _SetupIO, *, non_interactive: bool) -> _StepEResult:
    """Run the starter agent once to verify the install.

    Returns a `_StepEResult` so the orchestrator can branch on the
    precise outcome (pass / fail / skipped) for both the banner
    and the exit code.
    """
    agent_ref = f"{_STARTER_BUNDLE}/agents/{_STARTER_AGENT}"
    agent_path = (
        io.root / "bundles" / _STARTER_BUNDLE / "agents" / f"{_STARTER_AGENT}.yaml"
    )
    if not agent_path.exists():
        io.out.write("  (no starter agent to test; skipping test run)\n")
        return _StepEResult(outcome=_StepEOutcome.SKIPPED)
    if not non_interactive:
        run_it = io.prompter.confirm(
            "Run the helper agent once to confirm everything works?",
            default=True,
        )
        if not run_it:
            io.out.write("  → skipped test run\n")
            return _StepEResult(outcome=_StepEOutcome.SKIPPED)
    io.out.write(f"Running {agent_ref}...\n")
    result = await io.run_handler(
        argparse.Namespace(
            root=io.root,
            agent_ref=agent_ref,
            input=["content=hello"],
            stream=False,
            session=None,
        )
    )
    if result.rc != 0:
        # The hint surfaces via the partial-completion banner; don't
        # write a canned diagnostic here — the orchestrator owns
        # the user-facing message so the wording is in one place.
        code = result.error.get("type") if result.error is not None else None
        if result.preflight is not None:
            code = result.preflight_code
        return _StepEResult(
            outcome=_StepEOutcome.FAILED,
            failure_code=code if isinstance(code, str) else None,
            session_id=result.session_id,
            error=result.error,
            preflight=result.preflight,
        )
    io.out.write("  ✓ test run completed successfully\n")
    return _StepEResult(outcome=_StepEOutcome.PASSED)


# ---------------------------------------------------------------------------
# Step F — banner
# ---------------------------------------------------------------------------


def _step_f_banner_success(io: _SetupIO, *, test_skipped: bool = False) -> None:
    """The success banner — printed on Step E pass or skipped.

    A user pressing `[n]` on the test-run prompt is full success;
    the banner suffixes `(test skipped)` so the user knows nothing
    was actually exchanged but the workspace is ready.
    """
    agent_ref = f"{_STARTER_BUNDLE}/agents/{_STARTER_AGENT}"
    io.out.write("\n")
    if test_skipped:
        io.out.write("Kolsch is ready (test skipped).\n\n")
    else:
        io.out.write("Kolsch is ready.\n\n")
    io.out.write("Try:\n")
    io.out.write(f'  kolsch run {agent_ref} --input content="<your prompt>"\n')
    io.out.write("  kolsch sessions list\n")
    io.out.write("  kolsch --help\n\n")
    io.out.write(f"Workspace: {io.root}\n")
    io.out.write("Docs:      docs/tutorials/first-agent.md\n")


def _step_f_banner_failure(
    io: _SetupIO, result: _StepEResult, summary: _StepSummary
) -> None:
    """The partial-completion banner — printed when Step E failed.

    Rendered only from what setup actually observed: the B/D step
    results (`summary`) and the test run's session id + durable
    `thread.exchange_failed` error envelope (`result`). Never prints
    "Kolsch is ready" on this path; the user must see that something
    needs attention. Never prints `detail.traceback`.
    """
    agent_ref = f"{_STARTER_BUNDLE}/agents/{_STARTER_AGENT}"
    hint = _classify_failure(result.failure_code)
    error = result.error or {}
    workspace_status = "created" if summary.workspace_created else "already initialized"
    agent_status = "created" if summary.agent_created else "already present"
    io.out.write("\n")
    io.out.write("Kolsch setup completed partially — test run failed.\n\n")
    io.out.write(f"Workspace: {io.root}  ({workspace_status})\n")
    io.out.write(f"Agent:     {agent_ref}  ({agent_status})\n")
    if result.session_id is not None:
        io.out.write(f"Session:   {result.session_id}\n")
    if result.preflight is not None:
        # No exchange ran (bug-148): say so rather than the
        # exchange-failed hint.
        cause = f"the test run did not start: {_one_line(result.preflight)}"
        if result.failure_code:
            cause = f"{result.failure_code} — {cause}"
        io.out.write(f"Cause:     {cause}\n")
    elif result.failure_code:
        io.out.write(f"Cause:     {result.failure_code} — {hint}\n")
    else:
        io.out.write(f"Cause:     {hint}\n")
    message = error.get("message")
    if isinstance(message, str) and message:
        io.out.write(f"Error:     {_one_line(message)}\n")
    original = _original_error_text(error)
    if original is not None:
        io.out.write(f"Detail:    {_one_line(original)}\n")
    io.out.write("\nNext steps:\n")
    if result.session_id is not None:
        io.out.write(
            f"  kolsch sessions events {result.session_id}"
            "   # full event log of the failed run\n"
        )
    elif result.preflight is not None:
        io.out.write(
            "  kolsch doctor                        # diagnose the workspace\n"
        )
    else:
        io.out.write(
            "  kolsch sessions list                 # find the failed session\n"
        )
    io.out.write(f'  kolsch run {agent_ref} --input content="<retry>"\n')
    io.out.write("\n")
    io.out.write("Docs: docs/tutorials/first-agent.md\n")


def _original_error_text(error: dict[str, Any]) -> str | None:
    """The wrapped exception's own text from a durable error envelope.

    `PlatformInternalError` / `WrappedError` carry it as
    `detail["args"][0]`; returns it only when that is a non-empty
    string. Deliberately never reads `detail["traceback"]`.
    """
    detail = error.get("detail")
    if not isinstance(detail, dict):
        return None
    args = detail.get("args")
    if not isinstance(args, list) or not args:
        return None
    first = args[0]
    if not isinstance(first, str) or not first.strip():
        return None
    return first


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _setup_platform_payload() -> dict[str, Any]:
    """The platform.yaml setup writes.

    Starts from
    [`default_platform_payload`][assets.platform_config.default_platform_payload]
    and applies two overrides:

    1. `authorization.config.rules` is the workspace-only default
       set — read_file / list_files silently allowed; write_file /
       bash gated by the `prompt` effect; protected
       `rl_default_deny` tail.
    2. `defaults` is overridden to empty: the stock
       `defaults.memory` block doesn't fit the kernel's
       `ResolvedAgentConfig` shape at the top level.
    """
    payload = _stock_defaults()
    payload["defaults"] = {}
    payload["authorization"] = {
        "variant": "rule_based",
        "config": {"rules": _workspace_only_default_rules()},
    }
    return payload


def _workspace_only_default_rules() -> list[dict[str, str]]:
    """The workspace-only default rules.

    `what` is the 3-seg catalog ref `<bundle>/tools/<name>` for
    builtin tools, the system action namespace
    (`agent.execute.*`) for the engine pre-flight, and the
    catch-all `*` for the default-deny tail. New workspaces start
    in the 3-seg canonical form so the action matches the rule
    without translation.

    Includes an `agent.execute.*` allow at the top so the engine's
    pre-flight authz check passes — without this the default-deny
    tail would block every exchange.
    """
    return [
        # Pre-flight gate — the engine requires agent.execute.<id>
        # to run any exchange. Default to allow; users tighten by
        # adding a more specific deny before this rule.
        {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
        # The canonical builtins — owned by the kolsch bundle.
        {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/list_files", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/write_file", "pattern": "*", "effect": "prompt"},
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "prompt"},
        # qol-fetch-url-tool — network access is a fresh capability;
        # conservative default per bash/write_file.
        {"what": "kolsch/tools/fetch_url", "pattern": "*", "effect": "prompt"},
        # qol-browse-url-tool — Tier 2 (opt-in `kolsch[browse]`
        # extra). Same conservative prompt default as fetch_url so the
        # chat surface asks before the agent drives headless Chromium.
        {"what": "kolsch/tools/browse_url", "pattern": "*", "effect": "prompt"},
        # spec-136: `jev_decide` sends workspace data (the `state`) to a
        # third party (OpenRouter / TypeSafe) — same conservative
        # `prompt` default as fetch_url.
        {"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": "prompt"},
        # bug-024: workflow-service tools. `run_workflow` opens a
        # WorkflowCard AND queues a run; conservative `prompt` so the
        # user confirms the run before any nodes fire. `workflow_status`
        # is read-only; safe to `allow`. `cancel_workflow` is user-
        # initiated safety; `allow` so the model can react to an Esc
        # or an explicit "cancel that" instruction without a modal.
        {"what": "kolsch/tools/run_workflow", "pattern": "*", "effect": "prompt"},
        {"what": "kolsch/tools/workflow_status", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/cancel_workflow", "pattern": "*", "effect": "allow"},
        # bug-027: `workflow_inspect` is read-only introspection —
        # the model calls it to discover a workflow's required
        # inputs before `run_workflow`. Safe to `allow`; no side
        # effects, no run started.
        {"what": "kolsch/tools/workflow_inspect", "pattern": "*", "effect": "allow"},
        # Default-deny tail (protected).
        {"what": "*", "pattern": "*", "effect": "deny"},
    ]


# ---------------------------------------------------------------------------
# Authz-rules migration
# ---------------------------------------------------------------------------
#
# Older workspaces carry the legacy allow-all rule set, not the
# current workspace-only defaults. On `kolsch setup` re-run, detect
# the sentinel, prompt the user, and rewrite
# `authorization.config.rules` in place — preserving any
# user-added custom rules in their original relative position.
# Non-interactive re-runs default to "skip" so silent mutation of
# the user's config can't happen via CI.


_ALLOW_ALL_SENTINEL: dict[str, str] = {"who": ".*", "what": ".*", "effect": "allow"}


def _has_pre_phase_2_authz_shape(rules: list[object]) -> bool:
    """Return True when the rule list looks legacy-origin.

    Detects the allow-all sentinel without a default-deny tail.
    Older setup wrote only the sentinel; users may have appended
    their own custom rules after the fact, so the detection
    accepts `[sentinel, ...customs]` rather than `[sentinel]`-only.
    The absence of the `{who:.*, what:.*, effect:deny}` tail is the
    signal: the current defaults always end with that tail.
    """
    if not rules:
        return False
    has_sentinel = any(_is_sentinel_rule(r) for r in rules)
    has_default_deny_tail = _is_default_deny_rule(rules[-1])
    return has_sentinel and not has_default_deny_tail


def _is_sentinel_rule(rule: object) -> bool:
    return (
        isinstance(rule, dict)
        and rule.get("who") == _ALLOW_ALL_SENTINEL["who"]
        and rule.get("what") == _ALLOW_ALL_SENTINEL["what"]
        and rule.get("effect") == _ALLOW_ALL_SENTINEL["effect"]
    )


def _is_default_deny_rule(rule: object) -> bool:
    return (
        isinstance(rule, dict)
        and rule.get("who") == ".*"
        and rule.get("what") == ".*"
        and rule.get("effect") == "deny"
    )


def _migrate_rules_preserving_customs(
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the canonical defaults with non-sentinel rules preserved.

    Customs (rules that are not the allow-all sentinel) survive in
    their original relative position, before the default-deny
    tail. The tail `rl_default_deny` stays last so first-match-wins
    semantics keep the deny as the floor.
    """
    defaults = _workspace_only_default_rules()
    customs = [r for r in existing if not _is_sentinel_rule(r)]
    # The last default is the deny tail; keep it last.
    return defaults[:-1] + customs + defaults[-1:]


def _backup_platform_yaml(yaml_path: Path) -> Path:
    """Copy `yaml_path` to `.platform/platform.yaml.bak.<UTC-timestamp>`.

    UTC + ISO-ish basic format keeps backups sortable and avoids
    timezone ambiguity. Returns the backup path so the caller can
    surface it to the user.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = yaml_path.parent / ".platform"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"platform.yaml.bak.{ts}"
    backup.write_bytes(yaml_path.read_bytes())
    return backup


_MIGRATION_PROMPT_OPTIONS: list[str] = [
    "[m] migrate to canonical defaults",
    "[k] keep mine — no changes",
    "[d] diff first",
    "[s] skip — decide later",
]
_MIGRATION_PROMPT_DEFAULT = "s"


def _maybe_migrate_authz_rules(
    io: _SetupIO, yaml_path: Path, *, non_interactive: bool
) -> None:
    """Detect the legacy allow-all sentinel and prompt for migration.

    No-op when the workspace already has the canonical defaults
    (or any other non-sentinel rule set). Non-interactive runs
    default to "skip" so CI re-runs never mutate the user's config
    silently; migration is opt-in.
    """
    payload = _read_yaml(yaml_path)
    authz = payload.get("authorization") or {}
    if authz.get("variant") != "rule_based":
        return
    rules = (authz.get("config") or {}).get("rules") or []
    if not _has_pre_phase_2_authz_shape(rules):
        return

    if non_interactive:
        io.out.write(
            "  ⚠ this workspace has the old allow-all rule set. "
            "Re-run setup interactively to migrate to the canonical "
            "defaults, or edit platform.yaml by hand.\n"
        )
        return

    choice = io.prompter.choose(
        "This workspace has the old allow-all rule set. The current "
        "default is workspace-only with prompt-on-bash. Migrate?",
        _MIGRATION_PROMPT_OPTIONS,
        default=_MIGRATION_PROMPT_DEFAULT,
    )
    while choice == "d":
        _print_migration_diff(io, rules)
        choice = io.prompter.choose(
            "Choose:",
            _MIGRATION_PROMPT_OPTIONS,
            default=_MIGRATION_PROMPT_DEFAULT,
        )

    if choice == "m":
        backup = _backup_platform_yaml(yaml_path)
        previous_text = yaml_path.read_text(encoding="utf-8")
        new_rules = _migrate_rules_preserving_customs(rules)
        payload["authorization"]["config"]["rules"] = new_rules
        yaml_path.write_text(
            dump_preserving_trailing_comment_block(previous_text, payload),
            encoding="utf-8",
        )
        io.out.write("  ✓ migrated authorization rules → canonical defaults\n")
        io.out.write(f"  ✓ backup written to {backup.relative_to(yaml_path.parent)}\n")
    elif choice == "k":
        io.out.write("  → keeping current rules; no changes written\n")
    else:  # "s" or any unrecognised default — defer
        io.out.write("  → deferred; will prompt again on the next setup run\n")


async def _maybe_migrate_authz_rules_to_qol_schema(io: _SetupIO) -> None:
    """Migrate `platform.yaml` rules to the three-field schema.

    On `kolsch setup` re-run:

    1. Build the catalog so the migrator can resolve `tool.<name>`
       actions to `<bundle>/tools/<name>` qualified refs.
    2. Run
       [`migrate_authz_rules`][cli._migrations.authz_rules.migrate_authz_rules]
       over the existing rule list.
    3. For each rule that couldn't be migrated, emit a
       [`ConfigDeprecatedRegexRule`][errors.ConfigDeprecatedRegexRule]
       Recoverable warning via stdout.
    4. If anything migrated, atomically rewrite the YAML.

    Silent no-op when:

    - The workspace's rule list is already entirely new-shape (no
      `who` field on any entry).
    - The workspace is a non-rule-based authz variant.

    Never raises — a per-rule failure produces a warning line but
    doesn't abort setup. Migration is opt-out via the existing
    "skip" path on the prior prompt; this path runs unconditionally
    after that prompt has resolved.
    """
    from assets import AssetManagement  # noqa: PLC0415
    from cli._migrations.authz_rules import migrate_authz_rules  # noqa: PLC0415
    from errors import (  # noqa: PLC0415
        ConfigDeprecatedRegexRule,
        ConfigDeprecatedRuleForm,
        PlatformError,
    )

    yaml_path = io.root / "platform.yaml"
    if not yaml_path.is_file():
        return
    payload = _read_yaml(yaml_path)
    authz = payload.get("authorization") or {}
    if authz.get("variant") != "rule_based":
        return
    rules = (authz.get("config") or {}).get("rules") or []
    if not rules:
        return
    # Historical fast-path gated this on the presence of a `who`
    # field (legacy-shape marker); dropped because the migrator now
    # also handles orphaned 3-seg reattributions
    # (bug-authz-rule-caller-bundle-mismatch) which fire on
    # already-new-shape rules. The migrator is O(rules * catalog)
    # and idempotent; on a pristine workspace it produces zero
    # deprecation entries and `converted_count == 0` short-circuits
    # the YAML write below.

    am = AssetManagement(root=io.root)
    try:
        await am.initialize()
        catalog = am.catalog()
    except Exception as exc:  # noqa: BLE001 — never fatal at advisory layer
        io.out.write(
            f"  ⚠ authz schema migration skipped: catalog build failed ({exc})\n",
        )
        return

    result = migrate_authz_rules(rules, catalog=catalog)
    # Nothing changed (everything was unconvertible) → leave the YAML
    # alone but still emit deprecation warnings so the user knows.
    converted_count = sum(
        1 for old, new in zip(rules, result.rules, strict=False) if old != new
    )
    for entry in result.deprecated:
        # Dispatch on `kind` so the right event class fires per
        # deprecation cause: legacy 1-seg regex rules emit
        # ConfigDeprecatedRegexRule; new-shape rules with shape /
        # attribution issues (`two_seg`, `orphan_3seg`) emit
        # ConfigDeprecatedRuleForm. Both are Recoverable warnings.
        if entry.kind in ("two_seg", "orphan_3seg"):
            warn: PlatformError = ConfigDeprecatedRuleForm(
                rule_index=entry.rule_index,
                what=entry.what,
                reason=entry.reason,
            )
        else:
            warn = ConfigDeprecatedRegexRule(
                rule_index=entry.rule_index,
                what=entry.what,
                reason=entry.reason,
            )
        # PlatformError carries the message via `str(exc)` (super().__init__(message)).
        io.out.write(f"  ⚠ {warn}\n")
    if converted_count == 0:
        return
    backup = _backup_platform_yaml(yaml_path)
    previous_text = yaml_path.read_text(encoding="utf-8")
    payload["authorization"]["config"]["rules"] = result.rules
    yaml_path.write_text(
        dump_preserving_trailing_comment_block(previous_text, payload),
        encoding="utf-8",
    )
    io.out.write(
        f"  ✓ migrated {converted_count} authz rule(s) → new schema "
        "(what / pattern / effect)\n",
    )
    io.out.write(
        f"  ✓ backup written to {backup.relative_to(yaml_path.parent)}\n",
    )


def _maybe_add_workspace_root_to_file_access(io: _SetupIO) -> None:
    """Add `.` to `file_access.allowed_paths` if missing.

    The natural inspection call `list_files(path=".")` default-denies
    on older workspaces because the default template only allowed
    subdirectories (`data/`, `bundles/`, `sessions/`, `content/`,
    `logs/`) plus `platform.yaml`. Adding `.` at the front lets
    agents list the workspace root without falling back to `bash ls`.

    Silent no-op when:

    - `platform.yaml` is absent.
    - `file_access` section is absent (catalog-resolved default
      applies; nothing to migrate).
    - `.` is already in `allowed_paths` (already migrated, or
      hand-edited).
    """
    yaml_path = io.root / "platform.yaml"
    if not yaml_path.is_file():
        return
    payload = _read_yaml(yaml_path)
    file_access = payload.get("file_access")
    if not isinstance(file_access, dict):
        return
    allowed = file_access.get("allowed_paths")
    if not isinstance(allowed, list):
        return
    if "." in allowed:
        return
    backup = _backup_platform_yaml(yaml_path)
    previous_text = yaml_path.read_text(encoding="utf-8")
    file_access["allowed_paths"] = [".", *allowed]
    yaml_path.write_text(
        dump_preserving_trailing_comment_block(previous_text, payload),
        encoding="utf-8",
    )
    io.out.write(
        "  ✓ added '.' to file_access.allowed_paths — list_files(\".\") "
        "now works in the workspace root\n",
    )
    io.out.write(
        f"  ✓ backup written to {backup.relative_to(yaml_path.parent)}\n",
    )


_AGENT_EXECUTE_CATCHALL_REGEX_FORMS = frozenset(
    {
        # agent.execute.* namespace catchalls (new + legacy shapes)
        r"agent\.execute\..*",
        r"^agent\.execute\..*$",
        r"^agent\.execute\..*",
        r"agent\.execute\..*$",
        # Phase 1 allow-all sentinels (legacy regex + new-shape glob)
        r".*",
        r"^.*$",
    }
)


def _rule_covers_broad_agent_execute(rule: Any) -> bool:
    r"""Return True when the rule allows the pre-flight `agent.execute.*` check.

    Accepted shapes:

    - New-shape catchall: ``{what: agent.execute.*, effect: allow}``
      (exact string match on `what`).
    - Legacy regex catchall: ``what: ^?agent\.execute\..*$?`` with
      ``effect: allow`` (any anchoring variant).
    - Phase 1 allow-all sentinel: ``{what: .*, effect: allow}`` (or
      ``.*``, ``^.*$``, ``*``). These are strictly broader than
      ``agent.execute.*`` and would match the pre-flight check the
      same way.

    Legacy specific-agent rules (e.g. ``^agent\.execute\.ag_butler$``)
    are NOT treated as broad coverage — they scope to one agent, so
    a different agent (like the helper Step E runs) still hits the
    default-deny tail.

    Non-dict entries and rules without ``allow`` effect return False.
    """
    if not isinstance(rule, dict):
        return False
    if rule.get("effect") != "allow":
        return False
    what = str(rule.get("what", ""))
    return (
        what in {"agent.execute.*", "*"} or what in _AGENT_EXECUTE_CATCHALL_REGEX_FORMS
    )


def _maybe_ensure_agent_execute_allow(io: _SetupIO) -> None:
    r"""Guarantee at least one rule allows the pre-flight `agent.execute.*` check.

    Kolsch's pre-flight authz gate at
    [`engine.preflight.authorize`][engine.engine.Engine.execute] runs
    ONCE per exchange, before any tool call, and checks
    ``allow(principal, "agent.execute.<agent_id>")``. Fresh workspaces
    always include a broad
    ``{what: agent.execute.*, pattern: "*", effect: allow}`` in the
    default rule set (see `_workspace_only_default_rules`). Older
    workspaces — or workspaces where someone hand-narrowed the rule
    set to just one agent, e.g. ``^agent\.execute\.ag_butler$`` for
    a personal butler — can end up with NO rule that authorises the
    helper agent Step E runs as its smoke test. The pre-flight then
    denies and setup fails with a confusing ``agent.unauthorized``.

    This step prepends the broad-allow rule when none of the shapes
    recognised by
    [`_rule_covers_broad_agent_execute`][cli._setup._rule_covers_broad_agent_execute]
    is already present. Silent no-op when coverage is present. Runs
    BEFORE Step E so the smoke test sees the newly-inserted rule.

    Silent no-op when:

    - ``platform.yaml`` is absent.
    - The authz variant isn't ``rule_based``.
    - Coverage is already present.
    """
    yaml_path = io.root / "platform.yaml"
    if not yaml_path.is_file():
        return
    payload = _read_yaml(yaml_path)
    authz = payload.get("authorization") or {}
    if authz.get("variant") != "rule_based":
        return
    rules = (authz.get("config") or {}).get("rules") or []
    if not isinstance(rules, list):
        return

    if any(_rule_covers_broad_agent_execute(r) for r in rules):
        return

    backup = _backup_platform_yaml(yaml_path)
    previous_text = yaml_path.read_text(encoding="utf-8")
    new_rule = {
        "what": "agent.execute.*",
        "pattern": "*",
        "effect": "allow",
    }
    # Prepend so it beats any narrower agent-specific deny that might
    # be lurking above the default-deny tail. Rule order is
    # first-match-wins and this is a default-shape allow, not a
    # policy statement.
    payload["authorization"]["config"]["rules"] = [new_rule, *rules]
    yaml_path.write_text(
        dump_preserving_trailing_comment_block(previous_text, payload),
        encoding="utf-8",
    )
    io.out.write(
        "  ✓ added `agent.execute.*` allow rule — required for "
        "`kolsch run <agent>` pre-flight; see "
        "docs/how-to/permissions.md#how-permissions-gate-an-exchange\n",
    )
    io.out.write(
        f"  ✓ backup written to {backup.relative_to(yaml_path.parent)}\n",
    )


def _maybe_install_missing_kolsch_skills(io: _SetupIO) -> None:
    """Drop in canonical kolsch skills missing from an existing workspace.

    Additive drift handler for the kolsch bundle's `skills/` tree.
    Compares the packaged template's skill set to what's on disk;
    copies the difference. Never overwrites — if the user edited a
    canonical skill, their edits stay. If they deleted one, the
    next setup re-run adds it back (intentional: skills are
    additive content, not config the user owns).

    Silent no-op when:

    - The user has no kolsch bundle (they removed it; respect
      that).
    - The packaged template ships no skills.
    - Every canonical skill is already present.
    """
    import shutil  # noqa: PLC0415

    bundle_dir = io.root / "bundles" / _STARTER_BUNDLE
    if not (bundle_dir / "bundle.yaml").is_file():
        return  # User doesn't have the kolsch bundle; nothing to do.

    template_skills_dir = _STARTER_BUNDLE_TEMPLATE_DIR / "skills"
    if not template_skills_dir.is_dir():
        return  # Template ships no skills; nothing to install.

    skills_target = bundle_dir / "skills"
    skills_target.mkdir(parents=True, exist_ok=True)

    added: list[str] = []
    for source_skill in sorted(template_skills_dir.iterdir()):
        if not source_skill.is_dir():
            continue
        if not (source_skill / "SKILL.md").is_file():
            continue
        target_skill = skills_target / source_skill.name
        if target_skill.exists():
            continue  # Already present (possibly user-edited); leave alone.
        shutil.copytree(source_skill, target_skill)
        added.append(source_skill.name)

    if added:
        io.out.write(
            f"  ✓ installed {len(added)} canonical skill(s): {', '.join(added)}\n",
        )


async def _maybe_warn_invalid_skills(io: _SetupIO) -> None:
    """Skill-validation advisory hook — runs on setup re-runs.

    Builds a fresh `AssetManagement`, walks every catalogued skill,
    and prints a one-line warning per skill that fails
    [`validate_skill_entry_detailed`][cli.skills.validate_skill_entry_detailed].
    Never blocks setup — a user mid-editing a SKILL.md should not
    be locked out of completing setup.

    Silent when the workspace has no skills catalogued.
    """
    from assets import AssetManagement  # noqa: PLC0415
    from cli.skills import validate_skill_entry_detailed  # noqa: PLC0415

    am = AssetManagement(root=io.root)
    try:
        await am.initialize()
    except Exception as exc:  # noqa: BLE001 — never fatal at advisory layer
        io.out.write(f"  ⚠ skill validation skipped: {exc}\n")
        return

    skills = sorted(
        am.list_assets(asset_type="skill"),
        key=lambda e: e.ref,
    )
    if not skills:
        return

    failures: list[tuple[str, list[str]]] = []
    for entry in skills:
        issues = validate_skill_entry_detailed(entry, workspace_root=io.root)
        if issues:
            # Codes only, except frontmatter failures, whose detail
            # (e.g. the YAML error's line/column) is what makes them
            # actionable.
            errs = [
                f"{i.code} ({i.message})"
                if i.code.startswith("frontmatter-")
                else i.code
                for i in issues
            ]
            failures.append((entry.ref, errs))

    if not failures:
        io.out.write(f"  ✓ validated {len(skills)} skill(s)\n")
        return

    io.out.write(
        f"  ⚠ skill validation: {len(failures)} of {len(skills)} "
        "skill(s) have warnings:\n",
    )
    for ref, errs in failures:
        # Show error codes at the setup surface — keeps the
        # banner tight. Users get the full human-readable view via
        # `kolsch skills validate <ref>`.
        io.out.write(f"      {ref}: {', '.join(errs)}\n")
    io.out.write(
        "    (Run `kolsch skills validate` for details; "
        "warnings do not block setup.)\n",
    )


# ---------------------------------------------------------------------------
# qol-133 step 2 — one-time "hooks: exists" notice for existing workspaces
# ---------------------------------------------------------------------------

# Sentinel file: its mere existence means the notice has already been
# shown. Content is a small JSON blob for debuggability only — nothing
# reads it back except `_hooks_notice_shown`, and any read failure
# (missing file, corrupt JSON, wrong shape) is treated identically to
# "not shown yet". Lives alongside `.kolsch/bundle-baseline.json`
# (see `cli._bundle_baseline._BASELINE_REL`).
_HOOKS_NOTICE_MARKER_REL = ".kolsch/hooks-notice-shown"


def _hooks_notice_marker_path(root: Path) -> Path:
    """Return the absolute path to the one-time hooks-notice marker."""
    return root / _HOOKS_NOTICE_MARKER_REL


def _hooks_notice_shown(root: Path) -> bool:
    """True when the one-time hooks-availability notice already fired.

    A missing, unreadable, or unparseable marker file all count as
    "not shown yet" — the failure mode this must fail toward is
    showing the notice one extra time, never permanent silence and
    never a crash.
    """
    path = _hooks_notice_marker_path(root)
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("shown") is True


def _mark_hooks_notice_shown(root: Path) -> None:
    """Atomically record that the one-time hooks notice has fired."""
    path = _hooks_notice_marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {"shown": True, "shown_at": datetime.now(UTC).isoformat()},
                fh,
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _maybe_notify_hooks_available(io: _SetupIO) -> None:
    """Advisory: point an existing, hooks-unaware workspace at the docs.

    spec-131 shipped `platform.yaml`'s `hooks:` section; a workspace
    created before that (or one that never uncommented Step B's
    scaffold — see `_HOOKS_EXAMPLE_BLOCK`) has no way to discover the
    feature short of already knowing to look for it. This is a
    **read-only** advisory, never a migration: it prints a one-line
    pointer to `docs/how-to/event-hooks.md` and never touches
    `platform.yaml`. Never blocks setup.

    Fires at most once ever per workspace, tracked by the
    `.kolsch/hooks-notice-shown` marker — since this step does not
    write `hooks:` for the user, the "missing key" condition never
    self-clears, so without a marker this would nag on every re-run
    forever.

    Silent no-op when:

    - `platform.yaml` is absent.
    - `platform.yaml` already has a `hooks` key (any shape, including
      an empty list) — no marker is written in this case; the
      presence check alone is sufficient to stay silent forever.
    - The one-time marker shows the notice already fired.
    """
    yaml_path = io.root / "platform.yaml"
    if not yaml_path.is_file():
        return
    payload = _read_yaml(yaml_path)
    if "hooks" in payload:
        return
    if _hooks_notice_shown(io.root):
        return

    io.out.write(
        "  ℹ kolsch can run a shell command on events like an approval "  # noqa: RUF001
        "prompt (e.g. a desktop notification) — see "
        "docs/how-to/event-hooks.md to set up a hook.\n",
    )
    _mark_hooks_notice_shown(io.root)


def _print_migration_diff(io: _SetupIO, current_rules: list[dict[str, Any]]) -> None:
    """Show a unified diff of the rules block (current → migrated)."""
    before = dump({"rules": current_rules}).splitlines()
    after = dump(
        {"rules": _migrate_rules_preserving_customs(current_rules)},
    ).splitlines()
    diff = difflib.unified_diff(
        before, after, fromfile="current", tofile="after migration", lineterm=""
    )
    io.out.write("\n")
    for line in diff:
        io.out.write(line + "\n")
    io.out.write("\n")


# ---------------------------------------------------------------------------
# Default-IO factory (production)
# ---------------------------------------------------------------------------


def _default_connect_io_factory(
    root: Path, provider: str, api_key: str | None
) -> _CommandIO:
    """Build a `_CommandIO` for `cmd_connect`.

    When `api_key` is not `None` (the non-interactive env-driven
    path), wraps a prompter that returns the env-provided key for
    any `api_key` field; otherwise falls back to the production
    `StdinPrompter`.
    """
    from cli.llm_commands import (  # noqa: PLC0415
        StdinPrompter,
        _CommandIO,
        make_default_io,
    )
    from llm import get as registry_get  # noqa: PLC0415
    from llm.protocol import PromptField  # noqa: PLC0415

    _ = provider

    class _EnvKeyPrompter:
        """Returns `api_key` from the env-provided value.

        Everything else uses the field's default (good enough for
        OpenRouter, whose only required field is `api_key`;
        OpenCode also asks for `base_url` — defaults are populated
        by the connector's prompt spec).
        """

        def __init__(self, key: str, fallback: StdinPrompter) -> None:
            self._key = key
            self._fallback = fallback

        def ask(self, field: PromptField) -> str:
            """Resolve the field's value, preferring the env-provided api_key."""
            if field.name == "api_key":
                return self._key
            if field.default is not None:
                return field.default
            return self._fallback.ask(field)

    if api_key is None:
        return make_default_io(root=root)
    prompter = _EnvKeyPrompter(api_key, StdinPrompter())
    return _CommandIO(
        root=root,
        out=sys.stdout,
        prompter=prompter,
        connector_factory=registry_get,
    )


def make_default_setup_io(*, root: Path) -> _SetupIO:
    """Production wiring — used by `cli.main._cmd_setup`."""
    # Trigger LLM provider registration before any setup step calls
    # into the registry.
    import llm.providers  # noqa: F401, PLC0415
    from cli._run_failure import format_internal_failure  # noqa: PLC0415
    from cli.main import _run_agent_once, _write_agent_message  # noqa: PLC0415

    async def _run_handler(args: argparse.Namespace) -> _TestRunResult:
        """Run the test exchange through the `kolsch run` seam.

        Setup must not re-implement agent execution. Maps the
        `_RunOutcome` to a `_TestRunResult`: the session id, the
        durable `failed_event.data["error"]` envelope when the exchange
        ended Fatal, and the pre-flight message/code when the run
        stopped before any exchange started. An engine-Fatal failure
        is NOT logged — the durable event and the failure banner carry
        it. Any other caught exception is unexpected and follows
        `kolsch run`'s bug-148 rule: the one-line internal-error
        message on stderr, the traceback logged on the `cli` logger at
        INFO (so on stderr only under `kolsch -v`). On success the
        agent message JSON is echoed to stdout, as `kolsch run` prints
        it.
        """
        outcome = await _run_agent_once(args)
        if outcome.exc is not None and outcome.failed_event is None:
            sys.stderr.write(
                format_internal_failure(
                    outcome.exc, agent_ref=args.agent_ref, workspace_root=root
                )
            )
            logging.getLogger("cli").info(
                "agent execution raised unexpectedly", exc_info=outcome.exc
            )
        if outcome.agent_message is not None:
            _write_agent_message(outcome.agent_message)
        error: dict[str, Any] | None = None
        if outcome.failed_event is not None:
            raw = outcome.failed_event.data.get("error")
            if isinstance(raw, dict):
                error = raw
        preflight = outcome.preflight
        return _TestRunResult(
            rc=outcome.rc,
            session_id=(
                str(outcome.session_id) if outcome.session_id is not None else None
            ),
            error=error,
            preflight=preflight.message if preflight is not None else None,
            preflight_code=preflight.code if preflight is not None else None,
        )

    return _SetupIO(
        root=root,
        out=sys.stdout,
        err=sys.stderr,
        prompter=StdinSetupPrompter(sys.stdout),
        connect_io_factory=_default_connect_io_factory,
        run_handler=_run_handler,
    )

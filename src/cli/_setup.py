"""`kolsch setup` — Phase 1.2 first-run flow.

Six idempotent steps (A-F per patch §2.2). Each step has one
question: "is this already done?" → skip. "do I need to ask?"
→ prompt (or read env for ``--non-interactive``). "ready to
write?" → write atomically.

Step composition rules from the patch:

- Step C MUST call the existing :func:`cli.llm_commands.cmd_connect`;
  setup does not re-implement the connect flow.
- Step E MUST call the existing :func:`cli.main._cmd_run`; setup
  does not re-implement agent execution.
- Setup writes nothing outside the resolved workspace (the .env
  secret-value write is the only exception, per Phase 1.1).
- Each step is interruptible at any prompt; the next setup run
  resumes from the same content-derived state, NOT a state flag.
"""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

import yaml

from assets.platform_config import default_platform_payload as _stock_defaults

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cli.llm_commands import _CommandIO


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


_EXIT_OK = 0
_EXIT_PARTIAL = 4  # Phase 1.2 patch §2.5 — partial setup completion.


# ---------------------------------------------------------------------------
# Step-result token (returned by each step)
# ---------------------------------------------------------------------------


_PROCEEDED = "proceeded"
_SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Step E result — richer than the string tokens above (bug-setup-test-run-
# failure-handling.md edit (a)). Carries the outcome enum + an optional
# failure code so the orchestrator can pick the right banner + a
# classifier-derived hint.
# ---------------------------------------------------------------------------


class _StepEOutcome(StrEnum):
    """How the test exchange ended — drives the banner + exit code in
    the orchestrator. Distinct from the ``proceeded``/``skipped``/
    ``partial`` tokens the other steps return because Step E's outcome
    has finer-grained user-facing behaviour."""

    PASSED = "pass"
    SKIPPED = "skipped"
    FAILED = "fail"


@dataclass(frozen=True)
class _StepEResult:
    """The richer return type from Step E (bug-setup-test-run-failure-
    handling.md edit (a))."""

    outcome: _StepEOutcome
    failure_code: str | None = None
    failure_detail: str | None = None


# ---------------------------------------------------------------------------
# Prompter Protocol — test-injection seam
# ---------------------------------------------------------------------------


class SetupPrompter(Protocol):
    """Three methods cover every prompt shape in the setup flow."""

    def choose(self, question: str, options: list[str], default: str) -> str:
        """Pick one of ``options`` (single-letter answers OK). Returns
        the chosen option string (lowercased). Default fires on empty
        input."""

    def text(self, question: str, default: str = "") -> str:
        """Free-form text. Empty input returns ``default``."""

    def confirm(self, question: str, *, default: bool = True) -> bool:
        """Yes/no. Returns ``default`` on empty input."""


class StdinSetupPrompter:
    """Production prompter — uses stdin/stdout. Lowercases all answers."""

    def __init__(self, out: TextIO) -> None:
        self._out = out

    def choose(self, question: str, options: list[str], default: str) -> str:
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
        suffix = f" [{default}]" if default else ""
        raw = input(f"{question}{suffix}: ").strip()
        return raw or default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        suffix = " [Y/n]" if default else " [y/N]"
        raw = input(f"{question}{suffix} ").strip().lower()
        if not raw:
            return default
        return raw.startswith("y")


class NonInteractivePrompter:
    """Returns defaults for everything; no I/O. Used by ``--non-interactive``."""

    def choose(self, question: str, options: list[str], default: str) -> str:
        _ = question, options
        return default.lower()

    def text(self, question: str, default: str = "") -> str:
        _ = question
        return default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        _ = question
        return default


# ---------------------------------------------------------------------------
# Connect-IO and run-handler types — wired to the existing commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SetupIO:
    """The dependencies setup needs to do its job. Tests inject a
    custom instance to mock HTTP, capture output, and script prompts."""

    root: Path
    out: TextIO
    err: TextIO
    prompter: SetupPrompter
    # Factory that produces the _CommandIO the existing `cmd_connect`
    # needs. Setup creates one per provider it wants to connect.
    # The factory receives the provider name + an optional API key
    # override (used by --non-interactive's env-driven path).
    connect_io_factory: Callable[[Path, str, str | None], _CommandIO]
    # Run-handler — wraps `cli.main._cmd_run`. Used by step E to verify
    # the install with a real exchange. Returns ``(exit_code,
    # failure_code)`` so Step E can pass the failure code to
    # :func:`_classify_failure` for an actionable hint
    # (bug-setup-test-run-failure-handling.md). ``failure_code`` is
    # the canonical Error Model code from the failed event's
    # ``data.error.type`` when an exchange ended Fatal; ``None``
    # otherwise (success / non-engine failures).
    run_handler: Callable[[argparse.Namespace], Awaitable[tuple[int, str | None]]]


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


async def run_setup(args: argparse.Namespace, io: _SetupIO) -> int:
    """Walk the six steps. Returns exit code (0 ok, 4 partial).

    Bug-fix (bug-setup-test-run-failure-handling.md edit (b)): Step F
    is gated on Step E's outcome — the success banner only fires when
    the test exchange actually passed (or was actively skipped). A
    failed test produces a partial-completion banner that names the
    failed step, the workspace path, and a classifier-driven hint
    derived from the failure code, NOT a hardcoded "check provider
    credentials" string.
    """
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

    for step_name, step_fn in (
        ("A workspace location", _step_a_workspace_location),
        ("B workspace skeleton", _step_b_skeleton),
        ("C LLM provider", _step_c_provider),
        ("D starter agent", _step_d_starter_agent),
    ):
        try:
            result = await step_fn(io, non_interactive=non_interactive)
        except KeyboardInterrupt:
            io.err.write(f"\nsetup aborted at step {step_name}\n")
            return _EXIT_PARTIAL
        if result == "partial":
            partial = True

    # Step E is handled separately because its outcome drives the banner
    # + exit code (bug-setup-test-run-failure-handling.md).
    try:
        step_e_result = await _step_e_test_run(io, non_interactive=non_interactive)
    except KeyboardInterrupt:
        io.err.write("\nsetup aborted at step E test run\n")
        return _EXIT_PARTIAL

    # Branch on Step E's outcome. A FAILED test run prints the
    # partial-completion banner and returns 4, regardless of whether
    # earlier steps reported partial.
    if step_e_result.outcome is _StepEOutcome.FAILED:
        _step_f_banner_failure(io, step_e_result)
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
# Edit (c) — _classify_failure (bug-setup-test-run-failure-handling.md
# acceptance criterion 4)
# ---------------------------------------------------------------------------


def _classify_failure(failure_code: str | None) -> str:
    """Map a failed-event error code to a user-facing actionable hint.

    Single source of truth — extend the table as Phase 1.3 lands more
    ``model.*`` codes or as new failure modes appear. Unknown codes
    (including ``None``) fall through to the generic hint.

    Mapping is per bug-setup-test-run-failure-handling.md §3 acceptance
    criterion 4:

    - ``model.server_error`` / 5xx → "the model returned a 5xx; usually
      transient. Try again, or pick a different model by editing the
      agent's ``model:`` field (or `kolsch permissions`)."
    - ``model.authentication_failed`` / 401/403 → "credentials failed;
      re-run `kolsch connect <provider>`."
    - ``llm.provider_not_configured`` → "the agent's provider isn't
      connected; run `kolsch connect <provider>`."
    - ``model.timeout`` → "the model took too long to respond. Try
      again or pick a faster model."
    - Default (any other code) → "the test exchange failed; check
      `kolsch sessions events <id>` for details, then re-run setup or
      `kolsch run` directly."
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
            "Check `kolsch sessions events <id>` for the response detail."
        ),
    }
    return table.get(
        failure_code or "",
        "the test exchange failed; check `kolsch sessions events <id>` "
        "for details, then re-run setup or `kolsch run` directly.",
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


async def _step_b_skeleton(io: _SetupIO, *, non_interactive: bool) -> str:
    yaml_path = io.root / "platform.yaml"
    env_path = io.root / ".env"

    already = yaml_path.is_file()
    if already:
        io.out.write(f"✓ workspace already initialized at {io.root}\n")
        # bug-chat-permissions-and-console-leak Step C / E-BUG-CHAT-001
        # resolution (option a): when re-run against a workspace whose
        # platform.yaml still carries the Phase 1 allow-all sentinel,
        # offer to migrate to the Phase 2.0 §3.1 defaults.
        _maybe_migrate_authz_rules(io, yaml_path, non_interactive=non_interactive)
        # Phase 2.4 Step 9 — drop in canonical kolsch skills that
        # weren't in the packaged template at the time the user first
        # ran setup. Additive only; never overwrites existing files.
        # Runs BEFORE the validation hook so the new skills are in the
        # catalog when validation walks it.
        _maybe_install_missing_kolsch_skills(io)
        # qol-permission-system-human-readable.md Step 4 (carryover
        # E-QOL-PERMSYS-002) — migrate platform.yaml's authz rules
        # from the legacy `{id, who, what, effect}` regex shape to
        # the new `{what, pattern, effect}` glob shape. Runs AFTER
        # the kolsch-skills drop-in so the catalog walk has every
        # tool the migrator's `tool.<name>` heuristic needs to
        # resolve to `<bundle>/<name>`.
        await _maybe_migrate_authz_rules_to_qol_schema(io)
        # Step 6 (skills-subsystem.md Draft 0.2 follow-up): advisory
        # skill validation. Warning-only — never blocks setup so a
        # user mid-editing a SKILL.md doesn't get stuck.
        await _maybe_warn_invalid_skills(io)
        return _SKIPPED

    io.root.mkdir(parents=True, exist_ok=True)
    io.out.write(f"Creating workspace at {io.root}\n")
    for name in _WORKSPACE_DIRS:
        (io.root / name).mkdir(parents=True, exist_ok=True)
        io.out.write(f"  ✓ {name}/\n")

    yaml_path.write_text(
        yaml.safe_dump(_setup_platform_payload(), sort_keys=False),
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
        io.out.write("  → skipping; the starter agent will use the fake LLM.\n")
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
            "starter agent will use the fake LLM)\n"
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


_STARTER_BUNDLE = "kolsch"  # Phase 2.0 (patch §1.5) — was "starter" in 1.2.
_STARTER_AGENT = "helper"

# The packaged template the bundle is materialized from (Phase 2.0
# coordinator resolution to E-2.0-001 — Option A: ship the four tool
# files as a copyable template under src/, copy at setup time).
_STARTER_BUNDLE_TEMPLATE_DIR = Path(__file__).parent / "_starter_bundle" / "kolsch"


async def _step_d_starter_agent(io: _SetupIO, *, non_interactive: bool) -> str:
    """Materialize the kolsch bundle from the packaged template.

    Phase 2.0 (TOOL-R10, coordinator resolution E-2.0-001 Option A):
    copies the packaged template at ``src/cli/_starter_bundle/kolsch/``
    (bundle.yaml + agents/helper.yaml + the four tool .py files) into
    ``<workspace>/bundles/kolsch/`` verbatim. The chosen model lands in
    ``platform.yaml``'s ``defaults.model`` block — the helper's
    ``model:`` field is intentionally absent so the kernel's defaults
    composition (`assets/assembler.py:35` partial order +
    `kernel/resolver.py` RFC 7386 merge) supplies the value
    (E-BUG-MODELLINE-001 option a-prime).
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

    The four tools/*.py files are copied verbatim — they're standalone
    Python that the asset catalog discovers + the Tools subsystem
    registers per the standard bundle-tool path (per the Phase 2.0
    coordinator resolution E-2.0-001 Option A). The helper agent file
    is also a verbatim copy; per E-BUG-MODELLINE-001 the model value
    lives in ``platform.yaml``'s ``defaults.model``, not the agent
    file.
    """
    target = root / "bundles" / _STARTER_BUNDLE
    if target.exists():
        # Idempotent — leave existing bundle alone. (Drift detection
        # in Phase 2.3 will surface mismatches; setup doesn't overwrite.)
        return

    if not _STARTER_BUNDLE_TEMPLATE_DIR.exists():
        msg = (
            "packaged kolsch bundle template missing at "
            f"{_STARTER_BUNDLE_TEMPLATE_DIR}; installation may be corrupted"
        )
        raise RuntimeError(msg)

    shutil.copytree(_STARTER_BUNDLE_TEMPLATE_DIR, target)


def _write_defaults_model(yaml_path: Path, model: str | dict[str, str]) -> None:
    """Write the picked model into ``platform.yaml``'s ``defaults.model``.

    E-BUG-MODELLINE-001 option (a-prime): instead of substituting a
    placeholder in the starter helper.yaml, setup writes the chosen
    model to the workspace's ``defaults.model`` so the kernel's
    defaults composition supplies the agent's resolved ``model:``
    field. Alias strings are written verbatim; canonical
    ``{provider, model}`` dicts are written as-is.
    """
    payload = _read_yaml(yaml_path)
    defaults = payload.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    defaults["model"] = model
    payload["defaults"] = defaults
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _announce_picked_model(io: _SetupIO, model: str | dict[str, str]) -> None:
    """Tell the user (clearly) which model the starter uses + why.

    Three cases:

    - Fake fallback: announce loudly that no usable provider was
      available; the user needs to connect one to get real answers.
    - Picked from a connected provider: name the provider + model.
    - Picked alias: name the alias + what it resolves to.
    """
    if isinstance(model, dict) and model.get("provider") == "fake":
        io.out.write(
            "  ⚠ no usable provider was connected — the starter agent "
            "uses the built-in fake LLM (echoes input back). Run "
            "`kolsch connect <provider>` to swap in real answers.\n"
        )
        return
    if isinstance(model, dict):
        io.out.write(f"  → starter agent uses {model['provider']}/{model['model']}\n")
        return
    # Alias string.
    io.out.write(f"  → starter agent uses alias {model!r}\n")


_ALIAS_PRIORITY: tuple[str, ...] = ("opus", "smart", "cheap")
"""Deployment-alias preference order (bug-setup-test-run-failure-
handling.md acceptance criterion 3). ``opus`` first because it's the
historic recommendation; ``smart`` and ``cheap`` are the two other
documented aliases. Aliases outside this list fall through to
alphabetical."""

_PROVIDER_PRIORITY: tuple[str, ...] = ("openrouter", "opencode")
"""Provider preference order for the raw-discovery fallback when no
alias is present. OpenRouter first because it serves as the metadata
source and is the recommended primary provider per the first-agent
tutorial."""


def _pick_starter_model(io: _SetupIO) -> str | dict[str, str]:
    """Choose the model field value for the starter agent.

    **Bug-fix (bug-setup-test-run-failure-handling.md edit (d)):**
    The picker now prefers a deployment alias from ``platform.yaml``'s
    ``model_aliases`` over a raw discovered model id whenever any alias
    exists. The historic behaviour (raw-discovery from the first
    non-metadata-source provider) was the wrong shape — when the user
    has OpenRouter aliases like ``opus`` + ``smart`` and also has
    OpenCode connected, the user's curated aliases should win over the
    first-discovered raw OpenCode model.

    Tie-breaker order:

    1. Aliases. Within the aliases dict:
       a. ``opus`` if present (historic recommendation).
       b. ``smart`` if present (second documented alias).
       c. ``cheap`` if present (third documented alias).
       d. First alphabetical alias otherwise.
    2. Raw ``{provider, model}`` from discovered providers, when no
       alias exists. Within discovered providers:
       a. ``openrouter`` first (the recommended primary; metadata source).
       b. ``opencode`` second (the secondary documented provider).
       c. Other providers alphabetically.
       Within each provider, the first model id from its ``models`` list.
    3. ``{"provider": "fake", "model": "fake-model"}`` if no
       provider yielded a model.
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

    # 3) Nothing connected — fall back to the fake LLM so the starter
    # agent is still runnable.
    return {"provider": "fake", "model": "fake-model"}


def _first_model_id(entry: dict[str, Any]) -> str | None:
    """Return the first non-empty model id from a provider's `models`
    list, or ``None`` if the provider has none."""
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

    Returns a :class:`_StepEResult` so the orchestrator can branch on
    the precise outcome (pass / fail / skipped) for both the banner
    and the exit code. See bug-setup-test-run-failure-handling.md
    edits (a) and (b).
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
    rc, failure_code = await io.run_handler(
        argparse.Namespace(
            root=io.root,
            agent_ref=agent_ref,
            input=["content=hello"],
            stream=False,
            session=None,
        )
    )
    if rc != 0:
        # The hint surfaces via the partial-completion banner; don't
        # write a canned diagnostic here — the orchestrator owns
        # the user-facing message so the wording is in one place.
        return _StepEResult(
            outcome=_StepEOutcome.FAILED,
            failure_code=failure_code,
            failure_detail=f"test run failed (rc={rc})",
        )
    io.out.write("  ✓ test run completed successfully\n")
    return _StepEResult(outcome=_StepEOutcome.PASSED)


# ---------------------------------------------------------------------------
# Step F — banner
# ---------------------------------------------------------------------------


def _step_f_banner_success(io: _SetupIO, *, test_skipped: bool = False) -> None:
    """The success banner — printed on Step E pass OR skipped.

    Bug-setup-test-run-failure-handling.md acceptance criterion 2 + 3:
    a user pressing ``[n]`` on the test-run prompt is full success; the
    banner suffixes ``(test skipped)`` so the user knows nothing was
    actually exchanged but the workspace IS ready.
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


def _step_f_banner_failure(io: _SetupIO, result: _StepEResult) -> None:
    """The partial-completion banner — printed when Step E failed.

    Bug-setup-test-run-failure-handling.md acceptance criterion 1: name
    the failed step, the workspace path, and the classifier-driven
    hint. NEVER print "Kolsch is ready" on this path; the user must
    see that something needs attention.
    """
    agent_ref = f"{_STARTER_BUNDLE}/agents/{_STARTER_AGENT}"
    hint = _classify_failure(result.failure_code)
    io.out.write("\n")
    io.out.write("Kolsch setup completed partially — test run failed.\n\n")
    io.out.write(f"Workspace: {io.root}  (initialized successfully)\n")
    io.out.write(f"Agent:     {agent_ref}  (created)\n")
    if result.failure_code:
        io.out.write(f"Cause:     {result.failure_code} — {hint}\n")
    else:
        io.out.write(f"Cause:     test exchange failed — {hint}\n")
    if result.failure_detail:
        io.out.write(f"Detail:    {result.failure_detail}\n")
    io.out.write("\nNext steps:\n")
    io.out.write("  kolsch sessions list                 # find the failed session\n")
    io.out.write(f'  kolsch run {agent_ref} --input content="<retry>"\n')
    io.out.write("\n")
    io.out.write("Docs: docs/tutorials/first-agent.md\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _setup_platform_payload() -> dict[str, Any]:
    """The platform.yaml setup writes.

    Starts from :func:`assets.platform_config.default_platform_payload`
    and applies two Phase 2.0 overrides:

    1. ``authorization.config.rules`` is the workspace-only
       default set per Phase 2.0 patch §3.1 — read_file / list_files
       silently allowed; write_file / bash gated by the ``prompt``
       effect; protected ``rl_default_deny`` tail. (TOOL-R10 hard
       rule + Step 22 reviewer Q11.)
    2. ``defaults`` is overridden to empty for the same reason as
       Phase 1.2: the stock ``defaults.memory`` block doesn't fit
       the kernel's ``ResolvedAgentConfig`` shape at the top level.
       Flagged for spec follow-up; see Phase 1.2 Step 18 summary.
    """
    payload = _stock_defaults()
    payload["defaults"] = {}
    payload["authorization"] = {
        "variant": "rule_based",
        "config": {"rules": _workspace_only_default_rules()},
    }
    return payload


def _workspace_only_default_rules() -> list[dict[str, str]]:
    """The workspace-only default rules per Phase 2.0 patch §3.1.

    qol-permission-system-human-readable.md Step 4 — emits the
    new three-field schema ``{what, pattern, effect}`` directly.
    ``what`` is the qualified tool ref (``kolsch/<name>``) for
    builtin tools, the system action namespace
    (``agent.execute.*``) for the engine pre-flight, and the
    catch-all ``*`` for the default-deny tail. The old
    ``{who, what, effect}`` legacy shape still loads via the
    provider's compile dispatch (see
    :class:`providers.authz.Rule`), but new workspaces start in
    the canonical form so setup is idempotent on re-runs.

    Includes an ``agent.execute.*`` allow at the top so the
    engine's pre-flight authz check passes — without this the
    default-deny tail would block every exchange.
    """
    return [
        # Pre-flight gate — the engine requires agent.execute.<id> to
        # run any exchange. Default to allow; users tighten by adding
        # a more specific deny before this rule.
        {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
        # The four Phase 2.0 builtins — owned by the kolsch bundle.
        {"what": "kolsch/read_file", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/list_files", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/write_file", "pattern": "*", "effect": "prompt"},
        {"what": "kolsch/bash", "pattern": "*", "effect": "prompt"},
        # Default-deny tail (protected — TOOL-R10).
        {"what": "*", "pattern": "*", "effect": "deny"},
    ]


# ---------------------------------------------------------------------------
# Authz-rules migration — bug-chat-permissions-and-console-leak Step C
# ---------------------------------------------------------------------------
#
# Workspaces created before commit deaf95f (2026-05-30) carry the
# Phase 1 allow-all rule set, not the Phase 2.0 §3.1 defaults. The
# migration here is per E-BUG-CHAT-001 option (a): on `kolsch setup`
# re-run, detect the sentinel, prompt the user, and rewrite
# ``authorization.config.rules`` in place — preserving any
# user-added custom rules in their original relative position.
# Non-interactive re-runs default to "skip" so silent mutation of
# the user's config can't happen via CI.


_ALLOW_ALL_SENTINEL: dict[str, str] = {"who": ".*", "what": ".*", "effect": "allow"}


def _has_pre_phase_2_authz_shape(rules: list[object]) -> bool:
    """True when the rule list looks Phase-1-origin: the allow-all
    sentinel is present AND there is no default-deny tail.

    Phase 1 ``kolsch setup`` wrote only the sentinel; users may
    have appended their own custom rules after the fact, so the
    detection accepts ``[sentinel, ...customs]`` rather than
    ``[sentinel]``-only. The absence of the
    ``{who:.*, what:.*, effect:deny}`` tail is the signal: Phase
    2.0's §3.1 defaults always end with that tail, so a list with
    the sentinel and no deny tail can only have come from Phase 1.
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
    """Return the §3.1 defaults with any non-sentinel rules from
    ``existing`` preserved BEFORE the default-deny tail.

    Customs (rules that are not the allow-all sentinel) survive in
    their original relative position. The tail ``rl_default_deny``
    stays last so first-match-wins semantics keep the deny as the
    floor."""
    defaults = _workspace_only_default_rules()
    customs = [r for r in existing if not _is_sentinel_rule(r)]
    # The last default is the deny tail; keep it last.
    return defaults[:-1] + customs + defaults[-1:]


def _backup_platform_yaml(yaml_path: Path) -> Path:
    """Copy ``yaml_path`` to
    ``.platform/platform.yaml.bak.<UTC-timestamp>``.

    UTC + ISO-ish basic format keeps backups sortable and avoids
    timezone ambiguity. Returns the backup path so the caller can
    surface it to the user."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = yaml_path.parent / ".platform"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"platform.yaml.bak.{ts}"
    backup.write_bytes(yaml_path.read_bytes())
    return backup


_MIGRATION_PROMPT_OPTIONS: list[str] = [
    "[m] migrate to §3.1 defaults",
    "[k] keep mine — no changes",
    "[d] diff first",
    "[s] skip — decide later",
]
_MIGRATION_PROMPT_DEFAULT = "s"


def _maybe_migrate_authz_rules(
    io: _SetupIO, yaml_path: Path, *, non_interactive: bool
) -> None:
    """Detect the Phase 1 allow-all sentinel and prompt for migration.

    No-op when the workspace already has the §3.1 defaults (or any
    other non-sentinel rule set). Non-interactive runs default to
    "skip" so CI re-runs never mutate the user's config silently
    (per E-BUG-CHAT-001's resolution: migration is opt-in)."""
    payload = _read_yaml(yaml_path)
    authz = payload.get("authorization") or {}
    if authz.get("variant") != "rule_based":
        return
    rules = (authz.get("config") or {}).get("rules") or []
    if not _has_pre_phase_2_authz_shape(rules):
        return

    if non_interactive:
        io.out.write(
            "  ⚠ this workspace has the old allow-all rule set "
            "(pre-Phase 2.0). Re-run setup interactively to migrate "
            "to the §3.1 defaults, or edit platform.yaml by hand.\n"
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
        new_rules = _migrate_rules_preserving_customs(rules)
        payload["authorization"]["config"]["rules"] = new_rules
        yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        io.out.write("  ✓ migrated authorization rules → §3.1 defaults\n")
        io.out.write(f"  ✓ backup written to {backup.relative_to(yaml_path.parent)}\n")
    elif choice == "k":
        io.out.write("  → keeping current rules; no changes written\n")
    else:  # "s" or any unrecognised default — defer
        io.out.write("  → deferred; will prompt again on the next setup run\n")


async def _maybe_migrate_authz_rules_to_qol_schema(io: _SetupIO) -> None:
    """Migrate ``platform.yaml`` rules to the QoL three-field schema.

    qol-permission-system-human-readable.md Step 4 (carryover
    E-QOL-PERMSYS-002). On `kolsch setup` re-run:

    1. Build the catalog so the migrator can resolve
       ``tool.<name>`` actions to ``<bundle>/<name>`` qualified
       refs.
    2. Run :func:`cli._migrations.authz_rules.migrate_authz_rules`
       over the existing rule list.
    3. For each rule that couldn't be migrated, emit a
       :class:`errors.ConfigDeprecatedRegexRule` Recoverable
       warning via stdout (the chat/server runtime swap stdout
       for the Recoverable-event surface; setup is the CLI
       caller and writes directly).
    4. If anything migrated, atomically rewrite the YAML.

    Silent no-op when:

    - The workspace's rule list is already entirely new-shape
      (no ``who`` field on any entry).
    - The workspace is non-rule-based authz variant.

    The function never raises — a per-rule failure produces a
    warning line but doesn't abort setup. Migration is opt-out
    via the existing "skip" path on the prior
    (E-BUG-CHAT-001) prompt; this new path runs unconditionally
    AFTER that prompt has resolved.
    """
    from assets import AssetManagement  # noqa: PLC0415
    from cli._migrations.authz_rules import migrate_authz_rules  # noqa: PLC0415
    from errors import ConfigDeprecatedRegexRule  # noqa: PLC0415

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
    # Skip when nothing has a `who` field — already migrated.
    if not any(isinstance(r, dict) and "who" in r for r in rules):
        return

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
        1
        for old, new in zip(rules, result.rules, strict=False)
        if old != new
    )
    for entry in result.deprecated:
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
    payload["authorization"]["config"]["rules"] = result.rules
    yaml_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    io.out.write(
        f"  ✓ migrated {converted_count} authz rule(s) → new schema "
        "(what / pattern / effect)\n",
    )
    io.out.write(
        f"  ✓ backup written to {backup.relative_to(yaml_path.parent)}\n",
    )


def _maybe_install_missing_kolsch_skills(io: _SetupIO) -> None:
    """Drop in canonical kolsch skills missing from an existing workspace.

    Phase 2.4 Step 9 — additive drift handler for the kolsch bundle's
    ``skills/`` tree. Compares the packaged template's skill set to
    what's on disk; copies the difference. Never overwrites — if the
    user edited a canonical skill, their edits stay. If they deleted
    one, the next setup re-run adds it back (intentional: skills are
    additive content, not config the user "owns").

    Silent no-op when:
    - The user has no kolsch bundle (they removed it; respect that).
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
            f"  ✓ installed {len(added)} canonical skill(s): "
            f"{', '.join(added)}\n",
        )


async def _maybe_warn_invalid_skills(io: _SetupIO) -> None:
    """Skill-validation advisory hook — runs on setup re-runs.

    Builds a fresh :class:`AssetManagement`, walks every catalogued
    skill, and prints a one-line warning per skill that fails
    :func:`cli.skills.validate_skill_entry`. Never blocks setup —
    Step 6's explicit design call (a user mid-editing a SKILL.md
    should not be locked out of completing setup).

    Silent when the workspace has no skills catalogued — no noise
    on fresh re-runs.
    """
    from assets import AssetManagement  # noqa: PLC0415
    from cli.skills import validate_skill_entry  # noqa: PLC0415

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
        errs = validate_skill_entry(entry)
        if errs:
            failures.append((entry.ref, errs))

    if not failures:
        io.out.write(f"  ✓ validated {len(skills)} skill(s)\n")
        return

    io.out.write(
        f"  ⚠ skill validation: {len(failures)} of {len(skills)} "
        "skill(s) have warnings:\n",
    )
    for ref, errs in failures:
        # Show error codes only at the setup surface — keeps the
        # banner tight. Users get the full human-readable view via
        # `kolsch skills validate <ref>`.
        io.out.write(f"      {ref}: {', '.join(errs)}\n")
    io.out.write(
        "    (Run `kolsch skills validate` for details; "
        "warnings do not block setup.)\n",
    )


def _print_migration_diff(io: _SetupIO, current_rules: list[dict[str, Any]]) -> None:
    """Show a unified diff of the rules block (current → migrated)."""
    before = yaml.safe_dump({"rules": current_rules}, sort_keys=False).splitlines()
    after = yaml.safe_dump(
        {"rules": _migrate_rules_preserving_customs(current_rules)},
        sort_keys=False,
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
    """Build a `_CommandIO` for `cmd_connect`. When `api_key` is not
    `None` (the non-interactive env-driven path), wrap a prompter that
    returns the env-provided key for any ``api_key`` field; otherwise
    fall back to the production StdinPrompter.
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
        """Returns `api_key` from the env-provided value; everything
        else uses the field's default (good enough for OpenRouter,
        whose only required field is `api_key`; OpenCode also asks
        for `base_url` — defaults are populated by the connector's
        prompt spec)."""

        def __init__(self, key: str, fallback: StdinPrompter) -> None:
            self._key = key
            self._fallback = fallback

        def ask(self, field: PromptField) -> str:
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
    from cli.main import _cmd_run  # noqa: PLC0415

    async def _run_handler(
        args: argparse.Namespace,
    ) -> tuple[int, str | None]:
        """Adapter — wraps the existing ``_cmd_run`` so Step E can read
        the failure code from a Fatal ``AgentExecutionError`` without
        re-implementing agent execution (Phase 1.2 spec rule). On
        success returns ``(0, None)``; on Fatal returns
        ``(exit_code, error_code)`` where ``error_code`` is the
        canonical Error Model code from
        ``failed_event.data["error"]["type"]``.
        """
        from engine.context import AgentExecutionError  # noqa: PLC0415

        try:
            rc = await _cmd_run(args)
        except AgentExecutionError as exc:
            error_code = None
            if exc.failed_event is not None and isinstance(exc.failed_event.data, dict):
                err = exc.failed_event.data.get("error")
                if isinstance(err, dict):
                    code = err.get("type")
                    if isinstance(code, str):
                        error_code = code
            return (2, error_code)
        return (rc, None)

    return _SetupIO(
        root=root,
        out=sys.stdout,
        err=sys.stderr,
        prompter=StdinSetupPrompter(sys.stdout),
        connect_io_factory=_default_connect_io_factory,
        run_handler=_run_handler,
    )

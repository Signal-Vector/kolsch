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
import os
import shutil
import sys
from dataclasses import dataclass
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
    # Run-handler — the same shape as `cli.main._cmd_run`. Used by
    # step E to verify the install with a real exchange.
    run_handler: Callable[[argparse.Namespace], Awaitable[int]]


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


async def run_setup(args: argparse.Namespace, io: _SetupIO) -> int:
    """Walk the six steps. Returns exit code (0 ok, 4 partial)."""
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

    proceeded_any = False
    skipped_any = False
    partial = False

    for step_name, step_fn in (
        ("A workspace location", _step_a_workspace_location),
        ("B workspace skeleton", _step_b_skeleton),
        ("C LLM provider", _step_c_provider),
        ("D starter agent", _step_d_starter_agent),
        ("E test run", _step_e_test_run),
    ):
        try:
            result = await step_fn(io, non_interactive=non_interactive)
        except KeyboardInterrupt:
            io.err.write(f"\nsetup aborted at step {step_name}\n")
            return _EXIT_PARTIAL
        if result == _PROCEEDED:
            proceeded_any = True
        elif result == _SKIPPED:
            skipped_any = True
        elif result == "partial":
            partial = True

    _step_f_banner(io)

    if partial:
        return _EXIT_PARTIAL
    # If literally every step was skipped (re-run on a fully-set-up
    # workspace), we still report success — the workspace IS ready.
    _ = proceeded_any, skipped_any
    return _EXIT_OK


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
    _ = non_interactive
    yaml_path = io.root / "platform.yaml"
    env_path = io.root / ".env"

    already = yaml_path.is_file()
    if already:
        io.out.write(f"✓ workspace already initialized at {io.root}\n")
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

# Placeholder string in the packaged agents/helper.yaml replaced with
# the resolved model line at materialization time (Step D).
_MODEL_PLACEHOLDER = "{{MODEL_LINE}}"


async def _step_d_starter_agent(io: _SetupIO, *, non_interactive: bool) -> str:
    """Materialize the kolsch bundle from the packaged template.

    Phase 2.0 (TOOL-R10, coordinator resolution E-2.0-001 Option A):
    instead of rendering a synthetic agent.yaml inline, this step
    copies the packaged template at
    ``src/cli/_starter_bundle/kolsch/`` (bundle.yaml + agents/
    helper.yaml + the four tool .py files) into
    ``<workspace>/bundles/kolsch/``, substituting the resolved model
    line into the helper agent's ``model:`` field. Each tool .py file
    is the source of truth for what that tool does — users can edit,
    copy, or swap them per TOOL-R10's intent.
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

    _copy_kolsch_bundle(io.root, model=model)
    io.out.write(f"  ✓ materialized bundle at {bundle_dir.relative_to(io.root)}/\n")
    io.out.write(f"  ✓ wrote starter agent at {agent_path.relative_to(io.root)}\n")
    _announce_picked_model(io, model)
    return _PROCEEDED


def _copy_kolsch_bundle(root: Path, *, model: str | dict[str, str]) -> None:
    """Copy the packaged kolsch bundle template into the workspace.

    Substitutes the model placeholder in agents/helper.yaml with the
    resolved model line. The four tools/*.py files are copied
    verbatim — they're standalone Python that the asset catalog
    discovers + the Tools subsystem registers per the standard
    bundle-tool path (per the Phase 2.0 coordinator resolution E-2.0-001
    Option A).
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

    # Substitute the model placeholder in the agent file. The template
    # ships with the literal `{{MODEL_LINE}}` token; replace it with
    # the resolved model line.
    agent_path = target / "agents" / f"{_STARTER_AGENT}.yaml"
    rendered = agent_path.read_text(encoding="utf-8").replace(
        _MODEL_PLACEHOLDER, _render_model_line(model)
    )
    agent_path.write_text(rendered, encoding="utf-8")


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


def _pick_starter_model(io: _SetupIO) -> str | dict[str, str]:
    """Choose the model field value for the starter agent.

    Priority:

    1. Connected providers whose ``serves_as_metadata_source`` flag is
       NOT set — first provider's first model with a non-empty id.
    2. Connected providers WITH ``serves_as_metadata_source: true``
       (typically only OpenRouter) — same first-model rule, as a
       fallback. A metadata-source provider can still route real
       calls; we just prefer not to assume that intent.
    3. ``provider: fake`` if no connected provider yielded a model.

    When the picked model matches a deployment alias (lookup against
    `model_aliases`), the alias string is returned for readability —
    matches the existing `opus → openrouter/...` ergonomic.
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

    # Insertion-order traversal: dict iteration is connect-order in
    # platform.yaml as written; sort metadata-sources to the end so
    # the priority pool drains first.
    ordered: list[tuple[str, dict[str, Any]]] = sorted(
        providers.items(),
        key=lambda kv: bool(kv[1].get("serves_as_metadata_source")),
    )
    for name, entry in ordered:
        model_id = _first_model_id(entry)
        if model_id is None:
            continue
        # Alias override when one exists for the picked pair.
        for alias_name, target in aliases.items():
            if target == f"{name}/{model_id}":
                return alias_name
        return {"provider": name, "model": model_id}
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


def _render_model_line(model: str | dict[str, str]) -> str:
    """Render the model field value as it lands in agents/helper.yaml.

    The model field can be either a compact-string alias or the
    canonical ``{provider, model}`` dict; both shapes are valid YAML
    inline.
    """
    if isinstance(model, str):
        return model
    return "{ provider: " + model["provider"] + ", model: " + model["model"] + " }"


# ---------------------------------------------------------------------------
# Step E — test run
# ---------------------------------------------------------------------------


async def _step_e_test_run(io: _SetupIO, *, non_interactive: bool) -> str:
    agent_ref = f"{_STARTER_BUNDLE}/agents/{_STARTER_AGENT}"
    agent_path = (
        io.root / "bundles" / _STARTER_BUNDLE / "agents" / f"{_STARTER_AGENT}.yaml"
    )
    if not agent_path.exists():
        io.out.write("  (no starter agent to test; skipping test run)\n")
        return _SKIPPED
    if not non_interactive:
        run_it = io.prompter.confirm(
            "Run the helper agent once to confirm everything works?",
            default=True,
        )
        if not run_it:
            io.out.write("  → skipped test run\n")
            return _SKIPPED
    io.out.write(f"Running {agent_ref}...\n")
    rc = await io.run_handler(
        argparse.Namespace(
            root=io.root,
            agent_ref=agent_ref,
            input=["content=hello"],
            stream=False,
            session=None,
        )
    )
    if rc != 0:
        io.err.write(
            f"  → test run failed (rc={rc}); check provider credentials "
            "or re-run `kolsch connect`\n"
        )
        return "partial"
    io.out.write("  ✓ test run completed successfully\n")
    return _PROCEEDED


# ---------------------------------------------------------------------------
# Step F — banner
# ---------------------------------------------------------------------------


def _step_f_banner(io: _SetupIO) -> None:
    agent_ref = f"{_STARTER_BUNDLE}/agents/{_STARTER_AGENT}"
    io.out.write("\n")
    io.out.write("Kolsch is ready.\n\n")
    io.out.write("Try:\n")
    io.out.write(f'  kolsch run {agent_ref} --input content="<your prompt>"\n')
    io.out.write("  kolsch sessions list\n")
    io.out.write("  kolsch --help\n\n")
    io.out.write(f"Workspace: {io.root}\n")
    io.out.write("Docs:      docs/tutorials/first-agent.md\n")


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

    The ``who``/``what`` convention follows Phase 1's authz spec
    (``who`` = principal regex, ``what`` = action regex). The patch
    example inverts these; the implementation honours the existing
    convention. See Step 22 reviewer notes + the tests in
    ``tests/component/authz/test_phase2_authz.py``.

    Includes an ``agent.execute.*`` allow at the top so the engine's
    pre-flight authz check (``engine.engine:391``) passes — without
    this the default-deny tail would block every exchange.
    """
    return [
        # Pre-flight gate — the engine requires agent.execute.<id> to
        # run any exchange. Default to allow; users tighten by adding
        # a more specific deny before this rule.
        {"who": ".*", "what": r"agent\.execute\..*", "effect": "allow"},
        # The four Phase 2.0 builtins.
        {"who": ".*", "what": r"tool\.read_file", "effect": "allow"},
        {"who": ".*", "what": r"tool\.list_files", "effect": "allow"},
        {"who": ".*", "what": r"tool\.write_file", "effect": "prompt"},
        {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
        # Default-deny tail (protected — TOOL-R10).
        {"who": ".*", "what": ".*", "effect": "deny"},
    ]


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

    return _SetupIO(
        root=root,
        out=sys.stdout,
        err=sys.stderr,
        prompter=StdinSetupPrompter(sys.stdout),
        connect_io_factory=_default_connect_io_factory,
        run_handler=_cmd_run,
    )

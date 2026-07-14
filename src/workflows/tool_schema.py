"""Dynamic per-workflow tool schema builder (bug-028).

Emits one virtual ``ToolDefinition`` per catalogued workflow so the
model sees a TYPED args slot for each required input
(``url: str``) instead of the opaque
``dict[str, object]`` on the generic ``run_workflow`` tool.
Session ``ses_6962c187-…`` proved that even with bug-025 + bug-027's
rich prose + retry template, Kimi K2.6 cannot synthesise
``{"url": "..."}`` for a schema slot advertised as "any dict, keys
unknown." The fix is structural: give the model a TYPED schema slot
per known workflow.

**Two entry points:**

- :func:`build_workflow_args_model` — dynamic Pydantic model
  builder. Uses :func:`workflows.inputs_scan.scan_inputs` (bug-027) to
  determine required vs optional inputs; probes the YAML default
  value for bool/int typing on optional fields, otherwise ``str``.
- :func:`build_virtual_tool_definitions` — walks the workflows
  service's ``list_workflow_refs()`` seam and returns one
  ``ToolDefinition`` per discoverable ref. Each virtual tool's
  ``func`` closes over the fixed ref and delegates straight to
  ``services.workflows.start(ref=<fixed>, inputs=<from typed args>,
  card=<from ctx>)`` — same code path as the generic
  ``run_workflow``, so cards, anchors, and observers behave
  identically.

The generic ``run_workflow`` tool stays registered — the virtuals
are an ADDITION, not a replacement. Bug-027's ``@tool description``
tells the model to prefer the virtual when it exists in the tool
list.

**Naming.** ``run_workflow_<bundle>_<workflow>`` with ``/`` → ``_``
and any other non-``[a-z0-9_]`` char collapsed to ``_``. If the
resulting name exceeds 64 chars (a common provider tool-name limit),
we truncate to 55 chars and append ``_<8-char blake2b hex>`` for
determinism — hash first, so re-scanning the same ref always yields
the same virtual name.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, create_model

from tools.decorator import ToolDefinition
from workflows.inputs_scan import scan_inputs

if TYPE_CHECKING:
    from tools.decorator import ToolContext


__all__ = [
    "build_virtual_tool_definitions",
    "build_workflow_args_model",
    "virtual_tool_name",
]


# ---------------------------------------------------------------------------
# Name generation.
# ---------------------------------------------------------------------------


# Tool names must match ``^[a-z][a-z0-9_]*$`` per
# ``src/tools/decorator.py::_NAME_PATTERN``. The virtual name is
# always ``run_workflow_`` + a mangled ref; the ``run_workflow_``
# prefix guarantees the leading-``[a-z]`` requirement.
_NAME_CHAR_RE = re.compile(r"[^a-z0-9_]+")
_MAX_TOOL_NAME_LEN = 64
_HASH_SUFFIX_LEN = 8  # 8 hex chars from blake2b — 32 bits of collision-resistance.


def virtual_tool_name(ref: str) -> str:
    """Return the identifier-safe virtual tool name for ``ref``.

    ``andre/workflows/job_description_review`` →
    ``run_workflow_andre_workflows_job_description_review`` — the
    full ref is mangled to preserve the ``bundle`` segment (needed
    when two bundles ship a workflow with the same terminal name).
    Any non-``[a-z0-9_]`` character is collapsed to ``_``; the result
    is lower-cased. If the result exceeds
    ``_MAX_TOOL_NAME_LEN`` (64), we truncate to
    ``_MAX_TOOL_NAME_LEN - 1 - _HASH_SUFFIX_LEN`` chars and append
    ``_<hash>`` for deterministic collision-avoidance under repeat
    scans.

    Args:
        ref: The workflow's 3-seg catalog ref
            (``bundle/workflows/name``).

    Returns:
        The identifier-safe name matching
        ``^[a-z][a-z0-9_]*$``, at most 64 chars.
    """
    mangled = _NAME_CHAR_RE.sub("_", ref.lower()).strip("_") or "wf"
    full = f"run_workflow_{mangled}"
    if len(full) <= _MAX_TOOL_NAME_LEN:
        return full
    # Truncation branch: 55 chars of the mangled prefix + `_` + 8 hex.
    truncate_len = _MAX_TOOL_NAME_LEN - 1 - _HASH_SUFFIX_LEN
    digest = hashlib.blake2b(ref.encode("utf-8"), digest_size=4).hexdigest()
    return f"{full[:truncate_len]}_{digest}"


# ---------------------------------------------------------------------------
# YAML input default probing.
# ---------------------------------------------------------------------------


def _probe_default_value(yaml_source: str, name: str) -> tuple[type, Any] | None:
    """Return ``(type, default)`` for ``{{ inputs.<name> | default(...) }}``.

    Best-effort: scans the raw YAML text for the first
    ``inputs.<name> | default(<literal>)`` occurrence and probes the
    literal. Returns:

    - ``(bool, True)`` / ``(bool, False)`` when the literal parses
      as ``true``/``false`` (case-insensitive).
    - ``(int, <n>)`` when the literal parses as an integer.
    - ``(str, "<val>")`` when the literal is a quoted string.
    - ``None`` when the input has no ``| default(...)`` guard or the
      literal fails every probe.

    Callers fall back to ``str`` (with ``""``) on ``None``.
    """
    # Look for `inputs.<name>` followed by a `default(...)` filter.
    # Requires a small window scan; regex works because the YAML
    # tokenizer already rejected malformed defaults during scan.
    # Word boundaries around the name prevent
    # `inputs.limit_max` matching `inputs.limit`.
    pattern = re.compile(
        r"inputs\." + re.escape(name)
        + r"\b[^}]*?\|\s*default\s*\(\s*([^)]+?)\s*\)",
    )
    match = pattern.search(yaml_source)
    if match is None:
        return None
    literal = match.group(1).strip()
    # Bool probe first — Jinja renders ``true`` / ``false`` /
    # ``True`` / ``False`` identically.
    lowered = literal.lower()
    if lowered == "true":
        return (bool, True)
    if lowered == "false":
        return (bool, False)
    # Int probe — bare integer with optional leading sign.
    if re.fullmatch(r"[+-]?\d+", literal):
        with contextlib.suppress(ValueError):
            return (int, int(literal))
    # String probe — literal wrapped in matching single or double
    # quotes.
    if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in ("'", '"'):
        return (str, literal[1:-1])
    return None


def _lineno_of(yaml_source: str, name: str) -> int | None:
    """Return the 1-based line where ``inputs.<name>`` first appears.

    Best-effort — the search is a plain substring for
    ``inputs.<name>`` with a word boundary; if not found, returns
    ``None`` and the field description omits the line reference. Used
    only for user-facing description prose, so a miss degrades to
    "referenced in the workflow YAML" rather than raising.
    """
    pattern = re.compile(r"inputs\." + re.escape(name) + r"\b")
    for idx, line in enumerate(yaml_source.splitlines(), start=1):
        if pattern.search(line):
            return idx
    return None


# ---------------------------------------------------------------------------
# Args model builder.
# ---------------------------------------------------------------------------


def build_workflow_args_model(
    ref: str,
    yaml_source: str,
) -> type[BaseModel]:
    """Build a dynamic Pydantic model describing the workflow's inputs.

    Uses :func:`scan_inputs` to partition ``inputs.X`` references
    into required (unguarded) and optional (``| default(...)``-
    guarded) sets. Required fields default to ``str`` with no
    default (the Pydantic-created ``FieldInfo`` sets
    ``is_required()=True``). Optional fields inherit the YAML
    default's type when probeable (bool → int → str precedence),
    falling back to ``str = ""`` otherwise.

    Each field carries a description referencing the ``{{ inputs.X }}``
    site in the workflow YAML (with line number when known) so the
    tool-list JSON schema still explains WHY the key exists even for
    models that ignore top-level tool descriptions.

    Args:
        ref: The workflow's catalog ref — used to shape the model's
            class name (helps model tracebacks in tests).
        yaml_source: The workflow YAML as a raw string.

    Returns:
        A Pydantic ``BaseModel`` subclass whose fields correspond
        one-to-one to the workflow's ``inputs.X`` references.
    """
    scan = scan_inputs(yaml_source)

    # Class name — safe suffix derived from the ref. Not surfaced to
    # the model, but shows up in Pydantic's ``ValidationError``
    # locations if a virtual invocation ever fails validation.
    class_suffix = _NAME_CHAR_RE.sub("_", ref.lower()).strip("_") or "workflow"
    model_name = f"RunWorkflowArgs_{class_suffix}"

    fields: dict[str, Any] = {}

    for name in scan.required:
        line = _lineno_of(yaml_source, name)
        desc = _describe_required(name, line)
        # `(str, Field(...))` — Field with no `default` → required.
        # mypy: create_model's field spec is Any; the ignore is
        # narrow to the call.
        fields[name] = (
            str,
            Field(description=desc),
        )

    for name in scan.optional:
        probe = _probe_default_value(yaml_source, name)
        line = _lineno_of(yaml_source, name)
        desc = _describe_optional(name, line, probe)
        if probe is None:
            fields[name] = (str, Field(default="", description=desc))
        else:
            typ, default = probe
            fields[name] = (typ, Field(default=default, description=desc))

    # create_model returns type[BaseModel]; mypy's `Any`-typed value
    # spec on the ``**fields`` splat means the return is inferred as
    # ``type[BaseModel]`` cleanly today. Kept explicit for
    # documentation: this is the ONE narrow deviation the spec's AC
    # #9 pre-approved.
    return create_model(
        model_name,
        __base__=BaseModel,
        **fields,
    )


def _describe_required(name: str, line: int | None) -> str:
    """Human-readable description for a required per-workflow field."""
    if line is None:
        return (
            f"Value for {{{{ inputs.{name} }}}} in the workflow YAML. "
            f"Sourced from the user's request."
        )
    return (
        f"Value for {{{{ inputs.{name} }}}} referenced at "
        f"workflow YAML line {line}. Sourced from the user's request."
    )


def _describe_optional(
    name: str,
    line: int | None,
    probe: tuple[type, Any] | None,
) -> str:
    """Human-readable description for an optional per-workflow field."""
    if probe is None:
        default_repr = "(no probeable default)"
    else:
        default_repr = repr(probe[1])
    if line is None:
        return (
            f"Optional value for {{{{ inputs.{name} | default(...) }}}}. "
            f"YAML default: {default_repr}."
        )
    return (
        f"Optional value for {{{{ inputs.{name} | default(...) }}}} "
        f"referenced at workflow YAML line {line}. YAML default: "
        f"{default_repr}."
    )


# ---------------------------------------------------------------------------
# Virtual ToolDefinition assembly.
# ---------------------------------------------------------------------------


def build_virtual_tool_definitions(
    *,
    workflows_impl: Any,  # noqa: ANN401 — duck-typed
    home_bundle: str | None = None,
    authz_action: str | None = None,
) -> list[ToolDefinition]:
    """Return one virtual ``ToolDefinition`` per catalogued workflow.

    Discovery uses the same duck-typed seams the existing tools rely
    on:

    - ``workflows_impl.list_workflow_refs()`` — enumerates every
      workflow ref visible to the current agent.
    - ``workflows_impl.resolve_workflow_yaml(ref)`` — returns the
      workflow YAML source so :func:`build_workflow_args_model` can
      derive typed fields.

    Impls without EITHER seam short-circuit to an empty list (headless
    fixtures, minimal mocks, pre-bug-027 test doubles) — the caller
    (``ToolsSubsystem.register_for_agent``) treats an empty return as
    "no virtuals to register, just the generic ``run_workflow``."

    Each virtual tool's ``func`` closes over the fixed ref and calls
    the same ``services.workflows.start(...)`` path the generic tool
    uses. The card is opened via ``services.cards.open(...)`` first
    (to preserve card ownership transfer semantics — see
    ``bundles/kolsch/tools/run_workflow.py``) and the return payload
    is byte-identical to the generic tool's:
    ``{run_id, card_id, _chat_anchor}``.

    Args:
        workflows_impl: The ``ChatServiceRegistry.workflows`` impl
            (duck-typed).
        home_bundle: Bundle slug stamped on each virtual's
            ``ToolDefinition.home_bundle``. Mirrors the generic
            ``run_workflow``'s home so tracebacks / metadata surface
            the same bundle.
        authz_action: Explicit authz action string every virtual
            reuses. Defaults to the generic tool's canonical action
            ``<home_bundle>/tools/run_workflow`` (or ``tool.run_workflow``
            when ``home_bundle is None``) so a single
            ``kolsch/tools/run_workflow`` allow rule covers every
            virtual — the alternative (per-virtual actions) would
            need pattern-matching authz rules and would break the
            "declared: run_workflow" → all virtuals gated on the
            same permission invariant. Callers can override to a
            different action string for testing.

    Returns:
        A list of ``ToolDefinition``s ready for insertion into
        ``ToolRegistry``. Empty when the impl doesn't expose the
        discovery seams or when no workflows are catalogued.
    """
    if authz_action is None:
        # Match the generic tool's action_name derivation so the
        # same authz rule (`kolsch/tools/run_workflow`) covers the
        # virtuals. The generic tool's ``action_name`` property
        # picks the same branch when ``action`` isn't overridden.
        if home_bundle is None:
            authz_action = "tool.run_workflow"
        else:
            authz_action = f"{home_bundle}/tools/run_workflow"
    list_refs = getattr(workflows_impl, "list_workflow_refs", None)
    resolver = getattr(workflows_impl, "resolve_workflow_yaml", None)
    if list_refs is None or resolver is None:
        return []
    try:
        refs = list_refs()
    except Exception:  # noqa: BLE001 — best-effort probe
        return []
    if not isinstance(refs, list):
        return []

    defs: list[ToolDefinition] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            continue
        try:
            yaml_source = resolver(ref)
        except Exception:  # noqa: BLE001 — best-effort probe
            continue
        if not isinstance(yaml_source, str):
            continue
        try:
            args_model = build_workflow_args_model(ref, yaml_source)
        except Exception:  # noqa: BLE001 — best-effort probe
            # Skipping is safer than raising during tool-list
            # assembly: a broken virtual would take down every tool
            # registration for this agent.
            continue
        name = virtual_tool_name(ref)
        description = _describe_virtual(ref, yaml_source)
        func = _make_virtual_invoker(ref, args_model)
        defs.append(
            ToolDefinition(
                name=name,
                description=description,
                args_schema=args_model,
                func=func,
                summary=f"Start the {ref} workflow.",
                # Virtuals inherit the generic tool's service needs
                # so the dispatcher populates cards + workflows on
                # the per-call ``ToolContext``.
                requires=["cards", "workflows"],
                home_bundle=home_bundle,
                # Route authz through the generic tool's action —
                # every virtual is gated on the SAME permission the
                # generic tool is (see ``authz_action`` docstring).
                action=authz_action,
                # bug-029: virtual workflow tools have arbitrary
                # multi-field args models — the bug-025 priority
                # walk would short-circuit on ``url`` (or another
                # priority attr) and hide the rest of the call
                # shape from the user. Opt into the JSON dump path
                # so the approval widget's ``Args`` line shows the
                # full call shape (``{"url": "…", "limit": 5}``).
                target_shape="json",
            ),
        )
    return defs


def _describe_virtual(ref: str, yaml_source: str) -> str:
    """One-paragraph description for a virtual per-workflow tool.

    Includes the workflow's declared ``description:`` (when present)
    plus a directive telling the model that ``inputs`` are already
    typed on this tool — no ``workflow_inspect`` call needed. The
    generic ``run_workflow`` is called out as a fallback for
    workflows discovered dynamically.
    """
    yaml_desc = _extract_yaml_description(yaml_source)
    lines = [
        f"Start the `{ref}` workflow and mount a live workflow card. "
        f"This is a per-workflow typed variant of `run_workflow` — "
        f"each required input is a typed argument on THIS tool, so "
        f"you don't need `workflow_inspect` or a JSON `inputs` dict. "
        f"Returns `{{run_id, card_id, _chat_anchor}}` — same shape as "
        f"`run_workflow`.",
        # bug-030 Fix 4: anti-hallucination sentence propagated to
        # every per-workflow virtual so Kimi (and every other model)
        # sees "no dropdown; do not tell the user to expand" verbatim
        # regardless of whether the generic ``run_workflow`` tool is
        # in the tool list this turn.
        (
            "The workflow card renders progress INLINE in the "
            "transcript — header, per-node rows, and status footer "
            "are all visible on mount and update live. There is no "
            "dropdown, expand-to-view, or user gesture required to "
            "see progress. Do NOT instruct the user to expand, open, "
            "click, or drop down anything on the card. To draw the "
            "user's eye to it, emit the `_chat_anchor` markdown "
            "`[label](kolsch-anchor://workflow_card/<run_id>)`."
        ),
    ]
    if yaml_desc:
        lines.append(f"Workflow description: {yaml_desc}")
    return "\n\n".join(lines)


def _extract_yaml_description(yaml_source: str) -> str:
    """Best-effort ``description:`` field extraction (dupes workflow_inspect)."""
    from kolsch._yamlio import load  # noqa: PLC0415

    try:
        payload = load(yaml_source)
    except Exception:  # noqa: BLE001 — best-effort
        return ""
    if not isinstance(payload, dict):
        return ""
    desc = payload.get("description")
    if isinstance(desc, str):
        return desc
    return ""


_VIRTUAL_POLL_INTERVAL_SECONDS = 0.5
"""bug-031: same 500ms poll interval as the generic ``run_workflow``.

Kept as a module-level constant so a future perf tune can hit both
tools in one place.
"""

_VIRTUAL_DEFAULT_TIMEOUT_SECONDS = 300
"""Fallback timeout when the workflows service doesn't expose one.

Preferred source is
``services.workflows.default_timeout_seconds`` (driven by
``platform.yaml:workflows.default_timeout_seconds``); this constant
is only used when the service handle lacks the attribute (older
mocks / headless test paths).
"""

_VIRTUAL_TERMINAL_PHASES = frozenset({"success", "error", "cancelled"})
"""bug-031: services phase vocabulary the poll loop treats as done."""


def _make_virtual_invoker(
    ref: str,
    args_model: type[BaseModel],
) -> Any:  # noqa: ANN401 — returns Callable[..., Awaitable[dict]]
    """Return an async invoker that starts ``ref`` with typed args.

    The returned coroutine mirrors ``bundles/kolsch/tools/run_workflow.py``:
    it requires the ``cards`` + ``workflows`` services on ``ctx``,
    opens a workflow-kind card via ``cards.open``, calls
    ``workflows.start(ref, inputs, card=card)`` with the typed args
    flattened into a plain dict, and returns the standard
    ``{run_id, card_id, phase, outputs, error_message, _chat_anchor}``
    payload the assistant prose references via
    ``kolsch-anchor://workflow_card/<run_id>``.

    bug-031: this virtual variant BLOCKS until the workflow reaches a
    terminal phase (or the 300s timeout elapses) — same shape as the
    generic tool. Without this, the virtual tool's tool_result
    payload would remain the pre-bug-031 fire-and-forget shape and
    the model would be silent about the outcome, defeating the whole
    point of bug-031.

    Args:
        ref: The workflow's catalog ref — fixed at emit time,
            closed over by the invoker.
        args_model: The dynamic Pydantic model whose fields are the
            workflow's typed inputs (unused at invocation — dispatch
            validated ``args`` against it already).

    Returns:
        An async function with the ``(args, ctx) -> dict[str, object]``
        signature the tool dispatcher calls.
    """
    _ = args_model  # captured for closure closure documentation only

    async def virtual_invoker(
        args: BaseModel,
        ctx: ToolContext,
    ) -> dict[str, object]:
        import asyncio as _asyncio  # noqa: PLC0415

        from tools.services import require_services  # noqa: PLC0415

        services = require_services(ctx, ["cards", "workflows"])
        assert services.cards is not None  # narrowed by require_services
        assert services.workflows is not None  # narrowed by require_services

        # ``model_dump()`` gives us the plain dict shape ``workflows.
        # start`` expects. Extra Pydantic serialisation flags are not
        # needed: every field is a primitive (str / int / bool) from
        # the args-model builder.
        inputs = args.model_dump()
        card = await services.cards.open(
            f"Workflow · {ref}", kind="workflow",
        )
        run_id = await services.workflows.start(
            ref, dict(inputs), card=card,
        )
        anchor: dict[str, object] = {
            "kind": "workflow_card",
            "run_id": str(run_id),
            "ref": ref,
        }

        # bug-031: long-poll until terminal or timeout. Duck-typed on
        # ``status`` so test mocks without the method fall back to a
        # ``phase="running"`` return (matches the generic tool's
        # fallback semantics).
        status_probe = getattr(services.workflows, "status", None)
        if status_probe is None:
            return {
                "run_id": str(run_id),
                "card_id": str(card.id),
                "phase": "running",
                "_chat_anchor": anchor,
                "note": (
                    "Workflow started; this service does not expose "
                    "status polling."
                ),
            }

        timeout_seconds = int(
            getattr(
                services.workflows,
                "default_timeout_seconds",
                _VIRTUAL_DEFAULT_TIMEOUT_SECONDS,
            )
        )
        deadline_polls = max(
            1,
            int(timeout_seconds / _VIRTUAL_POLL_INTERVAL_SECONDS),
        )
        for _ in range(deadline_polls):
            await _asyncio.sleep(_VIRTUAL_POLL_INTERVAL_SECONDS)
            status_view = status_probe(run_id)
            phase = getattr(status_view, "phase", None)
            if isinstance(phase, str) and phase in _VIRTUAL_TERMINAL_PHASES:
                outputs = getattr(status_view, "outputs", None)
                error_message = getattr(status_view, "error_message", None)
                return {
                    "run_id": str(run_id),
                    "card_id": str(card.id),
                    "phase": phase,
                    "outputs": outputs,
                    "error_message": error_message,
                    "_chat_anchor": anchor,
                }

        return {
            "run_id": str(run_id),
            "card_id": str(card.id),
            "phase": "running",
            "_chat_anchor": anchor,
            "note": (
                f"Workflow still running after {timeout_seconds} "
                f"seconds; call workflow_status(run_id={str(run_id)!r}) "
                f"to check later."
            ),
        }

    return virtual_invoker

"""``validate_workflow`` — wrap the run-start gate into a callable tool.

Phase 3.11 §17.3 — the meta-agent's iteration loop calls this tool
after every YAML draft. The tool loads the workflow file from disk,
runs :func:`workflows.validator.validate_workflow` against it, and
returns a structured :class:`ValidationOutput` carrying one
:class:`ValidationFinding` per error/warning. The agent reads the
finding's ``error_class`` + ``identifying_names`` and consults its
skill's error-class table to plan the next revision.

Pure read — never writes, never spawns. The catalog is built
on-demand from ``ctx.workspace_root`` (no per-call cache yet; the
scan is single-digit-ms on a small workspace and the tool fires once
per agent turn).

Spec: ``process/phase3.11-author-workflow-meta-agent-spec.md`` §17.3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from assets.catalog import scan as scan_catalog
from errors import ToolExecutionFailed
from files import check_path_allowed
from tools.decorator import ToolContext, tool
from workflows.errors import WorkflowValidationError, WorkflowValidationWarning
from workflows.parser import load_workflow
from workflows.validator import validate_workflow as engine_validate_workflow


class ValidateWorkflowArgs(BaseModel):
    """Spec §17.3 — exactly one of ``workflow_ref`` / ``yaml_path``."""

    workflow_ref: str | None = Field(
        default=None,
        description=(
            "Catalog ref of the workflow to validate "
            "(e.g., ``kolsch/workflows/hello``). Resolved against the "
            "workspace's asset catalog. Mutually exclusive with "
            "``yaml_path``."
        ),
    )
    yaml_path: str | None = Field(
        default=None,
        description=(
            "Direct path to a workflow YAML on disk. "
            "Workspace-relative (``bundles/my-bundle/workflows/x.yaml``) "
            "or absolute (must resolve inside ``file_access.allowed_paths``). "
            "Use when validating a freshly-written draft that hasn't "
            "been catalog-registered yet. Mutually exclusive with "
            "``workflow_ref``."
        ),
    )


class ValidationFinding(BaseModel):
    """One validator-emitted finding, structured per spec §17.3."""

    error_class: str = Field(
        description="The exception subclass name (e.g., 'WorkflowBindCollision')."
    )
    identifying_names: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Keys named by the exception's "
            "``identifying_names_keys`` ClassVar mapped to their "
            "runtime values."
        ),
    )
    caller_message: str = Field(
        description=(
            "The exception's plain-English ``caller_message`` ClassVar. "
            "Falls back to ``str(exc)`` when the class doesn't set one."
        ),
    )
    file: str = Field(
        description="Source file path the finding points at, or '<catalog>' / '<args>'."
    )
    line: int = Field(description="Line number, or 0 when not available.")


class ValidationOutput(BaseModel):
    """The tool's return shape per spec §17.3."""

    is_valid: bool = Field(
        description=(
            "True iff the workflow passes the run-start gate "
            "(empty errors list). Warnings do NOT affect validity."
        ),
    )
    errors: list[ValidationFinding] = Field(default_factory=list)
    warnings: list[ValidationFinding] = Field(default_factory=list)


# The tool is loaded by ``discover_tools_in_file`` via
# ``importlib.util.spec_from_file_location`` into a synthetic module
# name; combined with ``from __future__ import annotations``,
# Pydantic needs an explicit ``model_rebuild`` to resolve the
# stringified ``dict[str, Any]`` and forward references against the
# synthetic module's namespace.
ValidationFinding.model_rebuild()
ValidationOutput.model_rebuild()


@tool(
    name="validate_workflow",
    summary="Validate a workflow YAML against the run-start gate.",
    description=(
        "Validate a workflow YAML against the engine's run-start gate "
        "and return structured findings. Use this AFTER writing or "
        "modifying a workflow draft, BEFORE telling the author the "
        "workflow is ready. The findings carry an ``error_class`` and "
        "``identifying_names`` so the agent can diagnose without "
        "re-parsing English. "
        "\n\n"
        "Pass EXACTLY ONE of ``workflow_ref`` (catalog ref) OR "
        "``yaml_path`` (direct path to a YAML on disk). "
        "\n\n"
        "Returns ``{is_valid: bool, errors: [...], warnings: [...]}``. "
        "Each finding is ``{error_class, identifying_names, "
        "caller_message, file, line}``. The tool never raises on a "
        "validation failure — invalid workflows surface as "
        "``is_valid: false`` with one or more findings. Missing files, "
        "malformed YAML, and bad args (both / neither workflow_ref / "
        "yaml_path) are also surfaced as findings, not raises, so the "
        "agent's iteration loop never crashes mid-draft."
    ),
    args_schema=ValidateWorkflowArgs,
)
async def validate_workflow(
    args: ValidateWorkflowArgs, ctx: ToolContext
) -> dict[str, Any]:
    """Run the workflow validator and return :class:`ValidationOutput`."""
    if ctx.path_policy is None or ctx.workspace_root is None:
        msg = (
            "validate_workflow requires the platform to populate "
            "ctx.path_policy and ctx.workspace_root."
        )
        raise ToolExecutionFailed(
            tool_name="validate_workflow", error_class="RuntimeError"
        ) from RuntimeError(msg)

    if (args.workflow_ref is None) == (args.yaml_path is None):
        return _output(
            is_valid=False,
            errors=[
                ValidationFinding(
                    error_class="InvalidToolArgs",
                    identifying_names={},
                    caller_message=(
                        "exactly one of workflow_ref or yaml_path must be set"
                    ),
                    file="<args>",
                    line=0,
                )
            ],
        )

    catalog = scan_catalog(ctx.workspace_root)
    if args.workflow_ref is not None:
        entry = catalog.lookup(args.workflow_ref, expected_type="workflow")
        if entry is None:
            return _output(
                is_valid=False,
                errors=[
                    ValidationFinding(
                        error_class="WorkflowNotFound",
                        identifying_names={"ref": args.workflow_ref},
                        caller_message=(
                            f"workflow {args.workflow_ref!r} not in catalog"
                        ),
                        file="<catalog>",
                        line=0,
                    )
                ],
            )
        path = Path(entry.path)
    else:
        # args.yaml_path is non-None by the XOR check above.
        yaml_path = args.yaml_path
        assert yaml_path is not None  # noqa: S101 — narrow for type-checker
        # Enforce the workspace's path policy on direct paths so the
        # agent can't read arbitrary disk locations via this tool.
        check_path_allowed(
            yaml_path, policy=ctx.path_policy, root=ctx.workspace_root
        )
        path = _resolve(yaml_path, ctx.workspace_root)

    try:
        workflow = load_workflow(path)
    except WorkflowValidationError as exc:
        return _output(
            is_valid=False,
            errors=[_finding_from(exc, default_file=str(path))],
        )

    result = engine_validate_workflow(workflow, catalog, file=str(path))
    return _output(
        is_valid=result.is_valid,
        errors=[
            _finding_from(err, default_file=str(path)) for err in result.errors
        ],
        warnings=[
            _finding_from(warn, default_file=str(path))
            for warn in result.warnings
        ],
    )


def _output(
    *,
    is_valid: bool,
    errors: list[ValidationFinding] | None = None,
    warnings: list[ValidationFinding] | None = None,
) -> dict[str, Any]:
    """Shape the response. Returns a plain dict so the agent layer's
    JSON serialiser sees a regular mapping."""
    return ValidationOutput(
        is_valid=is_valid,
        errors=errors or [],
        warnings=warnings or [],
    ).model_dump(mode="json")


def _finding_from(
    exc: WorkflowValidationError | WorkflowValidationWarning,
    *,
    default_file: str,
) -> ValidationFinding:
    """Convert a validator-emitted exception into a structured finding.

    Reads the exception's ``identifying_names_keys`` ClassVar (per
    R3 #11) and pulls every named attribute into the finding's
    ``identifying_names`` dict. Falls back to ``str(exc)`` for the
    caller-facing message when the ``caller_message`` ClassVar is the
    inherited default — but the validator-tree base classes set one
    explicitly so this is a defensive guard.
    """
    keys = getattr(type(exc), "identifying_names_keys", ())
    identifying_names: dict[str, Any] = {}
    for key in keys:
        if hasattr(exc, key):
            value = getattr(exc, key)
            identifying_names[key] = _jsonable(value)
    return ValidationFinding(
        error_class=type(exc).__name__,
        identifying_names=identifying_names,
        caller_message=getattr(type(exc), "caller_message", str(exc)),
        file=getattr(exc, "file", default_file) or default_file,
        line=int(getattr(exc, "line", 0) or 0),
    )


def _jsonable(value: Any) -> Any:  # noqa: ANN401 — heterogeneous attr values
    """Coerce identifying-name values into JSON-serialisable shapes.

    Tuples become lists (the validator uses tuples for cycle paths +
    ambiguous-start candidates so a single ``ValidationFinding`` round-
    trips cleanly through JSON when the agent layer serialises it).
    Everything else passes through — Pydantic's model_dump handles
    primitives + nested containers natively.
    """
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _resolve(path: str, root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate

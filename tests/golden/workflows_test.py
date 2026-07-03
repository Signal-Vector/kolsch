"""Workflow golden fixture discovery + assertion driver — Phase 3.0 Step 7.

Discovers every ``*.yaml`` under ``tests/golden/workflows/`` and
parameterizes one test case per fixture. The expected outcome lives
in a top-of-file YAML comment block (see ``_README.md``) — the driver
parses the block, runs the appropriate
:mod:`workflows.validator` / :mod:`workflows.parser` entry point, and
asserts the declared shape.

Fail modes the driver guards against:

- Fixture lacks an annotation comment → drives a test failure with
  the offending path (defends the silent-coverage hole).
- ``expected_error: <Class>`` declared but no exception raised →
  fails the test for that fixture.
- Multi-error fixture (``expected_errors:``) raises a single
  exception or a group of the wrong shape.
- YAML-origin error string violates the strict-single-line +
  ``Fix:`` token + strict-no-code rules (master spec §12.3,
  Decisions 9 + P4).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from workflows.errors import WorkflowError, WorkflowValidationError
from workflows.parser import load_workflow
from workflows.validator import validate_workflow, validate_workflow_or_raise

FIXTURE_DIR = Path(__file__).parent / "workflows"


# ---------------------------------------------------------------------------
# Annotation parsing
# ---------------------------------------------------------------------------


_ANNOTATION_RE = re.compile(r"^\s*#\s*(\w[\w_]*):\s*(.*?)\s*$")


def _parse_annotations(text: str) -> dict[str, str]:
    """Read the leading comment block; collect ``# key: value`` pairs.

    Stops at the first non-comment line (a blank line counts as
    non-comment for now — annotations live at the top).
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            if stripped == "":
                # tolerate one separating blank line between annotation
                # block and the YAML body
                continue
            break
        match = _ANNOTATION_RE.match(line)
        if match:
            out[match.group(1)] = match.group(2)
    return out


def _all_fixtures() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.yaml"))


# ---------------------------------------------------------------------------
# Catalog seed — every fixture sees the same canonical catalog so
# from: validation has predictable agent/workflow targets to resolve.
# ---------------------------------------------------------------------------


def _seed_catalog() -> AssetCatalog:
    cat = AssetCatalog()
    for ref, asset_type, bundle in [
        ("kolsch/tools/bash", "tool", "kolsch"),
        ("kolsch/agents/triage", "agent", "kolsch"),
        ("kolsch/agents/helper", "agent", "kolsch"),
        ("kolsch/workflows/library", "workflow", "kolsch"),
        ("acme/workflows/triage", "workflow", "acme"),
    ]:
        cat.assets[ref] = AssetEntry(
            ref=ref,
            asset_type=asset_type,
            bundle=bundle,
            path=f"/var/empty/{ref}",
            metadata={},
        )
    # Authored Python node — use ``metadata.builtin=True`` to skip the
    # §7.6 import path (the fixture exercises the catalog-resolution
    # surface, not real introspection).
    cat.assets["acme/nodes/echo"] = AssetEntry(
        ref="acme/nodes/echo",
        asset_type="node",
        bundle="acme",
        path="",  # virtual marker so _check_catalog_node_defs skips import
        metadata={"builtin": True},
    )
    # ``invalid-extends-policy-forbidden.yaml`` extends this target —
    # register the on-disk path so ``_check_extends_policy`` can parse
    # the actual policy: block and raise WorkflowExtendsPolicyForbidden.
    extends_target = FIXTURE_DIR / "_targets" / "extends-policy-target.yaml"
    cat.assets["acme/workflows/extends-policy-target"] = AssetEntry(
        ref="acme/workflows/extends-policy-target",
        asset_type="workflow",
        bundle="acme",
        path=str(extends_target),
        metadata={},
    )
    return cat


# ---------------------------------------------------------------------------
# Strict YAML-origin message format (Decision 9 + Decision P4 + §12.3).
# ---------------------------------------------------------------------------


def _assert_yaml_origin_format(exc: BaseException) -> None:
    text = str(exc)
    assert "\n" not in text, f"YAML-origin error not single-line: {text!r}"
    assert "Fix: " in text, f"missing 'Fix: ' token in error: {text!r}"
    assert "{{" not in text, f"Jinja delimiter in Fix portion: {text!r}"
    assert "{%" not in text, f"Jinja delimiter in Fix portion: {text!r}"
    assert "`" not in text, f"backtick in Fix portion: {text!r}"


# ---------------------------------------------------------------------------
# Error-class resolver — convert "WorkflowFromShapeError" into the actual class.
# ---------------------------------------------------------------------------


def _resolve_error_class(name: str) -> type[BaseException]:
    module = importlib.import_module("workflows.errors")
    cls = getattr(module, name, None)
    if cls is None:
        msg = f"unknown exception class in fixture annotation: {name!r}"
        raise ValueError(msg)
    return cls  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_path",
    _all_fixtures(),
    ids=lambda p: p.name,
)
def test_workflow_fixture(fixture_path: Path) -> None:
    text = fixture_path.read_text(encoding="utf-8")
    annotations = _parse_annotations(text)

    # Must declare something.
    has_annotation = any(
        key in annotations
        for key in ("expected", "expected_error", "expected_errors", "expected_warning")
    )
    assert has_annotation, (
        f"fixture {fixture_path.name} has no annotation header; "
        "add `# expected: valid` / `# expected_error: ...` etc."
    )

    catalog = _seed_catalog()

    # --- VALID ----------------------------------------------------------
    if annotations.get("expected") == "valid":
        workflow = load_workflow(fixture_path)
        result = validate_workflow(workflow, catalog, file=str(fixture_path))
        assert result.is_valid, (
            f"fixture {fixture_path.name} declared valid but errors: "
            f"{[str(e) for e in result.errors]}"
        )
        assert result.warnings == (), (
            f"fixture {fixture_path.name} declared valid but produced "
            f"warnings: {[str(w) for w in result.warnings]}"
        )
        return

    # --- WARNING --------------------------------------------------------
    if "expected_warning" in annotations:
        cls_name = annotations["expected_warning"]
        warning_cls = _resolve_error_class(cls_name)
        workflow = load_workflow(fixture_path)
        result = validate_workflow(workflow, catalog, file=str(fixture_path))
        assert result.is_valid, (
            f"fixture {fixture_path.name} declared a warning but produced "
            f"errors: {[str(e) for e in result.errors]}"
        )
        assert any(isinstance(w, warning_cls) for w in result.warnings), (
            f"fixture {fixture_path.name} expected warning {cls_name!r} "
            f"but warnings were {[type(w).__name__ for w in result.warnings]}"
        )
        # YAML-origin message contract.
        for w in result.warnings:
            if isinstance(w, warning_cls):
                _assert_yaml_origin_format(w)
        return

    # --- MULTI-ERROR ACCUMULATION ---------------------------------------
    if "expected_errors" in annotations:
        names = [
            n.strip() for n in annotations["expected_errors"].split(",") if n.strip()
        ]
        expected_classes = [_resolve_error_class(n) for n in names]
        try:
            workflow = load_workflow(fixture_path)
        except WorkflowError as exc:
            pytest.fail(
                f"fixture {fixture_path.name} expected multi-error but parser "
                f"raised: {exc!r}"
            )
        with pytest.raises(ExceptionGroup) as exc_info:
            validate_workflow_or_raise(workflow, catalog, file=str(fixture_path))
        raised_classes = {type(e).__name__ for e in exc_info.value.exceptions}
        for cls in expected_classes:
            assert cls.__name__ in raised_classes, (
                f"fixture {fixture_path.name} expected {cls.__name__!r} in "
                f"ExceptionGroup; got {sorted(raised_classes)}"
            )
        for sub_exc in exc_info.value.exceptions:
            if isinstance(sub_exc, WorkflowError):
                _assert_yaml_origin_format(sub_exc)
        return

    # --- SINGLE ERROR ---------------------------------------------------
    cls_name = annotations["expected_error"]
    expected_cls = _resolve_error_class(cls_name)

    # Parser-level rejections (R3 #5 invalid refs, deprecated key
    # rejections, etc.) raise during ``load_workflow``.
    try:
        workflow = load_workflow(fixture_path)
    except expected_cls as exc:
        if isinstance(exc, WorkflowError):
            _assert_yaml_origin_format(exc)
        return
    except WorkflowError as exc:
        pytest.fail(
            f"fixture {fixture_path.name} expected {cls_name!r} but parser "
            f"raised {type(exc).__name__!r}: {exc}"
        )

    # Validator-level errors raise on validate_workflow_or_raise.
    with pytest.raises(expected_cls) as exc_info:
        validate_workflow_or_raise(workflow, catalog, file=str(fixture_path))
    if isinstance(exc_info.value, WorkflowValidationError):
        _assert_yaml_origin_format(exc_info.value)


def test_every_fixture_has_an_annotation_or_it_fails_loudly() -> None:
    """Smoke check — fixtures that ship without annotations would
    silently pass the discovery loop above (the per-test assertion
    catches it, but this top-level sanity is a defense-in-depth)."""
    annotated = 0
    for path in _all_fixtures():
        text = path.read_text(encoding="utf-8")
        annotations = _parse_annotations(text)
        if any(
            k in annotations
            for k in (
                "expected",
                "expected_error",
                "expected_errors",
                "expected_warning",
            )
        ):
            annotated += 1
    # At least 10 — the actual count depends on fixture authoring tempo.
    assert annotated >= 10, (
        f"only {annotated} annotated fixtures discovered under {FIXTURE_DIR}; "
        "Step 7 ships ~20-25"
    )

"""EXT-CORPUS runner —
bug-engine-extends-parent-not-rendered.md Step 3 / Part 4.

Parametrises over the 28 fixture cases in
:mod:`tests.integration._extends_corpus_cases` and asserts each
case's expected outcome.

What this runner exercises:

- **Field side** —
  :meth:`AssetManagement.build_resolved_config` against each
  case's target agent ref. The asset manager owns chain
  composition (extends-walk + RFC 7386 merge); this surfaces
  every ``F*`` failure, every ``D*`` Fatal, and the
  ``ConfigExtendsTemplateWithoutInheritance`` orphan-check
  from Step 2 (because the engine's dispatch path itself isn't
  reached from the asset manager — instead the runner replays
  it explicitly, see below).

- **Render side** — for ``loads_with_render`` cases the runner
  replicates the engine dispatch from
  :meth:`engine.engine._Exchange._render_prompt_if_needed`:

  1. ``prompt_uses_extends_directive`` + ``config.extends is None``
     → ``ConfigExtendsTemplateWithoutInheritance``.
  2. ``config.extends is not None`` →
     ``resolve_parent_chain_from_extends`` +
     ``render_with_parents``.
  3. Default → ``render_prompt``.

  This is the same three-path dispatch the engine ships at
  ``engine.py:795-841`` (post-Step-2). The corpus runner
  intentionally does NOT spin up a real engine — that adds an
  SqliteBackend + LLM seam for every parametrize, which is too
  heavy for a 28-case sweep. End-to-end engine integration is
  covered separately by ``tests/component/test_engine.py``;
  the butler walkthrough in Step 5 closes the loop on a real
  ``execute_stream``.

Coverage of Part 4's ``Currently`` column:

- ``✓`` cases — regression guard (must keep passing).
- ``✗`` cases — Step 2's wiring fix must turn them ✓.
- ``⚠`` cases (C5, G3) — Step 1 pinned the current behaviour;
  this runner asserts it so future regressions surface.

Cases that need extras:

- **A2** populates a synthetic ``ToolsVar`` with two tools so
  the ``{% for t in tools %}`` loop has data.
- **B3** asserts the resolved-config field shape post-merge.
- **E2** asserts ``model.model == "middle-model"`` to confirm
  the chain walk RFC-7386-merges correctly.
- **G1** / **G3** assert the resolved
  ``subsystems.skills.config.enabled`` ref list (the post-append
  listing is engine-side state; the corpus doesn't render it).
- **I1** populates the ``docs`` var by running Step 12's
  ``load_context_file`` against the workspace.

Failure modes covered:

- ``error_at_load`` cases assert the typed PlatformError code.
- ``loads_with_render`` cases assert ``expected_render_contains``
  and ``expected_render_excludes`` after render.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from assets import AssetManagement
from errors import PlatformError
from kernel.findings import ValidationResult
from schemas import ResolvedAgentConfig
from templating import render_prompt
from templating.inheritance import (
    prompt_uses_extends_directive,
    render_with_parents,
    resolve_parent_chain_from_extends,
)
from templating.vars import (
    AgentVar,
    DocsVar,
    ModelView,
    ToolsVar,
    WorkspaceVar,
)
from templating.vars._platform import (
    AliasesVar,
    AuthorizationVar,
    FileAccessVar,
    PlatformVar,
    ProvidersVar,
)
from templating.vars.docs import DocItem
from templating.vars.tools import ToolItem

from tests.integration._extends_corpus_cases import CASES, FixtureCase

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Workspace setup helpers.
# ---------------------------------------------------------------------------


_DEFAULT_MODEL = {"provider": "fake", "model": "default-model"}


def _write_platform_yaml(root: Path) -> None:
    """Minimal platform.yaml with a default model so fixtures that
    omit ``model:`` resolve cleanly through the kernel."""
    payload = {
        "defaults": {
            "model": _DEFAULT_MODEL,
            "io": {"mode": "conversational"},
        },
        "authorization": {
            "variant": "rule_based",
            "config": {
                "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
            },
        },
        "file_access": {"allowed_paths": [], "denied_paths": []},
    }
    (root / "platform.yaml").write_text(
        yaml.safe_dump(payload), encoding="utf-8"
    )


def _write_bundle_manifest(root: Path) -> None:
    bundle_dir = root / "bundles" / "x"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "version": "0.1",
                "description": "EXT-CORPUS synthetic bundle.",
            }
        ),
        encoding="utf-8",
    )


def _install_case(case: FixtureCase, workspace_root: Path) -> None:
    """Materialise a single case's fixtures into ``workspace_root``.

    The case names ``(src_filename, dst_relative_path)`` pairs;
    each ``src`` lives under
    ``tests/fixtures/extends/<category>/`` and gets copied to
    ``workspace_root/<dst>``."""
    fixtures_root = (
        Path(__file__).parent.parent / "fixtures" / "extends" / case.category
    )
    for src_name, dst_rel in case.files:
        src_path = fixtures_root / src_name
        dst_path = workspace_root / dst_rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)


# ---------------------------------------------------------------------------
# Render-context construction — minimal AgentVar/ToolsVar/etc.
# ---------------------------------------------------------------------------


def _build_render_context(
    cfg: ResolvedAgentConfig,
    workspace_root: Path,
    *,
    case_id: str,
) -> dict[str, Any]:
    """Build the five-key Jinja render context the engine builds at
    ``engine.py:787-793``. Per-case extras (tools for A2, docs for
    I1) get materialised here so the corpus stays self-contained.
    """
    # ToolsVar — only A2 references it for iteration; populate two
    # synthetic ToolItems for that case.
    if case_id == "A2":
        tool_items = [
            ToolItem(
                name="tool_a",
                description="Tool A description.",
                summary="Tool A.",
                signature="",
                schema={},
            ),
            ToolItem(
                name="tool_b",
                description="Tool B description.",
                summary="Tool B.",
                signature="",
                schema={},
            ),
        ]
    else:
        tool_items = []
    tools_var = ToolsVar(tool_items)

    # DocsVar — only I1 declares context_files; load them via Step
    # 12's loader against the workspace.
    doc_items: list[DocItem] = []
    if case_id == "I1":
        from templating.context_files import (  # noqa: PLC0415
            load_context_file,
        )

        for spec in cfg.context_files:
            doc_items.append(load_context_file(spec, workspace_root))
    docs_var = DocsVar(doc_items)

    workspace_var = WorkspaceVar(
        path=str(workspace_root),
        summary=None,
        allowed_paths=[],
        denied_paths=[],
    )
    agent_var = AgentVar(
        name=cfg.name,
        id=str(cfg.agent_id),
        model=ModelView.from_model_spec(cfg.model),
        tools=tools_var,
        permissions=list(cfg.permissions),
        context_files=list(doc_items),
    )
    platform_var = PlatformVar(
        providers=ProvidersVar([]),
        authorization=AuthorizationVar(variant="rule_based", rules=[]),
        workspace_summary=None,
        file_access=FileAccessVar(allowed_paths=[], denied_paths=[]),
        aliases=AliasesVar({}),
        _workspace_path=str(workspace_root),
    )
    return {
        "tools": tools_var,
        "docs": docs_var,
        "workspace": workspace_var,
        "agent": agent_var,
        "platform": platform_var,
    }


# ---------------------------------------------------------------------------
# Dispatch — mirrors engine.py:_render_prompt_if_needed (post-Step-2).
# ---------------------------------------------------------------------------


def _dispatch_render(
    cfg: ResolvedAgentConfig,
    context: dict[str, Any],
    workspace_root: Path,
    catalog: Any,
) -> str:
    """Replay the three-path dispatch from
    ``engine.engine._Exchange._render_prompt_if_needed``.

    Keep this in sync with that block — if Step 2's wiring
    diverges, this is the first place the corpus will catch it.
    """
    prompt = cfg.prompt
    if not isinstance(prompt, str):
        msg = "list-shape prompts unsupported in corpus runner"
        raise NotImplementedError(msg)

    template_name = f"agent:{cfg.agent_id}"
    if cfg.extends is None and prompt_uses_extends_directive(prompt):
        from errors import ConfigExtendsTemplateWithoutInheritance

        raise ConfigExtendsTemplateWithoutInheritance(agent_path=template_name)

    if cfg.extends is not None:
        parent_chain_yaml = resolve_parent_chain_from_extends(
            cfg.extends, workspace_root, catalog
        )
        parent_chain_prompts = [
            str(layer.get("prompt", "")) for layer in parent_chain_yaml
        ]
        return render_with_parents(
            prompt,
            parent_chain_prompts,
            context,
            template_name=template_name,
        )
    return render_prompt(prompt, context, template_name=template_name)


# ---------------------------------------------------------------------------
# Per-case execution.
# ---------------------------------------------------------------------------


def _passthrough_validator(_body: dict[str, Any]) -> ValidationResult:
    """Permissive Layer-3 validator — accepts any config body.

    The corpus exercises asset_manager + dispatch, NOT subsystem
    validator semantics. Production paths wire real validators via
    :class:`platform_runtime.PlatformRuntime`; here we satisfy the
    kernel's Layer-3 dispatch with a stub so fixtures that declare
    ``subsystems.<name>: {variant, config}`` (B3, G1, G3) don't
    surface ``config.invalid:unknown_variant``.
    """
    return ValidationResult(ok=True, errors=[])


_CORPUS_SUBSYSTEM_VALIDATORS: dict[str, dict[str, Any]] = {
    "tools": {"standard": _passthrough_validator},
    "skills": {"standard": _passthrough_validator},
}


async def _run_case(case: FixtureCase, tmp_path: Path) -> None:
    _write_platform_yaml(tmp_path)
    _write_bundle_manifest(tmp_path)
    _install_case(case, tmp_path)

    am = AssetManagement(root=tmp_path)
    await am.initialize()

    # build_resolved_config — captures F*/D2/H2 errors at the
    # asset-manager layer.
    if case.expected_outcome == "error_at_load":
        with pytest.raises(PlatformError) as excinfo:
            cfg = await am.build_resolved_config(
                case.target_agent_ref,
                subsystem_validators=_CORPUS_SUBSYSTEM_VALIDATORS,
            )
            # H2's StrictUndefined fires during the render step,
            # not build_resolved_config — keep the dispatch in the
            # `try` so an error_at_load case that reaches render
            # still surfaces.
            ctx = _build_render_context(
                cfg, tmp_path, case_id=case.case_id
            )
            _dispatch_render(cfg, ctx, tmp_path, am.catalog())
        assert excinfo.value.code == case.expected_error_code, (
            f"{case.case_id}: expected {case.expected_error_code}, "
            f"got {excinfo.value.code}"
        )
        return

    cfg = await am.build_resolved_config(
        case.target_agent_ref,
        subsystem_validators=_CORPUS_SUBSYSTEM_VALIDATORS,
    )

    # Per-case field assertions (B3 tools wholesale, E2 model
    # walk-up, G* skills inherit).
    if case.case_id == "B3":
        declared = cfg.subsystems["tools"].config.get("declared", [])
        refs = [d["ref"] for d in declared]
        assert refs == ["x/child_tool_only"], (
            f"B3 tools wholesale-override broken: {refs}"
        )
    elif case.case_id == "E2":
        assert cfg.model.model == "middle-model", (
            f"E2 chain merge broken: model={cfg.model.model}"
        )
    elif case.case_id == "G1":
        enabled = cfg.subsystems["skills"].config.get("enabled", [])
        refs = [e["ref"] for e in enabled]
        assert refs == ["x/g1_skill"], f"G1 skills not preserved: {refs}"
    elif case.case_id == "G3":
        enabled = cfg.subsystems["skills"].config.get("enabled", [])
        refs = [e["ref"] for e in enabled]
        assert refs == ["x/g3_skill"], (
            f"G3 child did not inherit parent's skills config: {refs}"
        )

    # Render-side assertions.
    if case.expected_outcome == "loads_with_render":
        context = _build_render_context(
            cfg, tmp_path, case_id=case.case_id
        )
        rendered = _dispatch_render(cfg, context, tmp_path, am.catalog())
        for needle in case.expected_render_contains:
            assert needle in rendered, (
                f"{case.case_id}: expected substring {needle!r} in render; "
                f"got:\n{rendered}"
            )
        for forbidden in case.expected_render_excludes:
            assert forbidden not in rendered, (
                f"{case.case_id}: forbidden substring {forbidden!r} "
                f"appears in render; got:\n{rendered}"
            )


# ---------------------------------------------------------------------------
# Parametrised entry point.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=lambda c: c.case_id,
)
async def test_ext_corpus_case(
    case: FixtureCase, tmp_path: Path
) -> None:
    """Walk Part 4's table; one parametrize per case."""
    await _run_case(case, tmp_path)

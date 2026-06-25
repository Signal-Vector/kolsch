"""EXT-CORPUS case table —
bug-engine-extends-parent-not-rendered.md Part 4.

One entry per Part-4 ID (A1..K2, 24 total). Each case names:

- ``case_id`` — the Part-4 ID (verbatim, uppercase).
- ``category`` — one-letter category code (matches the subdir
  under ``tests/fixtures/extends/`` and the Part-4 grouping).
- ``files`` — list of ``(fixture_filename, dst_path_in_workspace)``
  pairs. The runner copies each from
  ``tests/fixtures/extends/<category>/<fixture_filename>`` to
  ``<workspace>/<dst>``.
- ``target_agent_ref`` — the 3-segment catalog ref the runner
  passes to :meth:`AssetManagement.build_resolved_config`. Always
  ``x/agents/<agent_name>`` since fixtures live under the synthetic
  ``x`` bundle.
- ``expected_outcome`` — one of:
    - ``loads_with_render``: build_resolved_config succeeds AND
      the render produces output.
    - ``error_at_load``: build_resolved_config raises a
      :class:`PlatformError`; ``expected_error_code`` must match.
    - ``loads_with_render_warning``: render succeeds AND a
      Recoverable warning ought to be emitted (e.g. H1).
- ``expected_error_code`` — the ``code`` attribute of the
  :class:`PlatformError` for ``error_at_load`` cases.
- ``expected_render_contains`` — substrings the rendered prompt
  must contain.
- ``expected_render_excludes`` — substrings the rendered prompt
  must NOT contain.
- ``notes`` — free-form annotation for ⚠ (undertested) cases or
  edge-cases worth flagging at the table.

The fixture layout matches the Step 1 pin —
``tests/fixtures/extends/<a..k>/`` per category. Within a
category, file names start with the case ID (e.g.
``c1_parent.yaml``, ``c1_child.yaml``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class FixtureCase:
    case_id: str
    category: str
    files: tuple[tuple[str, str], ...]
    target_agent_ref: str
    expected_outcome: str
    expected_error_code: str | None = None
    expected_render_contains: tuple[str, ...] = ()
    expected_render_excludes: tuple[str, ...] = ()
    notes: str = ""


FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "extends"


# Per-category agent dst convention — every fixture YAML lands at
# bundles/x/agents/<filename>, and skills under
# bundles/x/skills/<name>/SKILL.md. Workspace-level docs (I1) go
# at the workspace root.
def _agent(name: str) -> str:
    return f"bundles/x/agents/{name}"


def _skill(name: str) -> str:
    return f"bundles/x/skills/{name}/SKILL.md"


CASES: tuple[FixtureCase, ...] = (
    # ------------------------------------------------------------------
    # Category A — No inheritance (baseline regression guard).
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="A1",
        category="a",
        files=(("a1_plain.yaml", _agent("a1_plain.yaml")),),
        target_agent_ref="x/agents/a1_plain",
        expected_outcome="loads_with_render",
        expected_render_contains=("You are a plain agent.",),
    ),
    FixtureCase(
        case_id="A2",
        category="a",
        files=(("a2_jinja.yaml", _agent("a2_jinja.yaml")),),
        target_agent_ref="x/agents/a2_jinja",
        expected_outcome="loads_with_render",
        expected_render_contains=("You are a2", "tool_a"),
        notes="Render context populated with two synthetic ToolItems.",
    ),
    # ------------------------------------------------------------------
    # Category B — Field inheritance (RFC 7386).
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="B1",
        category="b",
        files=(
            ("b1_parent.yaml", _agent("b1_parent.yaml")),
            ("b1_child.yaml", _agent("b1_child.yaml")),
        ),
        target_agent_ref="x/agents/b1_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("You are b1_child",),
    ),
    FixtureCase(
        case_id="B2",
        category="b",
        files=(
            ("b2_parent.yaml", _agent("b2_parent.yaml")),
            ("b2_child.yaml", _agent("b2_child.yaml")),
        ),
        target_agent_ref="x/agents/b2_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("Parent body",),
        notes="Field-only check: resolved model == child's; prompt inherited.",
    ),
    FixtureCase(
        case_id="B3",
        category="b",
        files=(
            ("b3_parent.yaml", _agent("b3_parent.yaml")),
            ("b3_child.yaml", _agent("b3_child.yaml")),
        ),
        target_agent_ref="x/agents/b3_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("Parent body",),
        notes=(
            "Tools-list wholesale override checked at the resolved-config "
            "level (runner asserts declared length + ref)."
        ),
    ),
    FixtureCase(
        case_id="B4",
        category="b",
        files=(
            ("b4_parent.yaml", _agent("b4_parent.yaml")),
            ("b4_child.yaml", _agent("b4_child.yaml")),
        ),
        target_agent_ref="x/agents/b4_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("CHILD_FRESH_BODY",),
        expected_render_excludes=("PARENT_BODY_SHOULD_BE_IGNORED",),
    ),
    # ------------------------------------------------------------------
    # Category C — Prompt block inheritance (the bug surface).
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="C1",
        category="c",
        files=(
            ("c1_parent.yaml", _agent("c1_parent.yaml")),
            ("c1_child.yaml", _agent("c1_child.yaml")),
        ),
        target_agent_ref="x/agents/c1_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("Header", "OVERRIDDEN-ROLE", "Footer"),
        expected_render_excludes=("parent-default-role",),
    ),
    FixtureCase(
        case_id="C2",
        category="c",
        files=(
            ("c2_parent.yaml", _agent("c2_parent.yaml")),
            ("c2_child.yaml", _agent("c2_child.yaml")),
        ),
        target_agent_ref="x/agents/c2_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("Header", "parent-default-role", "Footer"),
    ),
    FixtureCase(
        case_id="C3",
        category="c",
        files=(
            ("c3_parent.yaml", _agent("c3_parent.yaml")),
            ("c3_child.yaml", _agent("c3_child.yaml")),
        ),
        target_agent_ref="x/agents/c3_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("Plain parent prompt with no blocks.",),
        expected_render_excludes=("silently discarded",),
        notes="Jinja drops child's non-block content when parent has no blocks.",
    ),
    FixtureCase(
        case_id="C4",
        category="c",
        files=(
            ("c4_parent.yaml", _agent("c4_parent.yaml")),
            ("c4_child.yaml", _agent("c4_child.yaml")),
        ),
        target_agent_ref="x/agents/c4_child",
        expected_outcome="loads_with_render",
        expected_render_contains=(
            "OVERRIDDEN-ROLE",
            "parent-default-tail",
            "middle prose",
        ),
        expected_render_excludes=("parent-default-role",),
    ),
    FixtureCase(
        case_id="C5",
        category="c",
        files=(("c5_orphan.yaml", _agent("c5_orphan.yaml")),),
        target_agent_ref="x/agents/c5_orphan",
        expected_outcome="error_at_load",
        expected_error_code="config.extends_template_without_inheritance",
        notes=(
            "Step 1's pinned class (already exists in errors/kernel.py); "
            "Step 2 wires the raise at the dispatch site."
        ),
    ),
    # ------------------------------------------------------------------
    # Category D — Required blocks.
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="D1",
        category="d",
        files=(
            ("d1_parent.yaml", _agent("d1_parent.yaml")),
            ("d1_child.yaml", _agent("d1_child.yaml")),
        ),
        target_agent_ref="x/agents/d1_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("Header", "HERE", "Footer"),
    ),
    FixtureCase(
        case_id="D2",
        category="d",
        files=(
            ("d2_parent.yaml", _agent("d2_parent.yaml")),
            ("d2_child.yaml", _agent("d2_child.yaml")),
        ),
        target_agent_ref="x/agents/d2_child",
        expected_outcome="error_at_load",
        expected_error_code="config.required_block_missing",
    ),
    FixtureCase(
        case_id="D3",
        category="d",
        files=(
            ("d3_parent.yaml", _agent("d3_parent.yaml")),
            ("d3_child.yaml", _agent("d3_child.yaml")),
        ),
        target_agent_ref="x/agents/d3_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("Fresh child prompt",),
        expected_render_excludes=("Header", "Footer"),
        notes=(
            "Child writes fresh prompt without `{% extends parent %}`; "
            "required-block check tied to inheritance dispatch must NOT "
            "fire — see docs/how-to/agent-templating.md:315-319."
        ),
    ),
    # ------------------------------------------------------------------
    # Category E — Multi-level chains.
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="E1",
        category="e",
        files=(
            ("e1_c.yaml", _agent("e1_c.yaml")),
            ("e1_b.yaml", _agent("e1_b.yaml")),
            ("e1_a.yaml", _agent("e1_a.yaml")),
        ),
        target_agent_ref="x/agents/e1_a",
        expected_outcome="loads_with_render",
        expected_render_contains=(
            "ROOT_HEADER",
            "LEAF_OVERRIDE",
            "ROOT_FOOTER",
        ),
        expected_render_excludes=("root-default",),
    ),
    FixtureCase(
        case_id="E2",
        category="e",
        files=(
            ("e2_c.yaml", _agent("e2_c.yaml")),
            ("e2_b.yaml", _agent("e2_b.yaml")),
            ("e2_a.yaml", _agent("e2_a.yaml")),
        ),
        target_agent_ref="x/agents/e2_a",
        expected_outcome="loads_with_render",
        expected_render_contains=("Root body",),
        notes=(
            "Field-side check: resolved model.model == 'middle-model'; "
            "prompt inherited from C. Render-side just confirms the "
            "inherited prompt renders cleanly."
        ),
    ),
    # ------------------------------------------------------------------
    # Category F — Cycles + missing references (clean failure).
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="F1",
        category="f",
        files=(("f1_self.yaml", _agent("f1_self.yaml")),),
        target_agent_ref="x/agents/f1_self",
        expected_outcome="error_at_load",
        expected_error_code="config.agent_inheritance_cycle",
    ),
    FixtureCase(
        case_id="F2",
        category="f",
        files=(
            ("f2_a.yaml", _agent("f2_a.yaml")),
            ("f2_b.yaml", _agent("f2_b.yaml")),
        ),
        target_agent_ref="x/agents/f2_a",
        expected_outcome="error_at_load",
        expected_error_code="config.agent_inheritance_cycle",
    ),
    FixtureCase(
        case_id="F3",
        category="f",
        files=(("f3_orphan.yaml", _agent("f3_orphan.yaml")),),
        target_agent_ref="x/agents/f3_orphan",
        expected_outcome="error_at_load",
        expected_error_code="config.parent_agent_not_found",
    ),
    FixtureCase(
        case_id="F4",
        category="f",
        files=(("f4_no_prefix.yaml", _agent("f4_no_prefix.yaml")),),
        target_agent_ref="x/agents/f4_no_prefix",
        expected_outcome="error_at_load",
        expected_error_code="config.parent_agent_not_found",
        notes=(
            "Post-qol-prefix-defaults, the permissive validator accepts "
            "any non-empty string; an unparseable many-segment ref falls "
            "through to resolution and surfaces as parent_agent_not_found."
        ),
    ),
    FixtureCase(
        case_id="F5",
        category="f",
        files=(("f5_kolsch_no_agent.yaml", _agent("f5_kolsch.yaml")),),
        target_agent_ref="x/agents/f5_kolsch",
        expected_outcome="error_at_load",
        expected_error_code="config.parent_agent_not_found",
        notes=(
            "`kolsch:agents/...` resolves via importlib.resources against "
            "the installed kolsch package; the package ships only docs/ "
            "+ AGENTS.md, so the lookup fails with parent_agent_not_found."
        ),
    ),
    # ------------------------------------------------------------------
    # Category G — Skills + prompts (post-append vs inline).
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="G1",
        category="g",
        files=(
            ("g1_agent.yaml", _agent("g1_agent.yaml")),
            ("g1_skill.md", _skill("g1_skill")),
        ),
        target_agent_ref="x/agents/g1_agent",
        expected_outcome="loads_with_render",
        expected_render_contains=("G1 agent body",),
        notes=(
            "Field-side check: resolved subsystems.skills.config.enabled "
            "carries the single ref. The post-append behaviour lives in "
            "engine/assembly.py:83-87 and is exercised end-to-end in "
            "the component suite, not here."
        ),
    ),
    FixtureCase(
        case_id="G3",
        category="g",
        files=(
            ("g3_parent.yaml", _agent("g3_parent.yaml")),
            ("g3_child.yaml", _agent("g3_child.yaml")),
            ("g3_skill.md", _skill("g3_skill")),
        ),
        target_agent_ref="x/agents/g3_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("Parent body",),
        notes=(
            "Field-side check: child inherits parent's skills config "
            "wholesale (RFC 7386). Was ⚠ pre-Step-2; corpus pins as ✓."
        ),
    ),
    # ------------------------------------------------------------------
    # Category H — Render-time guarantees.
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="H1",
        category="h",
        files=(("h1_empty_tools.yaml", _agent("h1_empty_tools.yaml")),),
        target_agent_ref="x/agents/h1_empty_tools",
        expected_outcome="loads_with_render",
        expected_render_contains=("Tools:",),
        notes=(
            "agent.template_warning Recoverable is emitted by the engine, "
            "not the dispatch helpers; the corpus runner asserts the "
            "render succeeds — engine warning surface is component-test "
            "territory."
        ),
    ),
    FixtureCase(
        case_id="H2",
        category="h",
        files=(("h2_typo.yaml", _agent("h2_typo.yaml")),),
        target_agent_ref="x/agents/h2_typo",
        expected_outcome="error_at_load",
        expected_error_code="config.template_render_failed",
    ),
    # ------------------------------------------------------------------
    # Category I — context_files prefix resolution.
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="I1",
        category="i",
        files=(
            ("i1_agent.yaml", _agent("i1_agent.yaml")),
            ("i1_doc.md", "i1_doc.md"),
        ),
        target_agent_ref="x/agents/i1_agent",
        expected_outcome="loads_with_render",
        expected_render_contains=("Docs:", "I1 Fixture Doc"),
    ),
    # ------------------------------------------------------------------
    # Category K — Inheritance + Jinja vars.
    # ------------------------------------------------------------------
    FixtureCase(
        case_id="K1",
        category="k",
        files=(
            ("k1_parent.yaml", _agent("k1_parent.yaml")),
            ("k1_child.yaml", _agent("k1_child.yaml")),
        ),
        target_agent_ref="x/agents/k1_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("name-from-block=k1_child",),
        expected_render_excludes=("parent-default",),
        notes="Envelope vars referenced INSIDE a child block render against the child's context.",
    ),
    FixtureCase(
        case_id="K2",
        category="k",
        files=(
            ("k2_parent.yaml", _agent("k2_parent.yaml")),
            ("k2_child.yaml", _agent("k2_child.yaml")),
        ),
        target_agent_ref="x/agents/k2_child",
        expected_outcome="loads_with_render",
        expected_render_contains=("PARENT_PART + CHILD_PART",),
        notes="{{ super() }} additive composition — the only additive path kolsch has.",
    ),
)


# Sanity check at import time — fail loudly if a category subdir
# or fixture filename drifts.
_EXPECTED_IDS: tuple[str, ...] = (
    "A1", "A2",
    "B1", "B2", "B3", "B4",
    "C1", "C2", "C3", "C4", "C5",
    "D1", "D2", "D3",
    "E1", "E2",
    "F1", "F2", "F3", "F4", "F5",
    "G1", "G3",
    "H1", "H2",
    "I1",
    "K1", "K2",
)


def _validate_case_table() -> None:
    """Pin the corpus shape at import time.

    Fails fast on (a) missing Part-4 IDs, (b) duplicate IDs, or
    (c) a fixture filename that doesn't exist under
    ``tests/fixtures/extends/<cat>/``. Saves the runner from
    cryptic FileNotFoundError later.
    """
    seen = {c.case_id for c in CASES}
    missing = set(_EXPECTED_IDS) - seen
    extra = seen - set(_EXPECTED_IDS)
    if missing or extra:
        msg = (
            f"EXT-CORPUS shape drift: missing={sorted(missing)} "
            f"extra={sorted(extra)}"
        )
        raise RuntimeError(msg)
    if len(CASES) != len(seen):
        msg = "EXT-CORPUS has duplicate case_id entries"
        raise RuntimeError(msg)
    for case in CASES:
        cat_dir = FIXTURES_ROOT / case.category
        for src_name, _ in case.files:
            src_path = cat_dir / src_name
            if not src_path.is_file():
                msg = (
                    f"EXT-CORPUS fixture missing: {case.case_id} → "
                    f"{src_path}"
                )
                raise RuntimeError(msg)


_validate_case_table()

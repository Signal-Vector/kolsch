"""qol-088 — `run_workflow` full-definition validation pre-flight.

Covers the acceptance criteria of qol-088:

1. A definition-invalid workflow is rejected BEFORE opening the card
   / calling `services.workflows.start(...)` (no run, no card).
2. The raised `WrappedError` message ENUMERATES each validation
   sub-exception (node + location + problem + fix) — the LLM sees the
   specifics, not a bare "N sub-exceptions" count.
3. A valid workflow proceeds normally (same card/run behaviour as
   before qol-088).
4. Test doubles without `validate_workflow_definition` fall through
   to the pre-qol-088 no-validation path — the duck-typed seam keeps
   the `Workflows` Protocol byte-identical.
5. The observed `'nodes' is undefined` case (a bash node referencing
   `{{ nodes.X.Y }}` from a Jinja scope that has no `nodes`) is
   caught pre-flight if the validator sees it as invalid.

Exercises `run_workflow` against a mock `Workflows` service that
exposes the duck-typed `validate_workflow_definition` seam — no
ChatApp / Pilot needed. Mirrors the shape of the bug-025 tests in
``tests/bundles/kolsch/tools/test_run_workflow_input_validation.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest

from errors import PlatformError
from schemas.identifiers import AgentId, SessionId, ThreadId
from tests.fixtures.bundle_paths import STARTER_TOOLS_DIR, load_bundle_tool
from tools.decorator import ToolContext

# ----------------------------------------------------------------------
# Load the tool function from the bundle (matches other tests).
# ----------------------------------------------------------------------


def _load_run_workflow() -> Any:
    """Load the shipped ``run_workflow`` tool from the starter bundle."""
    return load_bundle_tool(STARTER_TOOLS_DIR, "run_workflow.py", "run_workflow")


# ----------------------------------------------------------------------
# Test doubles.
# ----------------------------------------------------------------------


class _MockCard:
    def __init__(self) -> None:
        self.id = "card_probe"
        self._owner_transferred = False
        self.is_closed = False


class _MockCards:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    async def open(self, label: str, *, kind: str) -> Any:
        self.opened.append((label, kind))
        return _MockCard()


class _MockWorkflows:
    """Mock `Workflows` service with the duck-typed qol-088 seam.

    - `resolve_workflow_yaml` — always returns the fixed YAML; ensures
      the pre-existing bug-025 input pre-flight passes on tests where
      inputs are fully supplied.
    - `validate_workflow_definition` — returns whatever the fixture
      requested (issue list or `None`); this is the qol-088 seam.
    - `start` — records the call so tests can assert it did NOT fire
      when the pre-flight rejected the workflow.
    """

    def __init__(
        self,
        *,
        yaml_source: str = "",
        validation_issues: list[str] | None = None,
    ) -> None:
        self._yaml_source = yaml_source
        self._validation_issues = validation_issues
        self.starts: list[tuple[str, dict[str, Any]]] = []
        self.validate_calls: list[str] = []

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        _ = ref
        return self._yaml_source or None

    def validate_workflow_definition(self, ref: str) -> list[str] | None:
        self.validate_calls.append(ref)
        return self._validation_issues

    async def start(
        self,
        ref: str,
        inputs: dict[str, Any],
        *,
        card: Any = None,
    ) -> str:
        _ = card
        self.starts.append((ref, inputs))
        return "wrk_probe"


def _mk_ctx(cards: Any, workflows: Any) -> ToolContext:
    return ToolContext(
        agent_id=AgentId(f"ag_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.test",
        secrets={},
        cards=cards,
        workflows=workflows,
    )


# ----------------------------------------------------------------------
# AC #1 — invalid workflow rejected pre-flight, no run/card.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_workflow_rejected_no_card_no_run() -> None:
    """A definition-invalid workflow must not open a card or start a run.

    Fail means a doomed run/card is still created — the exact bug the
    spec targets (ses_6e8e).
    """
    run_workflow = _load_run_workflow()
    cards = _MockCards()
    workflows = _MockWorkflows(
        validation_issues=[
            "<workflow>:0: edges[0].to references node 'ghost' which is not "
            "declared in 'nodes:'. Fix: add 'ghost' to 'nodes:' ...",
        ],
    )
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "acme/workflows/broken", "inputs": {}})
    with pytest.raises(PlatformError):
        await run_workflow._tool_definition.func(args, ctx)

    # The validate pre-flight was consulted, and it rejected the launch
    # BEFORE any card open / workflow start.
    assert workflows.validate_calls == ["acme/workflows/broken"]
    assert workflows.starts == []
    assert cards.opened == []


# ----------------------------------------------------------------------
# AC #2 — specific errors surfaced (not a bare "N sub-exceptions").
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_message_enumerates_each_validation_issue_messages() -> None:
    """The error `message` MUST contain each issue's specifics.

    Two distinct issues are surfaced; both must appear verbatim in the
    raised `WrappedError.message` (which the engine observer at
    `src/engine/observers.py:284` preserves as the LLM-facing
    `TOOL_FAILED` text). Fail means the message is still opaque.
    """
    run_workflow = _load_run_workflow()
    cards = _MockCards()
    issue_one = (
        "<workflow>:0: edges[0].to references node 'ghost' which is not "
        "declared in 'nodes:'."
    )
    issue_two = (
        "<workflow>:0: node 'target' has multiple inbound edges "
        "binding 'value' — sources: 'a', 'b'"
    )
    workflows = _MockWorkflows(
        validation_issues=[issue_one, issue_two],
    )
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "acme/workflows/broken", "inputs": {}})
    with pytest.raises(PlatformError) as exc_info:
        await run_workflow._tool_definition.func(args, ctx)
    text = str(exc_info.value)

    # Each specific issue is present — not just a count.
    assert issue_one in text, f"issue_one missing in error text: {text}"
    assert issue_two in text, f"issue_two missing in error text: {text}"
    # And the count phrasing is helpful, not a bare "N sub-exceptions".
    assert "2 issues" in text
    # The message names the ref so the LLM knows WHICH workflow to fix.
    assert "acme/workflows/broken" in text


# ----------------------------------------------------------------------
# AC #3 — valid workflow launches unchanged.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_workflow_launches_unchanged_valid() -> None:
    """A valid workflow (empty issue list) proceeds to card + start.

    Fail means the pre-flight gate has false positives.
    """
    run_workflow = _load_run_workflow()
    cards = _MockCards()
    workflows = _MockWorkflows(validation_issues=[])
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "acme/workflows/good", "inputs": {}})
    result = await run_workflow._tool_definition.func(args, ctx)
    assert result["run_id"] == "wrk_probe"
    assert workflows.starts == [("acme/workflows/good", {})]
    assert len(cards.opened) == 1


# ----------------------------------------------------------------------
# AC #4 — the observed `'nodes' is undefined` case (map-scope) is
# caught pre-flight via the real validator.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_worker_nodes_reference_caught_at_validate_nodes_undefined() -> None:
    """A map worker's bash `command:` referencing `nodes.X` fails pre-flight.

    Uses the REAL `validate_workflow` (via a mock service that shells
    out to it) so this test is a direct check that the validator
    catches the `'nodes' is undefined` case observed in session
    `ses_6e8e`. Fail means the most common observed error still
    escapes to run-time.
    """
    yaml_source = """
nodes:
  worker:
    type: kolsch/nodes/bash
    command: "echo {{ nodes.other.stdout }}"
  do_map:
    type: kolsch/nodes/map
    apply: worker
    over: [1, 2, 3]
    item_as: item
  other:
    type: kolsch/nodes/bash
    command: "echo hi"
graph:
  edges: []
"""

    from workflows.parser import load_workflow
    from workflows.validator import validate_workflow

    tmp_dir = Path(__file__).resolve().parent
    tmp = tmp_dir / "_qol088_map_scope_fixture.yaml"
    try:
        tmp.write_text(yaml_source, encoding="utf-8")
        workflow = load_workflow(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    class _StubCatalog:
        assets: ClassVar[dict[str, Any]] = {}

        def lookup(self, ref: str, *, expected_type: str | None = None) -> Any:
            _ = (ref, expected_type)
            return None

    result = validate_workflow(workflow, _StubCatalog(), file="<map-scope-fixture>")
    # The map worker's `command:` references `nodes.other.stdout`, but
    # a map worker has no graph predecessors (its only entry point is
    # the map.apply target), so the `nodes.other.*` reference MUST be
    # flagged by `_check_jinja_refs`.
    assert result.errors, (
        "expected validate_workflow to catch the map worker's "
        "nodes.X.Y reference (worker has no predecessors)"
    )
    joined = "\n".join(str(e) for e in result.errors)
    # The finding should mention the offending worker + reference.
    assert "worker" in joined or "nodes.other" in joined, (
        f"expected the validator to name 'worker' or 'nodes.other' — got: {joined}"
    )


# ----------------------------------------------------------------------
# AC #5 — headless test doubles without the seam fall through.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_absent_falls_back_to_pre_qol088_shape() -> None:
    """Mocks without `validate_workflow_definition` skip the gate.

    Backward-compat: headless test mocks that predate qol-088 keep
    working — the duck-typed probe returns `None` and the tool
    proceeds to enqueue as before. Matches the bug-025 fallback test.
    """

    class _MinimalWorkflows:
        def __init__(self) -> None:
            self.starts: list[tuple[str, dict[str, Any]]] = []

        async def start(
            self,
            ref: str,
            inputs: dict[str, Any],
            *,
            card: Any = None,
        ) -> str:
            _ = card
            self.starts.append((ref, inputs))
            return "wrk_headless"

    run_workflow = _load_run_workflow()
    cards = _MockCards()
    workflows = _MinimalWorkflows()
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "acme/workflows/good", "inputs": {}})
    result = await run_workflow._tool_definition.func(args, ctx)
    assert result["run_id"] == "wrk_headless"
    assert workflows.starts == [("acme/workflows/good", {})]

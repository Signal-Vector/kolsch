# Workflow golden fixtures — Phase 3.0 Step 7

Each `*.yaml` file here is a self-contained workflow fixture that
`tests/golden/workflows_test.py` discovers, parses, and asserts against
its declared outcome.

## File naming

- `valid-<scenario>.yaml` — parses + validates cleanly.
- `invalid-<scenario>.yaml` — parses but the validator raises (or, in
  R3 #5 rejection cases, the parser raises before validation runs).
- `warning-<scenario>.yaml` — validates cleanly but produces at least
  one :class:`workflows.errors.WorkflowValidationWarning`.

## Top-of-file annotation

Each fixture begins with one or more YAML comments that declare the
expected outcome. The discovery test parses these comments to wire
the assertion:

| Annotation | Meaning |
| --- | --- |
| `# expected: valid` | `validate_workflow_or_raise` returns `None`; `result.warnings == ()`. |
| `# expected_error: <ClassName>` | `validate_workflow_or_raise` raises that class. |
| `# expected_error_field: <YAML path>` | (optional) Asserts the raised exception's `identifying_names` field includes this path. |
| `# expected_errors: <C1>, <C2>, …` | Multi-error accumulation — assertion is `ExceptionGroup` and every named class appears in `exc.exceptions`. |
| `# expected_warning: <ClassName>` | The validator's `ValidationResult.warnings` tuple contains that class. |

Exactly one of `expected:`, `expected_error:`, `expected_errors:`, or
`expected_warning:` MUST appear in every fixture. The discovery test
fails loudly if a fixture lacks an annotation — that defends against
the silent-coverage hole where a new fixture lands without asserting
anything.

## Conventions enforced by the discovery test

- Every YAML-origin error message is strict single-line and contains
  the `<file>:<line>` token plus the literal `"Fix: "` token (per
  Decision 9 + master spec §12.3).
- Strict-no-code in the Fix portion (Decision P4) — no `{{`, `{%`, or
  backticks survive in the error string.

## Adding a new fixture

1. Choose a name following the convention above.
2. Add the top-of-file annotation comment(s).
3. Author the YAML.
4. Run `uv run pytest tests/golden/workflows_test.py -k <name>` to
   verify it asserts what you intended.

## Related test helpers

- `tests/workflows/conftest.py:make_workflow(**overrides)` builds an
  in-memory :class:`workflows.parser.Workflow` for tests that do not
  need a file fixture.
- `workflows.NodeContext.for_test(**overrides)` is the canonical entry
  point for tests that construct a :class:`workflows.NodeContext`
  (Round 7 A7-1).

## The `_targets/` subdirectory

Some fixtures need a sibling on-disk YAML that the validator parses
through the catalog (the test driver registers the path into the
seed catalog). Those targets live under `_targets/` so the
discovery glob `FIXTURE_DIR.glob("*.yaml")` (non-recursive) skips
them. Today this houses:

- `_targets/extends-policy-target.yaml` — the parent that
  `invalid-extends-policy-forbidden.yaml` extends. Declares a
  non-empty `policy:` block so the validator's
  `_check_extends_policy` walk can raise on it.

## Coverage notes — classes covered elsewhere

Step 7 AC 7.2 wants every `WorkflowValidationError` subclass to have
a `*.yaml` fixture. A handful of classes either don't fire from a
pure-YAML validator pass (their trigger surface lives in catalog
scan, runtime execution, or Phase 3.1 resolvers) or are covered by
dedicated Python tests outside the golden suite:

| Class | Coverage location | Why not a YAML fixture |
| --- | --- | --- |
| `WorkflowBuiltinShadowed` | `tests/component/test_workflow_catalog.py` | Fires at `AssetCatalog.scan()` time (Step 3), not during workflow YAML validation. |
| `WorkflowJinjaSecurityError` | `tests/security/test_jinja_sandbox_escape.py` | Fires at Jinja **render** time. The validator only parses the AST; the sandbox doesn't engage until the Phase 3.1 executor renders values. |
| `WorkflowBindTypeMismatch` | (Phase 3.1) | Runtime-only — fires when a rendered bind value fails the receiver's Pydantic strict check at node-fire time. |
| `WorkflowExtendsCycle` / `WorkflowExtendsDepthExceeded` / `WorkflowExtendsUnresolved` | (Phase 3.1) | The extends resolver lives in the Phase 3.1 engine; Phase 3.0's `_check_extends_policy` covers only the policy-shadowing case. |
| `WorkflowCallCycle` | (Phase 3.1) | Sub-workflow call-graph cycle detection lives in the engine. |
| `WorkflowNodeImportFailed` / `WorkflowNodeMissingRun` / `WorkflowNodeNotDecorated` / `WorkflowNodeSignatureInvalid` / `WorkflowNodeReturnTypeInvalid` | `tests/component/test_workflow_validator.py::TestCatalogNodeDefIntrospection` | These §7.6 classes need an authored `.py` file alongside the YAML; the Python tests construct fixtures inline via `tmp_path`. |
| `WorkflowAmbiguousStart` / `WorkflowNoStartNode` / `WorkflowDuplicateNode` / `WorkflowDuplicateEdge` / `WorkflowMapperWorkerMissingField` | (Phase 3.1) | The Phase 3.0 validator gates the structural checks it implements today (DAG via reachability, bind-key collision, branch arms, etc.); these specific shapes weren't required to ship at v1. Pydantic dict-key uniqueness covers a chunk of `WorkflowDuplicateNode` automatically. |

When any of the above classes graduate to a YAML fixture, add a row
to the table above and remove it from the deferred set.

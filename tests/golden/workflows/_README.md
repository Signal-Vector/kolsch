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

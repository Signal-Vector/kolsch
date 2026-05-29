"""End-to-end tests — TC-E2E-01..04.

Whole platform, real infrastructure, real LLM where applicable. Slow,
few, deliberate (handbook §8.1). NOT in the default gate; run via
``pytest -m e2e``.

CI runs E2E on a separate (nightly / pre-release) schedule per
``docs/process/test-catalog.md`` Part 13.
"""

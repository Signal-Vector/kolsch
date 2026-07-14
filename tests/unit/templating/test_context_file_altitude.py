"""Guard test: shipped agents must not mark oversized docs as ``primary: true``.

Every ``primary: true`` context file is inlined into the system prompt
on every LLM call.  This test enumerates the agent configs shipped in
the starter bundle and asserts that no primary file exceeds
**PRIMARY_MAX_BYTES** (~8 KB).  The threshold is chosen so
``kolsch:docs/index.md`` (~5 KB) passes comfortably while a file like
``AGENTS.md`` (~16 KB) fails — catching the regression from qol-070
where the contributor guide was incorrectly marked primary.

Run: ``uv run pytest tests/unit/templating/test_context_file_altitude.py -q``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from templating.context_files import resolve_path

# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

# 8 KB — docs/index.md (~5 KB) passes; AGENTS.md (~16 KB) fails.
# Large, reference-style docs belong at primary: false (the "Also
# available" secondary tier) so the agent reads them on demand via
# read_file.  Small, always-relevant docs (an index, hard rules) may
# be primary.
PRIMARY_MAX_BYTES: int = 8 * 1024

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STARTER_AGENTS_DIR = REPO_ROOT / "src" / "cli" / "_starter_bundle" / "kolsch" / "agents"


def _iter_shipped_agent_yamls() -> list[Path]:
    """Return every ``*.yaml`` under ``src/cli/_starter_bundle/**/agents/``."""
    if not STARTER_AGENTS_DIR.is_dir():
        return []
    return sorted(STARTER_AGENTS_DIR.glob("*.yaml"))


def _parse_context_files(yaml_path: Path) -> list[dict[str, Any]]:
    """Parse a shipped agent YAML and return its ``context_files`` list."""
    with yaml_path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}
    raw = data.get("context_files", [])
    return raw if isinstance(raw, list) else []


def _check_primary_sizes(
    yaml_path: Path,
) -> list[tuple[str, str, int]]:
    """Return list of (yaml_relpath, context_path, size) for oversized primaries."""
    violations: list[tuple[str, str, int]] = []
    context_files = _parse_context_files(yaml_path)
    for entry in context_files:
        if not entry.get("primary", False):
            continue
        file_path: str = entry["path"]
        resolved = resolve_path(file_path, REPO_ROOT)
        if not resolved.is_file():
            continue
        size = resolved.stat().st_size
        if size > PRIMARY_MAX_BYTES:
            violations.append((str(yaml_path.relative_to(REPO_ROOT)), file_path, size))
    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoOversizedPrimaryInShippedAgents:
    """Guard: no shipped agent marks a >8 KB doc as ``primary: true``."""

    def test_no_oversized_primary_in_shipped_agents(self) -> None:
        """Enumerate starter-bundle agents; fail if any primary exceeds threshold."""
        violations: list[tuple[str, str, int]] = []

        for agent_yaml in _iter_shipped_agent_yamls():
            violations.extend(_check_primary_sizes(agent_yaml))

        assert not violations, (
            "Oversized primary context files found "
            f"(threshold={PRIMARY_MAX_BYTES} bytes):\n"
            + "\n".join(
                f"  {yaml_path}: {path} = {size:,} bytes"
                for yaml_path, path, size in violations
            )
        )

    def test_oversized_primary_guard_catches_violation(self) -> None:
        """The guard would fail for an agent that marks AGENTS.md primary."""
        agents_md = REPO_ROOT / "AGENTS.md"
        assert agents_md.is_file(), "AGENTS.md should exist for this test"
        size = agents_md.stat().st_size
        assert size > PRIMARY_MAX_BYTES, (
            f"AGENTS.md ({size:,} bytes) should exceed threshold "
            f"({PRIMARY_MAX_BYTES} bytes) to validate the guard"
        )

    def test_helper_has_no_agents_md_context_file(self) -> None:
        """The helper agent no longer lists AGENTS.md in context_files."""
        helper_yaml = STARTER_AGENTS_DIR / "helper.yaml"
        assert helper_yaml.is_file(), "helper.yaml should exist in starter bundle"

        context_files = _parse_context_files(helper_yaml)
        primary_paths = [e["path"] for e in context_files if e.get("primary", False)]

        assert "kolsch:AGENTS.md" not in primary_paths, (
            "helper.yaml must not have kolsch:AGENTS.md as a primary context file"
        )
        # Ensure docs/index.md is still primary
        assert "kolsch:docs/index.md" in primary_paths, (
            "helper.yaml must still have kolsch:docs/index.md as primary"
        )

    def test_index_doc_passes_threshold(self) -> None:
        """docs/index.md is small enough to be primary."""
        index_md = REPO_ROOT / "docs" / "index.md"
        assert index_md.is_file(), "docs/index.md should exist"
        size = index_md.stat().st_size
        assert size <= PRIMARY_MAX_BYTES, (
            f"docs/index.md ({size:,} bytes) exceeds threshold "
            f"({PRIMARY_MAX_BYTES} bytes) — consider raising the threshold "
            f"or splitting the doc"
        )


class TestHelperAltitude:
    """qol-070-specific: helper agent context-file hygiene."""

    def test_helper_context_files_have_no_oversized_primary(self) -> None:
        """The helper agent's primary context files all fit within threshold."""
        helper_yaml = STARTER_AGENTS_DIR / "helper.yaml"
        violations = _check_primary_sizes(helper_yaml)
        assert not violations, (
            "helper.yaml has oversized primary context files:\n"
            + "\n".join(
                f"  {path} = {size:,} bytes (max {PRIMARY_MAX_BYTES:,})"
                for _, path, size in violations
            )
        )

"""Unit tests for the `compaction` section on `platform.yaml` — feat-054 Phase 1.

Two contracts:

1. The loader recognises `compaction` as a known section (no warning /
   error path).
2. Section-absent semantics are silent — bootstrap and downstream code
   see an empty mapping via `get_platform_config` KeyError, and the
   default payload does NOT write a `compaction:` block (matches the
   `author_workflow` precedent — leaving the section absent IS the
   documented "off" mode).
"""

from __future__ import annotations

from assets.platform_config import (
    _KNOWN_SECTIONS,
    default_platform_payload,
    is_known_section,
)


def test_compaction_is_recognised_section() -> None:
    assert "compaction" in _KNOWN_SECTIONS


def test_is_known_section_accepts_compaction() -> None:
    assert is_known_section("compaction") is True


def test_default_payload_omits_compaction_section() -> None:
    payload = default_platform_payload()
    # Section-absent = disabled. The default payload MUST NOT
    # write a `compaction:` block; otherwise every fresh workspace
    # would opt in silently.
    assert "compaction" not in payload

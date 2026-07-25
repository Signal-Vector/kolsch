"""qol-089 — pure-function tests for ``cli._migrations.tools``.

Covers:

- Rewrite of ``bundles/kolsch/agents/*.yaml`` — ``declared[].ref``
  entries whose value is in the migrator manifest are renamed (with
  ``also`` companions appended) or removed (``Retired``).
- Rewrite of ``platform.yaml`` — ``authorization.config.rules[].what``
  entries follow the same rules; companion rules clone the primary
  rule's shape (pattern, effect).
- ``bundles/<other>/**/*.yaml`` scanning — hits are *reported*, never
  rewritten.
- YAML round-trip fidelity — comments and key order in the source
  files are preserved through ``ruamel.yaml``.
- The migrator is pure — no filesystem writes beyond returning the
  new YAML text via ``ToolsMigrationResult.yaml_bodies``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cli._migrations.tools import (
    Renamed,
    Retired,
    ToolsMigrationResult,
    migrate_tool_refs,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_HELPER_YAML = """\
schema_version: "0.1"
name: helper
description: A friendly helper agent.

# Tool declarations — the model can call any tool listed here.
subsystems:
  tools:
    variant: standard
    config:
      declared:
        - ref: kolsch/tools/read_file
        - ref: kolsch/tools/write_file
        # fetch_website was split in qol-fetch-url-tool.
        - ref: kolsch/tools/fetch_website
        - ref: kolsch/tools/bash
"""


_PLATFORM_YAML = """\
# Comment at the top — must survive the round-trip.
authorization:
  variant: rule_based
  config:
    rules:
      # Allow the helper to read files everywhere.
      - what: kolsch/tools/read_file
        pattern: "*"
        effect: allow
      - what: kolsch/tools/fetch_website
        pattern: "*"
        effect: allow

llm_providers:
  opencode:
    type: opencode
    api_key_secret: OPENCODE_API_KEY
"""


_USER_OWNED_WORKFLOW = """\
# bundles/autoply/workflows/job.yaml
nodes:
  fetch:
    kind: agent
    config:
      policy:
        allow:
          - kolsch/tools/fetch_website
"""


def _seed(root: Path) -> None:
    """Materialize the qol-089 fixture workspace under ``root``."""
    (root / "bundles" / "kolsch" / "agents").mkdir(parents=True)
    (root / "bundles" / "kolsch" / "agents" / "helper.yaml").write_text(
        _HELPER_YAML, encoding="utf-8"
    )
    (root / "platform.yaml").write_text(_PLATFORM_YAML, encoding="utf-8")
    (root / "bundles" / "autoply" / "workflows").mkdir(parents=True)
    (root / "bundles" / "autoply" / "workflows" / "job.yaml").write_text(
        _USER_OWNED_WORKFLOW, encoding="utf-8"
    )


def _default_manifest() -> dict[str, Renamed | Retired]:
    return {
        "kolsch/tools/fetch_website": Renamed(
            to="kolsch/tools/fetch_url",
            also=("kolsch/tools/browse_url",),
            notes="Split into fetch_url + browse_url.",
        ),
    }


# ---------------------------------------------------------------------------
# Test — helper agent rewrite (Test 6)
# ---------------------------------------------------------------------------


def test_helper_agent_ref_rewritten_with_companion(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = migrate_tool_refs(tmp_path, _default_manifest())

    helper_rewrites = [
        r for r in result.rewrites if r.kind == "agent_declared" and "helper" in r.path
    ]
    assert helper_rewrites, "expected an agent-declared rewrite for helper.yaml"
    rewrite = helper_rewrites[0]
    assert rewrite.old_ref == "kolsch/tools/fetch_website"
    assert rewrite.new_ref == "kolsch/tools/fetch_url"
    assert rewrite.added == ("kolsch/tools/browse_url",)

    rel = "bundles/kolsch/agents/helper.yaml"
    body = result.yaml_bodies.get(rel)
    assert body is not None, "helper.yaml body must be in yaml_bodies"
    assert "kolsch/tools/fetch_website" not in body
    assert "kolsch/tools/fetch_url" in body
    assert "kolsch/tools/browse_url" in body


def test_platform_yaml_authz_rewrite_with_companion(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = migrate_tool_refs(tmp_path, _default_manifest())

    authz_rewrites = [r for r in result.rewrites if r.kind == "authz_rule"]
    assert authz_rewrites, "expected an authz-rule rewrite"
    rewrite = authz_rewrites[0]
    assert rewrite.old_ref == "kolsch/tools/fetch_website"
    assert rewrite.new_ref == "kolsch/tools/fetch_url"
    assert rewrite.added == ("kolsch/tools/browse_url",)

    body = result.yaml_bodies.get("platform.yaml")
    assert body is not None, "platform.yaml body must be in yaml_bodies"
    assert "kolsch/tools/fetch_website" not in body
    assert "kolsch/tools/fetch_url" in body
    assert "kolsch/tools/browse_url" in body


# ---------------------------------------------------------------------------
# Test — Retired action removes the ref
# ---------------------------------------------------------------------------


def test_retired_action_removes_ref(tmp_path: Path) -> None:
    _seed(tmp_path)
    manifest: dict[str, Renamed | Retired] = {
        "kolsch/tools/fetch_website": Retired(
            reason="Replaced by browse_url + fetch_url; no direct 1:1 substitute.",
            replacement="kolsch/tools/browse_url",
        ),
    }
    result = migrate_tool_refs(tmp_path, manifest)

    helper_rewrites = [
        r for r in result.rewrites if r.kind == "agent_declared" and "helper" in r.path
    ]
    assert helper_rewrites and helper_rewrites[0].new_ref is None
    body = result.yaml_bodies["bundles/kolsch/agents/helper.yaml"]
    assert "kolsch/tools/fetch_website" not in body


# ---------------------------------------------------------------------------
# Test — user-owned files are reported, never rewritten (Test 3, unit-side)
# ---------------------------------------------------------------------------


def test_user_owned_refs_are_reported_only(tmp_path: Path) -> None:
    _seed(tmp_path)
    autoply_path = tmp_path / "bundles" / "autoply" / "workflows" / "job.yaml"
    pre = autoply_path.read_text(encoding="utf-8")

    result = migrate_tool_refs(tmp_path, _default_manifest())

    # File on disk MUST be untouched (migrator is a pure function).
    assert autoply_path.read_text(encoding="utf-8") == pre
    # And no autoply path is in yaml_bodies.
    assert not any("autoply" in k for k in result.yaml_bodies)
    # But the hit was reported.
    autoply_hits = [h for h in result.user_owned_hits if "autoply" in h.path]
    assert autoply_hits, "expected user-owned hit for autoply job.yaml"
    hit = autoply_hits[0]
    assert hit.old_ref == "kolsch/tools/fetch_website"
    assert hit.suggested_new_ref == "kolsch/tools/fetch_url"
    assert "fetch_url" in hit.notes or "browse_url" in hit.notes


# ---------------------------------------------------------------------------
# Test — YAML round-trip preserves comments + key order (Test 7)
# ---------------------------------------------------------------------------


def test_yaml_roundtrip_preserves_comments(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = migrate_tool_refs(tmp_path, _default_manifest())

    helper_body = result.yaml_bodies["bundles/kolsch/agents/helper.yaml"]
    # Comment blocks in the input MUST survive.
    assert "# Tool declarations" in helper_body
    assert "# fetch_website was split" in helper_body
    # Non-touched refs retain position (read_file appears before write_file).
    read_i = helper_body.index("kolsch/tools/read_file")
    write_i = helper_body.index("kolsch/tools/write_file")
    bash_i = helper_body.index("kolsch/tools/bash")
    assert read_i < write_i < bash_i

    platform_body = result.yaml_bodies["platform.yaml"]
    assert "# Comment at the top" in platform_body
    assert "# Allow the helper to read files everywhere." in platform_body
    # llm_providers block must survive verbatim.
    assert "opencode:" in platform_body
    assert "api_key_secret: OPENCODE_API_KEY" in platform_body


# ---------------------------------------------------------------------------
# Test — migrator writes nothing (Test 6 invariant)
# ---------------------------------------------------------------------------


def test_migrator_does_not_write_files(tmp_path: Path) -> None:
    _seed(tmp_path)
    helper_path = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    platform_path = tmp_path / "platform.yaml"
    autoply_path = tmp_path / "bundles" / "autoply" / "workflows" / "job.yaml"
    pre = {
        helper_path: helper_path.read_text(encoding="utf-8"),
        platform_path: platform_path.read_text(encoding="utf-8"),
        autoply_path: autoply_path.read_text(encoding="utf-8"),
    }

    result = migrate_tool_refs(tmp_path, _default_manifest())
    assert isinstance(result, ToolsMigrationResult)

    # Every file on disk is byte-identical to before the call.
    for path, before in pre.items():
        assert path.read_text(encoding="utf-8") == before, f"{path} was mutated on disk"


# ---------------------------------------------------------------------------
# Test — clean workspace yields an empty result
# ---------------------------------------------------------------------------


def test_clean_workspace_yields_empty_result(tmp_path: Path) -> None:
    (tmp_path / "bundles" / "kolsch" / "agents").mkdir(parents=True)
    (tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml").write_text(
        "name: helper\nsubsystems:\n  tools:\n    config:\n      declared:\n"
        "        - ref: kolsch/tools/read_file\n",
        encoding="utf-8",
    )
    result = migrate_tool_refs(tmp_path, _default_manifest())
    assert result.clean()
    assert not result.rewrites
    assert not result.yaml_bodies
    assert not result.user_owned_hits


# ---------------------------------------------------------------------------
# Test — companion already declared is not duplicated
# ---------------------------------------------------------------------------


def test_companion_not_duplicated_if_already_present(tmp_path: Path) -> None:
    (tmp_path / "bundles" / "kolsch" / "agents").mkdir(parents=True)
    (tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml").write_text(
        "name: helper\nsubsystems:\n  tools:\n    config:\n      declared:\n"
        "        - ref: kolsch/tools/fetch_website\n"
        "        - ref: kolsch/tools/browse_url\n",
        encoding="utf-8",
    )
    result = migrate_tool_refs(tmp_path, _default_manifest())
    body = result.yaml_bodies["bundles/kolsch/agents/helper.yaml"]
    # browse_url was already declared → should appear exactly ONCE.
    assert body.count("kolsch/tools/browse_url") == 1
    # fetch_website replaced by fetch_url.
    assert "kolsch/tools/fetch_website" not in body
    assert body.count("kolsch/tools/fetch_url") == 1


# ---------------------------------------------------------------------------
# Test — rename primary already declared → drop the old entry (dedup)
# ---------------------------------------------------------------------------


def test_rename_primary_already_declared_drops_old(tmp_path: Path) -> None:
    (tmp_path / "bundles" / "kolsch" / "agents").mkdir(parents=True)
    (tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml").write_text(
        "name: helper\nsubsystems:\n  tools:\n    config:\n      declared:\n"
        "        - ref: kolsch/tools/fetch_url\n"
        "        - ref: kolsch/tools/fetch_website\n",
        encoding="utf-8",
    )
    result = migrate_tool_refs(tmp_path, _default_manifest())
    body = result.yaml_bodies["bundles/kolsch/agents/helper.yaml"]
    # fetch_website's target (fetch_url) was already declared — the
    # redundant post-rename entry gets dropped.
    assert body.count("kolsch/tools/fetch_url") == 1
    assert "kolsch/tools/fetch_website" not in body
    # Companion still added.
    assert "kolsch/tools/browse_url" in body

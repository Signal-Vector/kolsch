"""Unit tests for the `hooks` section on `platform.yaml` — spec-131 Step 1.

Covers config schema + loading only (no matcher, no subprocess
execution, no engine wiring — those land in later spec-131 steps).
Three contracts:

1. The loader recognises `hooks` as a known section (no warning /
   error path), matching the `compaction`/`author_workflow`
   "absent = off" convention — see `test_compaction_section.py`.
2. `parse_hooks_section` turns a raw `hooks:` list into validated
   `HookRule` objects, and fails fast with a clear `ConfigInvalid` on
   a missing required field or an unknown extra key.
3. `_validate_section_shape` (invoked from `load_or_create` /
   `_load_existing`) runs `hooks:` list validation as part of the
   normal `platform.yaml` load path, in addition to the special-cased
   "hooks is a list, not a mapping" shape carve-out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from assets.platform_config import (
    _KNOWN_SECTIONS,
    _validate_section_shape,
    default_platform_payload,
    is_known_section,
    load_or_create,
    parse_hooks_section,
)
from errors import ConfigInvalid
from schemas import HookRule

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Known-section / absent-section semantics (mirrors test_compaction_section.py)
# ---------------------------------------------------------------------------


def test_hooks_is_recognised_section() -> None:
    assert "hooks" in _KNOWN_SECTIONS


def test_is_known_section_accepts_hooks() -> None:
    assert is_known_section("hooks") is True


def test_default_payload_omits_hooks_section() -> None:
    payload = default_platform_payload()
    # Section-absent = zero hooks, zero overhead. The default payload
    # MUST NOT write a `hooks:` block; otherwise every fresh workspace
    # would opt in silently.
    assert "hooks" not in payload


# ---------------------------------------------------------------------------
# parse_hooks_section — valid parse
# ---------------------------------------------------------------------------


def test_parse_hooks_section_valid_list() -> None:
    raw = [
        {
            "event": "kolsch.prompt.requested",
            "matcher": "bash",
            "command": "terminal-notifier -message 'approval needed'",
            "timeout_seconds": 5,
        },
        {
            "event": "thread.exchange_completed",
            "command": "curl -X POST https://example.com/hook",
        },
    ]
    rules = parse_hooks_section(raw)

    assert rules == [
        HookRule(
            event="kolsch.prompt.requested",
            matcher="bash",
            command="terminal-notifier -message 'approval needed'",
            timeout_seconds=5,
        ),
        HookRule(
            event="thread.exchange_completed",
            command="curl -X POST https://example.com/hook",
        ),
    ]
    # Defaults apply when omitted.
    assert rules[1].matcher == "*"
    assert rules[1].timeout_seconds == 10


def test_parse_hooks_section_empty_list_returns_empty() -> None:
    assert parse_hooks_section([]) == []


def test_parse_hooks_section_rejects_non_list() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        parse_hooks_section({"event": "kolsch.prompt.requested"})

    errors = exc_info.value.detail["validation_errors"]
    assert errors
    assert errors[0]["code"] == "not_a_list"
    assert errors[0]["path"] == "/hooks"


# ---------------------------------------------------------------------------
# parse_hooks_section — malformed entries fail fast, with a clear error
# ---------------------------------------------------------------------------


def test_parse_hooks_section_missing_event_raises_clear_error() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        parse_hooks_section([{"command": "touch /tmp/kolsch-hook-test"}])

    errors = exc_info.value.detail["validation_errors"]
    assert errors
    assert errors[0]["code"] == "hook_rule_invalid"
    assert errors[0]["path"] == "/hooks/0"
    assert "event" in errors[0]["message"]


def test_parse_hooks_section_missing_command_raises_clear_error() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        parse_hooks_section([{"event": "kolsch.prompt.requested"}])

    errors = exc_info.value.detail["validation_errors"]
    assert errors
    assert errors[0]["code"] == "hook_rule_invalid"
    assert errors[0]["path"] == "/hooks/0"
    assert "command" in errors[0]["message"]


def test_parse_hooks_section_unknown_key_raises_clear_error() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        parse_hooks_section(
            [
                {
                    "event": "kolsch.prompt.requested",
                    "command": "touch /tmp/kolsch-hook-test",
                    "not_a_real_field": "oops",
                }
            ]
        )

    errors = exc_info.value.detail["validation_errors"]
    assert errors
    assert errors[0]["code"] == "hook_rule_invalid"
    assert "not_a_real_field" in errors[0]["message"]


def test_parse_hooks_section_timeout_over_ceiling_raises() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        parse_hooks_section(
            [
                {
                    "event": "kolsch.prompt.requested",
                    "command": "touch /tmp/kolsch-hook-test",
                    "timeout_seconds": 61,
                }
            ]
        )

    errors = exc_info.value.detail["validation_errors"]
    assert errors
    assert errors[0]["code"] == "hook_rule_invalid"


def test_parse_hooks_section_entry_not_a_mapping_raises() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        parse_hooks_section(["not-a-mapping"])

    errors = exc_info.value.detail["validation_errors"]
    assert errors
    assert errors[0]["code"] == "hook_rule_invalid"
    assert errors[0]["path"] == "/hooks/0"


def test_parse_hooks_section_reports_every_bad_entry() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        parse_hooks_section(
            [
                {"command": "touch /tmp/a"},  # missing event
                {"event": "kolsch.prompt.requested"},  # missing command
            ]
        )

    errors = exc_info.value.detail["validation_errors"]
    assert [e["path"] for e in errors] == ["/hooks/0", "/hooks/1"]


# ---------------------------------------------------------------------------
# HookRule schema — field defaults, in isolation from the loader
# ---------------------------------------------------------------------------


def test_hook_rule_defaults() -> None:
    rule = HookRule(event="kolsch.prompt.requested", command="echo hi")
    assert rule.matcher == "*"
    assert rule.timeout_seconds == 10


def test_hook_rule_is_frozen() -> None:
    rule = HookRule(event="kolsch.prompt.requested", command="echo hi")
    with pytest.raises(Exception, match=r"frozen|immutable"):
        rule.command = "echo bye"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _validate_section_shape — hooks list carve-out + integrated validation
# ---------------------------------------------------------------------------


def test_validate_section_shape_accepts_valid_hooks_list() -> None:
    # Should not raise: hooks is a list (not a mapping, unlike every
    # other section), and its entries are individually valid.
    _validate_section_shape(
        {
            "hooks": [
                {"event": "session.opened", "command": "touch /tmp/kolsch-sentinel"},
            ]
        }
    )


def test_validate_section_shape_rejects_malformed_hooks_entry() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        _validate_section_shape({"hooks": [{"matcher": "*"}]})

    errors = exc_info.value.detail["validation_errors"]
    assert errors
    assert errors[0]["code"] == "hook_rule_invalid"


def test_validate_section_shape_absent_hooks_key_no_error() -> None:
    # No `hooks` key at all: no error, and parse_hooks_section is
    # never invoked (there's nothing to validate).
    _validate_section_shape({"secrets": {"variant": "dotenv", "config": {}}})


# ---------------------------------------------------------------------------
# End-to-end through the real file loader (load_or_create)
# ---------------------------------------------------------------------------


def test_load_or_create_accepts_real_platform_yaml_with_hooks(
    tmp_path: Path,
) -> None:
    (tmp_path / "platform.yaml").write_text(
        "hooks:\n"
        "  - event: kolsch.prompt.requested\n"
        "    command: \"terminal-notifier -message 'approval needed'\"\n"
        "  - event: tool.failed\n"
        "    matcher: bash\n"
        "    command: echo failed\n"
        "    timeout_seconds: 30\n"
    )

    payload = load_or_create(tmp_path)

    rules = parse_hooks_section(payload["hooks"])
    assert len(rules) == 2
    assert rules[0].event == "kolsch.prompt.requested"
    assert rules[0].matcher == "*"
    assert rules[1].matcher == "bash"
    assert rules[1].timeout_seconds == 30


def test_load_or_create_rejects_malformed_hooks_in_real_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "platform.yaml").write_text(
        "hooks:\n  - matcher: '*'\n    command: echo hi\n"  # missing `event`
    )

    with pytest.raises(ConfigInvalid) as exc_info:
        load_or_create(tmp_path)

    errors = exc_info.value.detail["validation_errors"]
    assert errors
    assert errors[0]["code"] == "hook_rule_invalid"


def test_load_or_create_absent_hooks_key_produces_no_hooks(
    tmp_path: Path,
) -> None:
    (tmp_path / "platform.yaml").write_text(
        "secrets:\n  variant: dotenv\n  config: {}\n"
    )

    payload = load_or_create(tmp_path)

    assert "hooks" not in payload

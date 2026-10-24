"""`.kolsch/default-rules.json` — starter-rule decisions per workspace (bug-146).

`kolsch setup --sync-bundle` offers a missing default rule at most once
per workspace; this ledger is how it remembers. It is workspace config
metadata, like `.kolsch/bundle-baseline.json`: the runtime never reads
it, and deleting it only means the still-missing defaults are offered
again.

Schema v1:

.. code-block:: json

    {
      "schema_version": 1,
      "rules": {
        "jev_decide": {
          "decision": "added",
          "at": "2026-10-24T12:00:00+00:00",
          "kolsch_version": "0.1.0"
        }
      }
    }

Decisions: `installed` (written by a fresh `kolsch setup`, or the
legacy allow-all migration), `added` (accepted on upgrade), `declined`
(skipped on upgrade), `present` (the user already had a rule for that
target). A missing, unreadable or malformed ledger reads as empty —
"offer again", never a crash. Writes are atomic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from cli._bundle_baseline import _current_kolsch_version, _write_atomic

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

Decision = Literal["installed", "added", "declined", "present"]
_DECISIONS: frozenset[str] = frozenset({"installed", "added", "declined", "present"})
_LEDGER_REL = ".kolsch/default-rules.json"
_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One recorded decision."""

    decision: Decision
    at: str
    kolsch_version: str


def ledger_path(workspace_root: Path) -> Path:
    """Return the absolute path to the workspace's default-rules ledger."""
    return workspace_root / _LEDGER_REL


def read_ledger(workspace_root: Path) -> dict[str, LedgerEntry]:
    """Return the recorded decisions by manifest key; `{}` when absent or corrupt."""
    try:
        raw = json.loads(ledger_path(workspace_root).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        return {}
    rules = raw.get("rules")
    if not isinstance(rules, dict):
        return {}
    out: dict[str, LedgerEntry] = {}
    for key, value in rules.items():
        if not isinstance(value, dict):
            continue
        decision = value.get("decision")
        if decision not in _DECISIONS:
            continue
        out[str(key)] = LedgerEntry(
            decision=decision,
            at=str(value.get("at", "")),
            kolsch_version=str(value.get("kolsch_version", "")),
        )
    return out


def record_decisions(workspace_root: Path, decisions: Mapping[str, Decision]) -> None:
    """Merge `decisions` into the ledger (atomic write; creates it if absent).

    Existing entries for other keys are kept; a key in `decisions`
    replaces its entry. No-op for an empty mapping.
    """
    if not decisions:
        return
    entries = read_ledger(workspace_root)
    stamp = datetime.now(UTC).isoformat()
    version = _current_kolsch_version()
    for key, decision in decisions.items():
        entries[key] = LedgerEntry(decision=decision, at=stamp, kolsch_version=version)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "rules": {
            key: {
                "decision": entry.decision,
                "at": entry.at,
                "kolsch_version": entry.kolsch_version,
            }
            for key, entry in sorted(entries.items())
        },
    }
    _write_atomic(
        ledger_path(workspace_root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def record_installed(workspace_root: Path, keys: Iterable[str]) -> None:
    """Record every key in `keys` as `installed` (fresh install / full reset)."""
    record_decisions(workspace_root, dict.fromkeys(keys, "installed"))

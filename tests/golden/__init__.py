"""Golden event-sequence tests — handbook §8.4.

Each `.jsonl` here is a normalized event log: one event per line,
JSON-serialized with sorted keys + minimal separators (matching the
byte-stable JSONL contract from Step 4 #7). UUIDs are replaced with
per-prefix sequential ids (``ev_001``, ``cl_001``, etc) and timestamps
with the sentinel ``<TS>`` so the file is deterministic across runs.

Regenerate via ``UPDATE_GOLDEN=1 uv run pytest tests/component/``.
Review the diff before committing — an unexpected change to a golden
file means engine behavior changed.
"""

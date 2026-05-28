"""Store — session/run entity records and the JSONL event log.

Per Module API Reference §3. The filesystem JSONL is ground truth;
entity records (SQLite default, Postgres scale-up) are rebuildable
from the log.
"""

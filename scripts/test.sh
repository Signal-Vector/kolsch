#!/usr/bin/env bash
# scripts/test.sh — test runner shortcuts.
#
# Why this script exists: the test suites have different
# parallelism profiles. tests/unit is fast serial (~1s) and xdist
# adds 2s of worker startup overhead. tests/component is slow
# serial (~7min, dominated by 10 CLI-chat outliers) and xdist with
# 14 workers drops it to ~2min. Mixing them under a single
# `-n auto` invocation is the worst of both.
#
# Usage:
#   ./scripts/test.sh fast              # tests/unit only, serial
#   ./scripts/test.sh slow              # component + integration + golden, parallel
#   ./scripts/test.sh all               # fast then slow
#   ./scripts/test.sh cov               # CI-style invocation with coverage
#   ./scripts/test.sh fast -k some_name # extra args pass through to pytest

set -euo pipefail

mode="${1:-fast}"
shift || true  # so "$@" is the remaining flags

case "$mode" in
    fast)
        # tests/unit — pure logic, ~1s serial. Don't parallelize.
        exec uv run pytest tests/unit -q "$@"
        ;;
    slow)
        # The long tail — component (dominated by CLI-chat tests),
        # integration, and golden. Parallel pays off here.
        exec uv run pytest \
            tests/component tests/integration tests/golden \
            -n auto -q "$@"
        ;;
    all)
        # Sequential: fast first (fail-fast on logic regressions),
        # then slow parallel.
        uv run pytest tests/unit -q "$@"
        exec uv run pytest \
            tests/component tests/integration tests/golden \
            -n auto -q "$@"
        ;;
    cov)
        # CI-style: coverage on, sequential (xdist + coverage is a
        # known footgun — workers must combine their .coverage data).
        # Use this locally to reproduce CI numbers.
        exec uv run pytest \
            --cov --cov-report=xml --cov-report=term \
            tests/unit tests/component tests/integration tests/golden \
            "$@"
        ;;
    -h|--help|help)
        sed -n '2,16p' "$0"
        ;;
    *)
        echo "unknown mode: $mode" >&2
        echo "usage: $0 {fast|slow|all|cov|help} [pytest args...]" >&2
        exit 2
        ;;
esac

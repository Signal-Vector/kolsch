# Containerfile — Agent Platform image (Podman, Docker-compatible).
#
# Per handbook §1.6 + §8.2: the container is the canonical environment.
# Source is live-mounted in compose.dev.yml (so code edits never trigger
# a rebuild) and COPIED in compose.test.yml (so the gate verifies the
# committed tree against a pristine environment).
#
# Layers ordered cheapest-first so dependency installs cache across
# code-only rebuilds. Everything is reproducible from uv.lock.

FROM python:3.12-slim-bookworm

# --- System packages ------------------------------------------------------
# git + ca-certificates: required by uv for VCS-based dependencies and
# TLS. build-essential is only needed for source-built wheels that don't
# ship a manylinux build — kept here so a `uv add` of such a package
# doesn't surprise contributors with a build failure later.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
        git \
        ca-certificates \
        build-essential \
        curl \
 && rm -rf /var/lib/apt/lists/*

# --- uv -------------------------------------------------------------------
# Pulled from the official static image to avoid pip-bootstrap churn.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# uv environment knobs:
# - PYTHONDONTWRITEBYTECODE: keeps the live-mounted source tree clean.
# - PYTHONUNBUFFERED: streams stdout/stderr live (good for compose logs).
# - UV_LINK_MODE=copy: required when the cache lives on a different
#   filesystem than the venv (e.g. named-volume vs. overlay).
# - UV_PROJECT_ENVIRONMENT: pin the venv location so it can be a named
#   volume in compose.dev.yml (persists across container restarts).
# - UV_CACHE_DIR: pinned for the named-volume cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/root/.cache/uv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin

WORKDIR /workspace

# --- Dependency layer ----------------------------------------------------
# Copy just the lockfile + pyproject so this layer caches as long as
# dependencies don't change. README.md is referenced by [project.readme],
# so it must be present at install time. We don't install the project
# itself yet — that happens after the source is mounted/copied so the
# wheel build sees real packages, not empty placeholders.
COPY pyproject.toml uv.lock README.md ./
# id=uv-cache-v3 (recurrence of bug-135): v2's named cache mount itself
# went stale on this host — `uv sync` failed resolving the pinned
# build backend with `ModuleNotFoundError: No module named
# 'hatchling.build'` even though pyproject.toml's [build-system] pin
# was already correct. Bumping the suffix again forces a clean cache,
# per the v2 comment's own instruction. Bump again if this recurs.
RUN --mount=type=cache,id=uv-cache-v3,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --all-groups

# --- Source layer --------------------------------------------------------
# In compose.dev.yml this is overlaid by a bind mount. In compose.test.yml
# it represents the committed tree.
#
# AGENTS.md and docs/ are force-included by pyproject's
# [tool.hatch.build.targets.wheel.force-include] (they ship as package
# data inside the `kolsch` umbrella package). Hatchling evaluates
# force-include even for editable installs, so both must exist on disk
# before `uv sync --frozen --all-groups` runs — otherwise the editable
# build fails with `Forced include not found`.
COPY src ./src
COPY tests ./tests
COPY AGENTS.md ./AGENTS.md
COPY docs ./docs

# bundles/ is the tracked `kolsch` agent bundle (bundle.yaml +
# tools/*.py + *.yaml). It is NOT under src/ and is NOT installed as
# package data — the test suite loads it straight from the committed
# tree via repo-root-relative paths (e.g.
# `Path(__file__).resolve().parents[4] / "bundles"` in
# tests/bundles/**, plus bundle-referencing tests under
# tests/component, tests/integration, tests/unit). Without this COPY
# those tests fail in the gate with `ToolSetupFailed: tool file does
# not exist: /workspace/bundles/...`.
COPY bundles ./bundles

# Install the project package itself (editable in-tree).
# id=uv-cache-v3: same cache as the dependency layer above — see that
# RUN instruction's comment (bug-135).
RUN --mount=type=cache,id=uv-cache-v3,target=/root/.cache/uv \
    uv sync --frozen --all-groups

# scripts/ holds commit-gate helpers — the gate runs
# `uv run python scripts/lint_chat_hex.py` (compose.test.yml). It is NOT
# part of the installed package (no force-include), so copy it AFTER the
# dependency sync so editing a script doesn't bust the sync cache layer.
COPY scripts ./scripts

# Default command keeps the dev container alive for `compose exec`.
CMD ["sleep", "infinity"]

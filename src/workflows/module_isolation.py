"""Per-run module-isolation discipline — Phase 3.1 Step 0.

The Phase 3.1 executor (Step 3+) MUST clean up bundle-loaded Python
modules between workflow runs so the next run re-imports them
fresh. Without this discipline, a second run would observe stale
top-level state from the first (caching dicts, decorator side-effects,
etc.) — silently wrong instead of loudly wrong.

The discipline is:

1. Snapshot ``sys.modules.keys()`` at run start, BEFORE the first
   node fires (see :func:`snapshot_sys_modules`).
2. Walk the worklist + execute nodes.
3. At run end (any terminal status: ``succeeded`` / ``failed`` /
   ``timed_out`` / ``aborted``), remove from ``sys.modules`` only
   those entries whose ``module.__file__`` lies under the workspace's
   ``bundles/`` tree (see :func:`cleanup_bundle_imports`). Third-party
   packages — pydantic, jinja2, httpx — survive untouched; their
   import cost dominates and re-importing them every run would be
   catastrophic.

The bundle-prefix filter is the load-bearing rule:
``module.__file__ startswith workspace_root / "bundles"`` is the
``"ours"`` discriminator. Modules without a ``__file__`` (built-ins,
namespace packages) survive too — they cannot belong to a bundle.

NO executor logic here — pure helper functions consumed by the
executor at Step 3.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "cleanup_bundle_imports",
    "snapshot_sys_modules",
]


def snapshot_sys_modules() -> frozenset[str]:
    """Capture the current ``sys.modules`` key set.

    The returned :class:`frozenset` is immutable so subsequent
    mutations to ``sys.modules`` during a run don't leak back into
    the snapshot. The :func:`cleanup_bundle_imports` discriminator
    only NEEDS the key set (it doesn't replay the modules
    themselves) — what matters is the diff between snapshot-time
    and cleanup-time entries.
    """
    return frozenset(sys.modules)


def cleanup_bundle_imports(
    *,
    before: frozenset[str],
    workspace_root: Path,
) -> int:
    """Remove bundle-loaded modules from :data:`sys.modules`.

    Walks the current ``sys.modules`` and deletes every entry that:

    - was NOT in ``before`` (i.e., loaded during this run), AND
    - has a ``module.__file__`` attribute, AND
    - whose ``__file__`` resolves to a path under
      ``workspace_root / "bundles" /``.

    Returns the count of removed entries — useful for the executor's
    ``workflow.run.completed`` payload + the multi-run test's
    assertion that the cleanup actually ran.

    Modules without ``__file__`` (built-ins, namespace packages) are
    left alone — they cannot belong to a bundle by construction.
    Third-party packages (httpx, pandas, etc.) also survive: their
    ``__file__`` lives in ``site-packages``, not the workspace.
    """
    bundles_root = (workspace_root / "bundles").resolve()
    removed = 0
    for name in list(sys.modules):
        if name in before:
            continue
        module = sys.modules.get(name)
        if module is None:
            continue
        file_attr = getattr(module, "__file__", None)
        if not isinstance(file_attr, str):
            continue
        try:
            module_path = Path(file_attr).resolve()
        except (OSError, RuntimeError):
            continue
        try:
            module_path.relative_to(bundles_root)
        except ValueError:
            continue
        del sys.modules[name]
        removed += 1
    return removed

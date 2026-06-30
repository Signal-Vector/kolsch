"""Per-run module-isolation discipline.

The executor MUST clean up bundle-loaded Python modules between
workflow runs so the next run re-imports them fresh. Without this
discipline, a second run would observe stale top-level state from the
first (caching dicts, decorator side-effects, etc.) — silently wrong
instead of loudly wrong.

The discipline is:

1. Snapshot `sys.modules.keys()` at run start, BEFORE the first node
   fires (see `snapshot_sys_modules`).
2. Walk the worklist and execute nodes.
3. At run end (any terminal status: `succeeded` / `failed` /
   `timed_out` / `aborted`), remove from `sys.modules` only those
   entries whose `module.__file__` lies under the workspace's
   `bundles/` tree (see `cleanup_bundle_imports`). Third-party
   packages — pydantic, jinja2, httpx — survive untouched; their
   import cost dominates and re-importing them every run would be
   catastrophic.

The bundle-prefix filter is the load-bearing rule:
`module.__file__ startswith workspace_root / "bundles"` is the
`"ours"` discriminator. Modules without a `__file__` (built-ins,
namespace packages) survive too — they cannot belong to a bundle.

[`RunModuleNamespace`][workflows.module_isolation.RunModuleNamespace]
is a per-run `sys.modules` key prefix so two concurrent runs loading
the same bundle module each see a distinct module instance under a
distinct key. The snapshot/cleanup discipline above continues to fire;
the per-run prefix is a complementary mechanism, not a replacement.

NO executor logic here — pure helper functions consumed by the
executor.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "RunModuleNamespace",
    "cleanup_bundle_imports",
    "snapshot_sys_modules",
]


def snapshot_sys_modules() -> frozenset[str]:
    """Capture the current `sys.modules` key set.

    The returned `frozenset` is immutable so subsequent mutations to
    `sys.modules` during a run don't leak back into the snapshot. The
    `cleanup_bundle_imports` discriminator only NEEDS the key set (it
    doesn't replay the modules themselves) — what matters is the diff
    between snapshot-time and cleanup-time entries.

    Returns:
        The set of module names present in `sys.modules` at call time.
    """
    return frozenset(sys.modules)


def cleanup_bundle_imports(
    *,
    before: frozenset[str],
    workspace_root: Path,
) -> int:
    """Remove bundle-loaded modules from `sys.modules`.

    Walks the current `sys.modules` and deletes every entry that:

    - was NOT in `before` (i.e., loaded during this run), AND
    - has a `module.__file__` attribute, AND
    - whose `__file__` resolves to a path under
      `workspace_root / "bundles" /`.

    Modules without `__file__` (built-ins, namespace packages) are
    left alone — they cannot belong to a bundle by construction.
    Third-party packages (httpx, pandas, etc.) also survive: their
    `__file__` lives in `site-packages`, not the workspace.

    Args:
        before: The snapshot taken at run start.
        workspace_root: The workspace whose `bundles/` subtree owns
            the modules to purge.

    Returns:
        The count of removed entries — useful for the executor's
        `workflow.run.completed` payload and the multi-run test's
        assertion that the cleanup actually ran.
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


# ---------------------------------------------------------------------------
# Per-run sys.modules namespace
# ---------------------------------------------------------------------------


class RunModuleNamespace:
    """Per-run `sys.modules` key prefix for concurrent-run isolation.

    Two concurrent runs that load the same bundle module would race
    on a shared `sys.modules` key under the snapshot/cleanup
    discipline alone (`_kolsch_node_<ref>` is the same string for
    both). This namespace prefixes the key with the run id's short
    tag so each run's module instance lives at a distinct key:

    ```text
    sys.modules["_kolsch_wrk_<short>._kolsch_node_<ref>"] = <module>
    ```

    The format is `_kolsch_<run-id[:12]>.` where the first 12 chars of
    the run id are `"wrk_"` + the first 8 chars of the UUID body.
    Cleanup at run end (`cleanup_for_run`) iterates `sys.modules` and
    removes every entry whose key startswith this run's prefix — two
    concurrent runs each have their own copy and their own cleanup;
    neither sees the other.

    Third-party imports remain global: `httpx`, `pandas`, `jinja2`,
    etc. continue to share `sys.modules` entries across runs. The
    per-run namespace covers ONLY bundle code routed through the
    catalog node runner.
    """

    __slots__ = ("_prefix",)

    def __init__(self, *, run_id: str) -> None:
        if not run_id.startswith("wrk_"):
            msg = f"RunModuleNamespace requires a wrk_-prefixed run id; got {run_id!r}"
            raise ValueError(msg)
        # The first 8 chars after the `wrk_` prefix are the namespace
        # tag; the full slice `run_id[:12]` is `wrk_<short>` (4 + 8
        # chars).
        self._prefix = f"_kolsch_{run_id[:12]}."

    @property
    def prefix(self) -> str:
        """The full `sys.modules` key prefix for this run.

        Returns a string like `"_kolsch_wrk_a1b2c3d4."`. Always ends
        with a trailing dot so `key.startswith(prefix)` correctly
        excludes a differently-named key that happens to share the
        leading characters.
        """
        return self._prefix

    def wrap_module_name(self, canonical: str) -> str:
        """Map a canonical (run-agnostic) module name to this run's prefixed key.

        Example: `wrap_module_name("_kolsch_node_acme_nodes_transform")`
        returns `"_kolsch_wrk_<short>._kolsch_node_acme_nodes_transform"`.

        Args:
            canonical: The run-agnostic module key.

        Returns:
            The same key namespaced under this run's prefix.
        """
        return f"{self._prefix}{canonical}"

    def cleanup_for_run(self) -> int:
        """Delete every `sys.modules` entry whose key startswith this run's prefix.

        Safe to call multiple times — subsequent calls are no-ops.
        Safe to call even when no modules were registered under the
        prefix (returns `0`).

        Returns:
            The number of entries removed.
        """
        removed = 0
        for name in list(sys.modules):
            if name.startswith(self._prefix):
                del sys.modules[name]
                removed += 1
        return removed

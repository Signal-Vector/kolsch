# QoL — `list_files(path=".")` should work in the workspace root

## Context

In session `ses_358e6f01-294c-40ff-87e8-5455f0fac8e6`, the
helper agent's first tool call was `list_files(path=".")` —
the natural way for an LLM to answer "what's in my current
directory." The call was denied with
`file_access.denied: matched_rule=null, reason=default deny`.
The agent gracefully fell back to `bash ls -la`, which worked
— but the fallback only works because bash is a more permissive
tool path. A user-authored agent that doesn't have `bash`
declared would hit a wall.

Root cause: the workspace's `file_access.allowed_paths`
contains subdirectories (`data/`, `bundles/`, `sessions/`,
`content/`, `logs/`) and one specific file (`platform.yaml`),
but **not the workspace root itself**. So when a tool resolves
`.` to the absolute workspace root, no allow rule matches and
the default-deny fires.

The intuitive behavior — `list_files(".")` shows the
workspace's top-level entries — should just work.

---

## Scope

| Change | File | Type |
|---|---|---|
| Add the workspace root to `allowed_paths` in setup's default | `src/cli/_setup.py`'s file_access template | edit |
| Document the default in the Phase 1.2 spec | `process/phase1.2-usability-specs.md` §2.2 Step B | edit |
| Regression test pinning the new entry | `tests/component/test_setup.py` | add |
| Migration: existing workspaces should gain the entry on `kolsch setup` re-run | piggyback on PR 3 of the chat-permissions bug doc (E-BUG-CHAT-001 option a) | edit |

**Not touched:**
- The `authorization.config.rules` (Phase 2.0 §3.1) — those are
  a separate layer and already work correctly.
- The `denied_paths` list — unchanged (`.env` and `.platform/`
  stay denied).

---

## Design

### Current setup template (per the user's platform.yaml)

```yaml
file_access:
  allowed_paths: [ data/, bundles/, sessions/, content/, logs/, platform.yaml ]
  denied_paths:  [ .env, .platform/ ]
```

### Proposed setup template

```yaml
file_access:
  allowed_paths: [ ., data/, bundles/, sessions/, content/, logs/, platform.yaml ]
  denied_paths:  [ .env, .platform/ ]
```

One entry added at the front: `.` (resolves to the workspace
root). That's the entire change.

### Why `.` and not `./` or the absolute path?

- `.` is what users and LLMs naturally type for "current
  directory."
- The file-access matcher resolves it against the workspace
  root via the existing `resolve_workspace` chain (Phase
  1.2 §1.1) — same as any other relative path in the allow
  list.
- Absolute paths in the template would lock the workspace
  location into the YAML, which breaks portability if the user
  moves the workspace (Phase 1.2's resolution chain handles
  moves transparently as long as paths stay relative).

### Listing-the-root behavior

After the fix:
- `list_files(path=".")` returns the workspace's top-level
  entries (the same six the user saw via `bash ls -la`).
- `list_files(path="bundles")` continues to work (matches
  `bundles/`).
- `read_file(path="platform.yaml")` continues to work
  (matches the explicit `platform.yaml` entry).
- `read_file(path=".env")` continues to be denied (matches
  the `denied_paths` entry; denied_paths beat allowed_paths
  per Phase 1 file-handling discipline).

No other behavior changes.

### Migration

Existing workspaces (the user's repro is one) have
`platform.yaml` files written before this fix. They need the
new entry to land too. The chat-permissions bug doc
(`bug-chat-permissions-and-console-leak.md`) already specs a
migration path via PR 3 / E-BUG-CHAT-001 option (a) — `kolsch
setup` detects the legacy allow-all auth rule and prompts to
migrate. **Piggyback this QoL onto that PR**: when the
migration runs, also add `.` to the front of `allowed_paths`
if it's missing. Backup discipline is the same (timestamped
`.platform/platform.yaml.bak.<ts>`).

If PR 3 of the chat-permissions bug lands first, this QoL
adds the workspace-root entry to its migration. If this QoL
lands first, PR 3's migration picks up both changes.

---

## Critical files

- `src/cli/_setup.py` — the file_access template (around the
  `_setup_platform_payload` function).
- `process/phase1.2-usability-specs.md` §2.2 Step B — the
  spec text that documents what setup writes.
- `tests/component/test_setup.py` — the test pin for §3.1
  rules already added per PR 1 of the chat-permissions bug
  can be extended with one more assertion.
- `bug-chat-permissions-and-console-leak.md` Step C developer
  prompt — needs a one-line addition to include the
  workspace-root entry in the migration.

---

## Verification

### Local smoke

```bash
# Fresh workspace via setup
uv run kolsch --root /tmp/kolsch-qol setup --non-interactive

# Confirm the new entry
grep -A2 'file_access' /tmp/kolsch-qol/platform.yaml
# Expected: allowed_paths includes "." as the first entry

# Confirm list_files(".") works
echo '{...}' | uv run kolsch --root /tmp/kolsch-qol run kolsch/agents/helper \
  --input content="list the files in my workspace"
# In the JSONL, the tool.called for list_files(path=".") should NOT have a
# matching tool.failed; the result should carry the six top-level entries.
```

### Tests

```bash
podman-compose exec dev uv run pytest tests/component/test_setup.py -q
```

The §3.1 rules test extended to also assert `.` is the first
entry in `file_access.allowed_paths`.

### Edge cases to eyeball

- A user running `list_files(path="..")` (parent directory) is
  still denied — `..` doesn't match `.` and resolves outside
  the workspace.
- A user running `list_files(path="./bundles")` still works —
  it resolves to `bundles/` and matches that entry; the new
  `.` entry isn't required for this case.
- `read_file(path=".")` returns the standard "is a directory"
  error from the tool itself, not a file-access denial. The
  allow rule doesn't change what `read_file` does with a
  directory path.

---

## Out of scope

- Workspace-relative globs (e.g., allowing `bundles/**` as one
  pattern instead of just `bundles/`). The current
  prefix-match semantics work; globs are a later refinement.
- Restricting which TOOLS can read the workspace root. Today
  `read_file` and `list_files` both consult the same
  allowed_paths list. Tool-scoped allow lists are a later
  feature.
- The `sessions/` allow-list architectural question
  (whether agents should be able to read session JSONLs).
  Coordinator already decided: agents can read sessions.
  Leaving `sessions/` in the list as-is.

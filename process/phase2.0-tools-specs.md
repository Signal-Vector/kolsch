# Phase 2.0 Patch — Builtin Tools, Workspace-Only Permissions, Live Rule Edits

**Draft 0.1**

> The first patch of Phase 2 (interactive agent experience). Phase 1
> shipped the Tools subsystem mechanics but no canonical tool set, and
> the v1 Authorization variant is allow-all — fine when nothing the
> agent does is risky. Phase 2.1 will introduce `kolsch chat`, where
> the agent runs `bash` and `write_file` against the user's machine
> in real time. Allow-all is no longer acceptable as a default; nor
> is "edit `platform.yaml` and restart" as a permission UX.
>
> This patch specifies: (1) the four builtin tools (`read_file`,
> `write_file`, `list_files`, `bash`) shipped as Python files in
> `bundles/kolsch/tools/`, (2) the new `prompt` effect that the
> Authorization provider treats as "ask the user at call time,"
> (3) the workspace-only default rule set written at `kolsch setup`,
> (4) the two live-edit surfaces — `kolsch permissions {list, add,
> remove}` out of chat and the four-choice modal in chat — and
> (5) the "rule changes apply within the same exchange" semantics.

---

## Part 0 — Why This Patch Exists

Phase 1 + 1.1 + 1.2 give us a workspace, a connected LLM, and the
ability to define an agent. The Tools subsystem exists (Phase 1) but
ships zero canonical tools. Authorization exists but is allow-all.
Together those mean: any agent that wants to *do* anything has to
have tools written for it from scratch, and once those tools exist
they can do anything to anything.

Missing today:
- A standard set of "this is what every agent needs to be useful"
  tools — read, write, list, shell.
- A permission default that's safe by construction. An agent
  pointed at the user's whole filesystem is the wrong starting
  point.
- A way to grant a permission without editing YAML and restarting.

This patch fills the gap. Headline design decisions:

1. **Tools ship as Python files in the kolsch bundle.** Not as
   "builtins" that hide inside the platform binary. The user can
   read them, copy them as templates, or swap them. Phase 1's
   Tools subsystem already discovers tool files in bundles; we
   add four files.
2. **Workspace-only by default; default-deny tail.** The setup-
   produced rule set allows `read_file` and `list_files` inside
   the workspace silently, prompts on `write_file` and `bash`,
   and denies everything else. Tightening, not loosening, is
   trivial for the user — adding rules is one CLI call.
3. **Live edits apply in the same exchange.** Authorization
   re-reads its rule list on every check. When the user picks
   "always allow" in the in-chat modal, the next tool call in
   the SAME exchange sees the new rule. No restart, no exchange
   boundary.
4. **The `prompt` effect is the third option, not a special-case
   approval hook.** Phase 1's authz returns allow / deny. We add
   `prompt`. The CLI's tool-call dispatcher knows that `prompt`
   means "pause and ask"; everything that isn't a chat surface
   (server, scripted `kolsch run`, tests) treats `prompt` as
   `deny` so non-interactive paths never hang.

---

## Part 1 — The Four Builtin Tools

Ship as `@tool`-decorated Python files in `bundles/kolsch/tools/`.
Each is a single function, typed strictly, with a docstring the
catalog uses to expose the tool to the model.

### 1.1 `read_file`

```python
# bundles/kolsch/tools/read_file.py
from kolsch.tools import tool
from pathlib import Path

@tool
def read_file(path: str, max_bytes: int = 1_048_576) -> str:
    """Read a UTF-8 text file from the workspace.

    :param path: Workspace-relative or absolute path. Absolute paths
        outside ``file_access.allowed_paths`` are rejected.
    :param max_bytes: Refuse to read files larger than this (default
        1 MiB). Protects context budgets.
    :returns: File contents as a UTF-8 string.
    :raises file.access_denied: Path outside allowed scope.
    :raises file.too_large: File exceeds max_bytes.
    :raises file.not_found: Path doesn't exist.
    """
```

### 1.2 `write_file`

```python
@tool
def write_file(path: str, content: str, *, create_parents: bool = True) -> dict:
    """Write a UTF-8 text file to the workspace.

    :param path: Workspace-relative or absolute path inside allowed scope.
    :param content: New file contents (full overwrite; not a patch).
    :param create_parents: Create missing parent directories.
    :returns: ``{"path": "<abs>", "bytes_written": N, "created": bool}``.
    :raises file.access_denied: Path outside allowed scope.
    :raises file.permission_denied: Authz returned ``deny``/``prompt-denied``.
    """
```

Full-overwrite semantics are deliberate — incremental editing tools
(patch-style) belong in a later phase when the model has reliable
diff-emission. Full-overwrite is honest about what it does.

### 1.3 `list_files`

```python
@tool
def list_files(
    path: str = ".",
    *,
    glob: str = "*",
    max_entries: int = 500,
    include_hidden: bool = False,
) -> list[dict]:
    """List entries under ``path`` (non-recursive by default).

    :param path: Directory to list. Workspace-relative or absolute.
    :param glob: Glob pattern; ``**/*`` for recursive.
    :param max_entries: Cap on returned entries.
    :param include_hidden: Include dotfiles.
    :returns: List of ``{"name", "path", "is_dir", "size_bytes"}`` dicts.
    """
```

### 1.4 `bash`

```python
@tool
def bash(
    command: str,
    *,
    cwd: str | None = None,
    timeout_seconds: int = 60,
) -> dict:
    """Run a shell command in the workspace.

    :param command: Command to execute. Single string; shell parsing
        applies (no auto-quoting of arguments).
    :param cwd: Working directory; defaults to the workspace root.
        Must resolve inside allowed scope.
    :param timeout_seconds: Kill the command after this many seconds.
    :returns: ``{"exit_code", "stdout", "stderr", "duration_seconds",
        "timed_out"}``. ``stdout``/``stderr`` truncated to 64 KiB each.
    :raises tool.timeout: Command exceeded ``timeout_seconds``.
    :raises file.access_denied: ``cwd`` outside allowed scope.
    """
```

Runs through the existing Sandbox provider (Phase 1's `native`
variant — subprocess, in-process). Docker sandbox is a later phase.

### 1.5 Bundle layout

```
bundles/kolsch/
├── bundle.yaml
├── agents/
│   └── helper.yaml       # from Phase 1.2; gains tools: declaration
└── tools/
    ├── read_file.py
    ├── write_file.py
    ├── list_files.py
    └── bash.py
```

Phase 1.2 created `bundles/starter/`; Phase 2.3 formalizes the
rename to `bundles/kolsch/`. If 2.0 ships before 2.3, the
implementation creates `bundles/kolsch/` minimally (the four tool
files + a `bundle.yaml`) and Phase 1.2's starter agent moves there
in the same change. The path mismatch with Phase 1.2 specs is
captured in a revision flag (TOOL-R12) so Phase 1.2 is updated
rather than silently superseded.

---

## Part 2 — The `prompt` Effect

### 2.1 The third value

Phase 1's Authorization Protocol returns one of two effects per
check: `allow` or `deny`. Phase 2.0 adds `prompt`.

```python
class AuthzEffect(StrEnum):
    ALLOW  = "allow"
    DENY   = "deny"
    PROMPT = "prompt"   # NEW: surface to interactive caller; treat as deny otherwise
```

Rule effects in `platform.yaml` extend to allow `prompt`:

```yaml
authorization:
  variant: rule_based
  config:
    rules:
      - { who: "tool.write_file", what: ".*", effect: prompt }
```

### 2.2 How callers interpret `prompt`

The Authorization provider returns `prompt` like any other effect.
The *caller* decides what to do with it:

| Caller | Treatment |
|---|---|
| `kolsch chat` (interactive) | Show the four-choice modal (§4.2). The user's choice resolves to allow-once / allow-always / deny-once / deny-always. |
| `kolsch run` (one-shot script) | Treat as `deny` and emit `authz.prompt_unattended` on the event log so replay shows what would have been asked. |
| `kolsch server` (HTTP) | Treat as `deny` with a structured response: `{"effect": "prompt", "rule_id": "<id>"}` so the HTTP caller can implement its own prompt UX. |
| Tests, golden | Treat as `deny`. |

This means `prompt` never hangs an unattended process. The chat is
the only surface that "owns" the interactive choice.

### 2.3 Where the decision lives

The Tools subsystem's call dispatcher calls Authorization before
invoking the tool function. Its dispatch table:

```python
match effect:
    case ALLOW:  return await tool_fn(**args)
    case DENY:   raise PlatformError("authz.denied", ...)
    case PROMPT: return await ctx.handle_prompt(effect_info, tool_fn, args)
```

`ctx.handle_prompt` is an injectable callback. Chat sets it to "show
the modal." Everything else sets it to "treat as deny." No
prompt-handling code in Authorization itself — Authorization
classifies, the caller chooses.

---

## Part 3 — Default Workspace-Only Rules

### 3.1 The rule set `kolsch setup` writes

When Phase 1.2's setup produces `platform.yaml`, the
`authorization.config.rules` section is generated with these
defaults (workspace path substituted):

```yaml
authorization:
  variant: rule_based
  config:
    rules:
      # Identifier convention: stable id assigned at parse time;
      # rules referenced by id in `kolsch permissions remove`.
      - id: rl_read_workspace
        who: "tool.read_file"
        what: "<workspace_root>/**"
        effect: allow
      - id: rl_list_workspace
        who: "tool.list_files"
        what: "<workspace_root>/**"
        effect: allow
      - id: rl_write_workspace
        who: "tool.write_file"
        what: "<workspace_root>/**"
        effect: prompt
      - id: rl_bash_any
        who: "tool.bash"
        what: ".*"
        effect: prompt
      - id: rl_default_deny
        who: ".*"
        what: ".*"
        effect: deny
```

### 3.2 Rule semantics

- **First-match wins.** Rules are evaluated in order; the first
  whose `who` and `what` match returns its effect. This is the
  existing Phase 1 semantics — unchanged.
- **`who` matches the tool name (and, in later phases, principal
  identity).** Phase 2.0 only uses tool name; the contract leaves
  room for `user.<id>` and `workflow.<id>` matchers in Phase 3.
- **`what` matches the tool's target string.** For file tools the
  target is the resolved absolute path. For `bash` it's the
  command string.
- **The default-deny tail is required.** `kolsch setup` writes it;
  `kolsch permissions remove` refuses to remove it; user can edit
  the YAML directly if they really want to but the CLI doesn't
  facilitate.

### 3.3 The `<workspace_root>` substitution

The setup-produced rules carry the resolved absolute workspace
path (not a `${VAR}` template). This is deliberate: rules are
data, not templates. If the workspace moves, the user re-runs
`kolsch setup` which detects drift and offers to update — same
mechanism as Phase 2.3's bundle drift.

### 3.4 Existing Phase 1 / 1.1 / 1.2 tests

Anything that ran against the old allow-all variant either:
- Sets its own rule set in the test fixture (preferred), or
- Uses an explicit `authorization.variant: allow_all_test` variant
  added under TOOL-R8 specifically for the test layer.

The `allow_all_test` variant is **not registered in production
builds** — it's gated behind a `KOLSCH_TEST_BUILD=1` env var or
similar. Production refuses to load it. Documented under TOOL-R10's
hard rule.

---

## Part 4 — Live Rule Editing

### 4.1 Out-of-chat: `kolsch permissions`

```
kolsch permissions list
kolsch permissions add  --who <pattern> --what <pattern> --effect {allow,prompt,deny} [--before <id> | --after <id> | --first]
kolsch permissions remove <id>
kolsch permissions show <id>
```

Each command:
- Reads `platform.yaml` from the resolved workspace.
- Mutates the `authorization.config.rules` list in place.
- Writes back atomically (temp + rename, per Phase 1.1's atomicity
  discipline).
- Reloads the in-memory Authorization provider's rule list **if a
  runtime is currently active in the same process** (long-running
  `kolsch server`); otherwise the next invocation picks it up from
  disk.

Stable rule IDs are assigned deterministically: `rl_<hash[:8]>`
based on the rule's `who+what+effect`. Re-ordering preserves IDs.

### 4.2 In-chat: the four-choice modal

When a tool call's authz check returns `prompt`, `kolsch chat`
shows a modal:

```
┌─ Permission requested ─────────────────────────────────────┐
│                                                            │
│ Tool:   bash                                               │
│ Command: rg "build_llm" src/                               │
│                                                            │
│ [y] allow once   [a] always allow this pattern             │
│ [n] deny once    [d] deny always                           │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

- `[y]` allow this single call; no rule change.
- `[a]` write a new rule with `effect: allow` and `who/what` matched
  to *the current call's exact tool name + target*. Position: just
  before the matching `prompt` rule, so future matching calls land
  on the new rule first.
- `[n]` deny this single call; no rule change.
- `[d]` symmetric to `[a]`: new `effect: deny` rule, placed before
  the `prompt` rule.

The "exact target" choice deserves a flag: for `bash`, "exact match"
on the command string is too narrow (`rg foo` next time wouldn't
match `rg bar`). The modal lets the user edit the `what` pattern
before committing:

```
[a] always allow this pattern  (edit pattern? [Enter] yes / [n] no)
> tool.bash | ^rg .*$
```

A reasonable default is offered (for `bash`, the first word of the
command becomes the pattern: `^rg .*`); the user can refine.

### 4.3 Live application semantics

When `[a]` or `[d]` runs:
1. The new rule is appended to the in-memory rule list at the
   computed position.
2. `platform.yaml` is rewritten atomically.
3. Chat resumes; the just-blocked tool call is re-issued; authz
   returns the new effect.

The "same exchange" guarantee: from the model's perspective, the
tool call either succeeds (after `[y]` or `[a]`) or fails with
`authz.denied` (after `[n]` or `[d]`). The model sees no "wait"
state; the modal pauses the engine's tool-call dispatch until the
user decides.

Internally, the engine emits:
- `authz.prompt_requested` event (with tool/target/rule_id) when
  the prompt fires.
- `authz.prompt_answered` event (with choice + new rule id if
  written) when the user responds.

Replay sees both events; reconstruction is honest about which
calls were user-approved live vs. allowed by pre-existing rules.

---

## Part 5 — Failure Modes

New error codes. Adds §4.16 "Tools & Permissions" to
`docs/explanation/error-model.md`.

| Code | Level | Detail | Where |
|---|---|---|---|
| `authz.denied` | Recoverable | `{tool, target, rule_id}` | Tools dispatcher when effect is deny |
| `authz.prompt_unattended` | Recoverable | `{tool, target, rule_id, caller}` | Non-chat callers when effect is prompt |
| `authz.rule_invalid` | Fatal | `{position, reason}` | `permissions add` validating user input |
| `authz.rule_not_found` | Fatal | `{id, available_ids}` | `permissions remove` for unknown id |
| `authz.protected_rule` | Fatal | `{id, reason}` | `permissions remove rl_default_deny` |
| `file.access_denied` | Recoverable | `{path, allowed_paths}` | File tools when path outside scope |
| `file.too_large` | Recoverable | `{path, size_bytes, max_bytes}` | `read_file` |
| `file.not_found` | Recoverable | `{path}` | `read_file`, `list_files` |
| `tool.timeout` | Recoverable | `{tool, timeout_seconds}` | `bash` |

Recoverable means the model sees the error in its tool-result
payload and can react (try a different path, retry with smaller
range, give up). Fatal means the engine aborts the exchange.

---

## Part 6 — Required Spec Revisions

### TOOL-R1. `docs/explanation/tools-subsystem.md`

Add §X "Builtin Tools" documenting the four tools in Part 1, their
parameters, error codes, and the fact that they live in
`bundles/kolsch/tools/`.

### TOOL-R2. `docs/explanation/file-handling.md`

Update the path-resolution discussion to clarify that
`file_access.allowed_paths` constrains the file tools, and the
tools surface `file.access_denied` for out-of-scope paths.

### TOOL-R3. `docs/explanation/secrets-sandbox-authz-contract.md`

Add §X "The `prompt` effect" covering: the third effect value,
caller dispatch rules from §2.2, and the live-edit semantics from
§4.3. Update the Authz Protocol signature.

### TOOL-R4. `docs/explanation/asset-management-contract.md`

Add `prompt` to the documented effect values in the `authorization`
section of the platform.yaml schema. Document the rule-id field.

### TOOL-R5. `docs/explanation/error-model.md` Part 4

Add §4.16 "Tools & Permissions" with the nine codes from Part 5.

### TOOL-R6. `docs/explanation/v1-scope.md` §2.4

Add `kolsch permissions {list, add, remove, show}` to the CLI
surface.

### TOOL-R7. `docs/reference/module-api-reference.md`

Add Part 11.8 "Permissions" covering: the live-edit API on the
Authorization provider, the `prompt` effect, the rule-id scheme,
and the CLI commands' module surface.

### TOOL-R8. `process/development-testing-handbook.md`

Add success criteria §10.15 "Tools & Permissions":
- The four builtin tools land in `bundles/kolsch/tools/` and are
  discovered by the existing Tools subsystem catalog.
- A fresh `kolsch setup` produces a workspace whose `platform.yaml`
  has the workspace-only default rules.
- `prompt` effect is treated as deny by non-chat callers; the
  `authz.prompt_unattended` event is emitted.
- Rule changes from `kolsch permissions` or the in-chat modal
  apply to the very next authz check, no restart.
- The `allow_all_test` variant is registered ONLY under
  `KOLSCH_TEST_BUILD=1`.

Add §10.15-also: every existing Phase 1+ component test that
relied on allow-all must explicitly set its own rule set (or use
the `allow_all_test` variant) — no test relies on production
defaults being permissive.

### TOOL-R9. `process/test-catalog.md`

Add Part 9.8 "Tools & Permissions (TOOL)" with cases TC-TOOL-01..15
covering the builtins, the prompt effect, default rules, live
editing, and the regression that the engine's pre-flight
respects the new rules.

### TOOL-R10. `AGENTS.md`

Add two Hard Rules:

> Tools shipped by the platform live in `bundles/kolsch/tools/` as
> plain Python files. Never embed a "builtin tool" in the engine
> or runtime; the file IS the tool, and users can read it.

> The production Authorization rule set must end with a
> default-deny tail. The `allow_all_test` variant is for tests
> only; production builds refuse to load it.

### TOOL-R11. `README.md` + `docs/tutorials/first-agent.md`

README's quickstart section mentions that the helper agent can
read/write/list/bash inside the workspace. Tutorial gains a small
sidebar on the in-chat permission modal at step 9 (the section
about real-LLM use).

### TOOL-R12. Phase 1.2 alignment

`process/phase1.2-usability-specs.md` §2.4 (starter agent
location) references `bundles/starter/`. This patch supersedes
that — flag as a TOOL-R12 revision pointing at the Phase 1.2
patch and add a one-paragraph note that Phase 2.0 + 2.3 land on
`bundles/kolsch/`. Do NOT edit Phase 1.2 mid-implementation if
it's already been built — Phase 2.3's drift detection handles the
rename.

### TOOL-R13. New how-to: `docs/how-to/permissions.md`

Recipes for: tightening defaults, granting bash patterns,
auditing the current rule set, undoing a chat-time approval, the
deny-list discipline.

---

## Part 7 — Explicitly Deferred

1. **Per-agent rule scopes.** Today rules apply platform-wide.
   Agent-scoped rules (one agent can `bash`, another can't) wait
   for the Workflow Engine to make the discrimination useful.
2. **Time-limited rules.** "Allow this for 5 minutes." Useful for
   exploratory chat; out of scope for 2.0. The mechanism would be
   `expires_at` on the rule; revisit if asked.
3. **Pattern history / smart pattern suggestions.** The in-chat
   modal offers a single default pattern when the user picks `[a]`.
   "Suggest the right `bash` pattern based on past calls" is
   pattern-mining; later phase.
4. **Audit log export.** `authz.prompt_requested` / `_answered`
   events are on the JSONL; pretty-printing them as a security
   audit trail is a how-to, not a feature.
5. **Sandbox tightening for `bash`.** The native sandbox runs
   bash in the same process. Real isolation (docker, firejail) is
   the original Phase 2's `docker_sandbox` variant, deferred to
   the later workflow/IDE phase.

---

## Part 8 — Open Items

1. **Multi-line `bash` commands and shell-meta safety.** `bash`
   takes a single string and runs it through `sh -c`. A model that
   emits `rm -rf /` would, if the user clicked `[y]`, succeed at
   destruction (within file-access scope). The default-deny tail
   prevents this for paths outside the workspace, but inside the
   workspace `bash` has near-full reach. Acceptable for 2.0;
   flagged because the in-chat modal is the only thing between
   the model and damage. Consider a "preview the command's
   effect" pre-execution dry-run later.
2. **The "edit the suggested pattern" UX in the modal.** A modal
   that lets the user free-form-edit a regex inside a TUI is
   non-trivial. v1 of the modal accepts the suggested default
   without inline edit; "edit pattern" is a follow-up command
   (`kolsch permissions add` is the escape valve). Flagged for
   2.1 polish.
3. **Re-applying rules to an in-flight tool call.** If a rule
   changes (via `permissions add` from another shell) while a
   tool call is mid-flight, do we re-check? Decision: NO. The
   authz check happens before tool dispatch; once dispatched, the
   tool runs to completion. Future-changing rules don't
   interrupt in-progress calls. Flagged.
4. **Cross-platform `bash`.** Windows doesn't ship `bash`. v1
   says "bash" and means it; Windows users need WSL. Document
   the limitation; rename to `shell` later if Windows-native
   support is added. Flagged.
5. **The default 1 MiB read limit.** Big enough for any sane code
   file; small enough that the model doesn't blow context on
   accident. Flagged as a tunable per-workspace via
   `defaults.tool_limits` in a later patch.

---

## Part 9 — Status

This is **Phase 2.0 patch draft 0.1**. The patch:

- Closes the "no canonical tools" gap with four files in one
  bundle.
- Replaces the allow-all Authorization default with a workspace-
  only, default-deny rule set.
- Adds the `prompt` effect so authorization can defer to the
  user without coupling Authz to a UI.
- Establishes live rule editing as a first-class CLI surface.

**Apply in two passes:**

1. **Revisions.** TOOL-R1 through TOOL-R13 mechanically edit
   existing docs and create the one new how-to.
2. **Implementation.** Ship the four tool files; extend the
   Authz Protocol with `prompt`; add the `kolsch permissions`
   subcommands; add the in-chat modal hook (callback only — the
   chat UI itself is Phase 2.1); add TC-TOOL-01..15.

After this patch lands, the platform has the tools and the
permission discipline for Phase 2.1's interactive chat to be safe
by default. That is the bar.

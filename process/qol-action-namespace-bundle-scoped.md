# QoL — Migrate tool action namespaces to bundle-rooted form (`tool.<home_bundle>.<name>`)

**Draft 0.1** *(stub spec, filed alongside the E-QOL-PERMDISC-001 resolution. Captures the design context for a future agent to pick up cold. Not yet sequenced.)*

> Action names produced by `src/tools/decorator.py:128`
> today are single-segment: `f"tool.{self.name}"`. The four
> builtin tools resolve to `tool.bash`, `tool.read_file`,
> etc. This means two tools sharing a name across different
> bundles (e.g., `kolsch/bash` and `andre/bash`) collide on
> the action string and share authz rules.
>
> The right long-term form is bundle-rooted:
> `tool.<home_bundle>.<name>` — so `tool.kolsch.bash` and
> `tool.andre.bash` are distinct authz subjects.
>
> Filed from `qol-tool-permission-discovery-uniform-with-stacking.md`
> E-QOL-PERMDISC-001 coordinator resolution. That QoL ships
> the modal-on-no-rule + bundle permissions feature against
> single-segment names; this follow-up handles the
> namespace migration as its own focused project.

---

## Part 0 — What needs to change

1. **Decorator** (`src/tools/decorator.py:128`) — return
   `f"tool.{home_bundle}.{name}"` instead of
   `f"tool.{name}"`. Requires the decorator to know the
   tool's home bundle, which today it doesn't — the
   decorator runs at import time, before the catalog has
   classified the tool's location.

2. **Tool registration** — the engine's pre-flight currently
   passes the agent's resolved config + a bundle-aware
   catalog to the tool subsystem. The home-bundle attribution
   can happen at registration time (when the tool is bound
   to an agent's session) rather than at decorator time.
   Easier than threading bundle info through the decorator
   itself.

3. **Existing authz rules** in `platform.yaml` carry strings
   like `tool\\.bash`. Post-migration, the rule's intent is
   `tool\\.kolsch\\.bash` (or a regex that matches both).
   Migration story needed:
   - Deprecation warning: rules using the legacy
     single-segment form emit `config.deprecated_rule_string`
     (Recoverable, new event class — parallel to the
     `config.deprecated_ref_form` shipped by
     `qol-prefix-defaults-and-unified-refs.md`).
   - Migration shim: legacy rules still match against the
     new namespace via prefix-fallback (treat `tool.bash`
     as equivalent to `tool.*.bash` during the migration
     window).
   - User-facing `kolsch permissions migrate` command
     (probably) to rewrite legacy rule strings to the new
     form.

4. **Starter bundle** — `helper.yaml`'s rule
   `what: "tool\\.bash"` becomes `what: "tool\\.kolsch\\.bash"`.

5. **Docs cascade** — every example in
   `docs/how-to/permissions.md`, `docs/reference/schemas.md`,
   any tutorial that shows authz rule strings.

---

## Part 1 — Why this isn't done today

The shipped Phase 2.2 work and the recent unified-refs QoL
(`qol-prefix-defaults-and-unified-refs.md`) both chose to
defer the action-namespace migration:

- **Phase 2.2** added agent-level permissions but kept
  the single-segment action names. Rationale at the time:
  no canonical bundle had two tools with the same name as
  another bundle's tool, so collision was theoretical.
- **The unified-refs QoL** changed how authors WRITE refs
  in YAML (`kolsch/bash` instead of `kolsch/tools/bash`)
  but didn't touch the runtime action namespace. The
  changes were scoped intentionally narrow.

The migration deferred until this point because no live
user complaint surfaced the collision. The bundle
permissions QoL is the first place where the asymmetry
becomes visible: bundle-scoped rules want to talk about
"this bundle's tools" — but action names don't currently
carry the bundle identity.

---

## Part 2 — Design questions worth resolving before specing the build

1. **Where does home-bundle attribution happen?**
   - Option A: at the `@tool` decorator (requires the
     decorator to know its file's bundle — sketchy).
   - Option B: at tool registration time, when the engine
     binds tools to an agent's session. Cleaner; the
     catalog already knows the home bundle.
   - Option C: introduce a new `bundle` kwarg on `@tool`
     that authors set manually. Simplest but invites
     drift between the YAML location and the declared
     bundle.

   Likely B, but worth Step 1 confirmation.

2. **Legacy-rule deprecation policy.** How long does the
   prefix-fallback shim live? One minor version? Two?
   Forever (no removal)?

3. **Migration command UX.** `kolsch permissions migrate`
   that auto-rewrites legacy rule strings — opt-in
   confirmation per rule, or batch?

4. **Cross-bundle authz reference.** Today a rule's
   `what` field is a regex. Post-migration, do we want a
   convenience syntax like `tool.kolsch.*` for "all
   kolsch tools," or do users just write regex anchors
   themselves?

---

## Part 3 — Suggested step shape (when picked up)

- **Step 1**: verification + design pick for the
  home-bundle attribution site (A/B/C above) + migration
  policy decisions.
- **Step 2**: implement attribution + deprecation event
  class + prefix-fallback shim.
- **Step 3**: starter bundle + docs cascade + tests +
  manual e2e covering both legacy and new rule strings.

Probably 4 steps if `kolsch permissions migrate` is
in-scope.

---

## Part 4 — Related work

- **Filed from**:
  `qol-tool-permission-discovery-uniform-with-stacking.md`
  E-QOL-PERMDISC-001 resolution.
- **Parallels**:
  `qol-prefix-defaults-and-unified-refs.md`'s
  `config.deprecated_ref_form` deprecation surface — same
  pattern for legacy rule strings.
- **Doc cascade target**: every example in
  `docs/how-to/permissions.md` and
  `docs/reference/schemas.md` that shows an authz rule
  string.
- **Sibling stub filed alongside this one**:
  `qol-tool-permissions-binding-unused.md`.

---

## Part 5 — When to pick this up

- When a user files a bug or qol complaint about two tools
  with the same name across bundles sharing rules.
- When a third-party bundle ecosystem emerges with
  conflicting tool names.
- When the canonical bundle ships more than one tool of
  the same name (currently doesn't).

Not urgent for v1; documented v1 caveat in
`docs/how-to/permissions.md` per
`qol-tool-permission-discovery-uniform-with-stacking.md`
Step 3.

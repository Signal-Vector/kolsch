# QoL — Ship canonical kolsch agents as package resources

**Draft 0.1** *(Step 5 follow-up from
`bug-engine-extends-parent-not-rendered.md`. Skeleton; flesh
out before scheduling.)*

## Problem

`importlib.resources.files("kolsch")` currently surfaces only
`docs/` + `AGENTS.md` (see `src/kolsch/__init__.py`'s
``packages.find` configuration); no agent YAMLs ship in the
installed wheel. So a `kolsch:` prefixed extends ref
(`extends: kolsch:agents/helper.yaml`) can never resolve —
the only way to inherit from the kolsch helper today is
through the workspace catalog (`extends: kolsch/helper`),
which requires the user to have run `kolsch setup` so the
starter bundle is materialised on disk.

Symptom in the wild: doc authors keep writing
`extends: kolsch:agents/<name>.yaml` because it reads like
`kolsch:docs/index.md` (a working `kolsch:` ref); they hit
`config.parent_agent_not_found` at agent-load. The
`bug-engine-extends-parent-not-rendered.md` Step 4 docs work
around this by routing readers to the catalog form
(`extends: kolsch/helper`) and explicitly calling out the
broken `kolsch:agents/` form, but the underlying
inconsistency in what the `kolsch:` prefix can address is
the real fix.

## Proposed

Ship the starter bundle (`src/cli/_starter_bundle/kolsch/`)
as installed package data so `importlib.resources.files
("kolsch")` exposes `agents/helper.yaml`, `tools/*.py`,
`skills/*/SKILL.md`, and `bundle.yaml` in addition to the
existing `docs/` + `AGENTS.md`. Then
`extends: kolsch:agents/helper.yaml` resolves to the wheel's
copy of the helper, independent of whether the user ran
`kolsch setup`.

## Open questions

- Two source-of-truth copies? After `kolsch setup` the
  workspace has `bundles/kolsch/agents/helper.yaml` and the
  package would have `agents/helper.yaml`. Which wins when
  the user edits the workspace copy? Probably: workspace
  catalog ref (`kolsch/helper`) keeps resolving to the
  workspace copy; only the `kolsch:` prefixed form reaches
  the package copy. That's surprising but consistent.
- Or alternative: deprecate the `kolsch:agents/` form
  entirely (it's already broken; nobody depends on it) and
  delete the prefix branch from
  `templating.inheritance._resolve_extends_to_path`. Forces
  the catalog-ref form as the single canonical path.

## Scope (rough)

Probably one step. `pyproject.toml` `[tool.setuptools.package-data]`
or `[tool.setuptools.packages.find]` adjustment;
`MANIFEST.in` update if present; a wheel-build integration
test confirming `importlib.resources.files("kolsch") /
"agents/helper.yaml"` reads. Docs follow-up in
`docs/how-to/extending-an-agent.md` to mention the package
form as an alternative.

## Sequencing

Independent. No blockers; no blockers downstream that the
team has named.

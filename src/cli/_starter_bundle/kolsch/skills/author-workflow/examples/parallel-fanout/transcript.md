# Transcript — parallel fanout

## Brief

> Build a workflow that, given a list of `urls`, fetches each one in parallel (just `curl -sI -o /dev/null` for now), then joins the results. Use auto-mode concurrency.

## Turn 1 — discover + plan

A `map` node is the right shape: `over: <list>` + `apply: <worker>` + `item_as: <var-name>` (REQUIRED — the loop variable name the worker reads as `inputs.<var-name>`). Setting `parallel: true` runs workers concurrently; setting top-level `concurrency: auto` opts the workflow into the concurrent worklist.

Important constraint: `parallel: true` workers may NOT touch state. The probe is a pure bash that emits to stdout — no state writes — so we're safe.

Sketch: `fetch_each (map) → summarise (bash)`. The worker `probe` is referenced by `apply:` and is NOT directly addressable from outside the map. `summarise` reads `nodes.fetch_each.results`, not `nodes.probe.stdout`.

## Turn 2 — first draft

`write_file: bundles/my-bundle/workflows/probe-urls.yaml`:

```yaml
description: ...
concurrency: auto
nodes:
  fetch_each:
    type: kolsch/nodes/map
    over: "{{ inputs.urls }}"
    apply: probe
    item_as: url
    parallel: true
  probe:
    type: kolsch/nodes/bash
    command: "curl -sI -o /dev/null -w '{{ inputs.url }} %{http_code}\\n' '{{ inputs.url }}'"
  summarise:
    type: kolsch/nodes/bash
    command: "echo {{ nodes.fetch_each.results | length }} fetched"
graph:
  edges:
    - from: fetch_each
      to: summarise
```

`validate_workflow: yaml_path=...` → `is_valid: true`. The probe is reachable as the `apply:` target of `fetch_each`; the map node IS the addressable identity from sibling nodes.

## Turn 3 — surface the ref

Ready. Try it:

```
/workflow my-bundle/workflows/probe-urls with urls='["https://example.com","https://example.org","https://example.net"]'
```

The three probes run in parallel; `summarise` fires once all three workers complete.

## What I'd watch for

If you add a `set_state` write inside the worker subgraph the gate will reject the workflow with `WorkflowParallelStateConflict`. Either drop `parallel: true`, drop `concurrency: auto`, or hoist the state write OUT of the worker.

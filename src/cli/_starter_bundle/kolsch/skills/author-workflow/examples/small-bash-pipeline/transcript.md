# Transcript — small bash pipeline

## Brief

> Build a workflow that prints the current date, lists the files in the workspace root, and writes a one-line summary to `out/summary.txt`.

## Turn 1 — discover + plan

I'll inspect the catalog first to see what's already shipping, then sketch a three-node linear graph.

```bash
$ kolsch workflows list
kolsch/workflows/hello
kolsch/workflows/hello-agent
...
$ kolsch nodes list
kolsch/nodes/bash
kolsch/nodes/write_file
...
```

No new authored `@node` needed — `bash` and `write_file` cover this. Three nodes: `stamp` (date), `inventory` (count), `summarise` (write). Fan-in into `summarise`.

## Turn 2 — first draft

`write_file: bundles/my-bundle/workflows/snapshot.yaml`:

```yaml
description: ...
nodes:
  stamp:
    type: kolsch/nodes/bash
    command: "date -u +%Y-%m-%dT%H:%M:%SZ"
  inventory:
    type: kolsch/nodes/bash
    command: "ls -1 | wc -l | tr -d ' '"
  summarise:
    type: kolsch/nodes/write_file
    path: "out/summary.txt"
    content: "snapshot at {{ nodes.stamp.stdout | trim }}: {{ nodes.inventory.stdout | trim }} entries\n"
graph:
  edges:
    - from: stamp
      to: summarise
    - from: inventory
      to: summarise
```

`validate_workflow: yaml_path=.../snapshot.yaml` → `is_valid: true`. (Two roots, both fan into `summarise` — AND-strict fan-in fires it once both complete.)

## Turn 3 — surface the ref

Ready. Try it:

```
/workflow my-bundle/workflows/snapshot
```

This writes `out/summary.txt` after both upstream bash nodes finish.

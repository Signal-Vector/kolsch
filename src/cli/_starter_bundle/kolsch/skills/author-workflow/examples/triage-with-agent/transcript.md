# Transcript — triage with agent

## Brief

> Build a workflow that takes a `ticket_text` input, classifies it as urgent or normal, and prints "URGENT" or "NORMAL" to stdout.

This is the canonical Scenario 1 verification case. Demonstrates the "agent → branch → terminal" shape, plus a first-draft revision driven by the error-class table.

## Turn 1 — discover + plan

```bash
$ kolsch agents list
kolsch/agents/classifier
kolsch/agents/urgent_responder
...
```

`kolsch/agents/classifier` already exists — its prompt emits a one-word classification (`urgent` / `normal`). I'll reuse it. Graph: `classify (agent) → is_urgent (branch) → urgent_marker | normal_marker (bash)`.

## Turn 2 — first draft (intentionally broken)

I'll bind the classifier's output into the branch — but on first pass I write TWO inbound binds with the same key into the branch:

```yaml
graph:
  edges:
    - from: classify
      to: is_urgent
      binds:
        label: "{{ nodes.classify.text }}"
    - from: classify   # duplicate edge — typo on the from-node
      to: is_urgent
      binds:
        label: "{{ nodes.classify.text }}"
    ...
```

`validate_workflow` returns:

```json
{
  "is_valid": false,
  "errors": [
    {
      "error_class": "WorkflowDuplicateEdge",
      "identifying_names": {"from_node": "classify", "to_node": "is_urgent"},
      "caller_message": "The workflow has the same connection between two steps declared twice...",
      ...
    }
  ]
}
```

## Turn 3 — diagnose + fix

The error-class table maps `WorkflowDuplicateEdge` to "remove the duplicate edge between the named pair". I delete one of the two `from: classify -> to: is_urgent` edges. Re-validate.

```json
{ "is_valid": true, "errors": [], "warnings": [] }
```

## Turn 4 — surface the ref

Ready. Try it:

```
/workflow my-bundle/workflows/triage with ticket_text="my server is on fire"
# → urgent_marker fires; normal_marker is pruned.
```

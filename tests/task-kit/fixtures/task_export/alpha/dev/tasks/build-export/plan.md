# Plan: Build the export

## Forward overview

```yaml
task_items:
  items:
  - id: write-schema
    title: Write the schema
    state: in-flight
    priority: P1
  - id: pick-name
    title: Pick the command name
    state: blocked-user
    priority: P1
    note: Choose between export and dump.
  - id: write-cli
    title: Write the CLI
    state: available
    priority: P2
  - id: later-polish
    title: Polish the messages
    state: deferred
    priority: P3
```

### write-schema -- Write the schema

Write one JSON Schema per kind and freeze it.

### pick-name -- Pick the command name

Both names are short; export reads as the verb.

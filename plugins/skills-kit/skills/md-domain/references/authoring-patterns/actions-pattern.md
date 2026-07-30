# Actions pattern

Actions encode deterministic multi-step recipes as ordered YAML step sequences. Each step is either a tool invocation or a user-facing message. The pattern makes execution reliable and repeatable -- the agent runs every step in order rather than improvising the sequence from memory.

Use when a document carries a recipe whose correctness depends on running the same N steps in the same order every time -- typically because (a) skipping a step has silent failure modes, (b) intermediate outputs flow into later steps, or (c) the user expects a consistent end-to-end report.

This is the canonical worked example of the "ordered list of operations" shape -- structural information about *how to perform an operation* AND *how to describe the operation to the user* sits in one place, carried by the same record.

## Schema

The shape of an `actions:` record. Each action is a named entry under `actions:` carrying description / prerequisite / inputs / steps.

```yaml
actions:
  action_name:
    description: What this action achieves end-to-end.
    prerequisite: Conditions that must be true before starting.
    inputs:
      param_name: Description of what the caller provides.
    steps:
      - tool: <tool_id>            # Which tool to use
        command: <template>         # Command with <param_name> placeholders
        capture: [field1, field2]   # Optional: values extracted for later steps
      - tool: <tool_id>
        command: <template using <field1>>
      - tell_user: <message>        # Communicate to the user (no tool call)
```

## Field semantics

- **`description`** -- one sentence stating what the action achieves end-to-end. Read aloud, it should answer "why would I run this action."
- **`prerequisite`** -- conditions the agent verifies (or assumes true) before starting. If a prerequisite is unmet, the action stops and reports rather than producing partial output.
- **`inputs`** -- per-parameter dictionary. Names appear as `<param_name>` placeholders in step commands.
- **`steps`** -- ordered list. Each entry is either a `tool` step (produces a tool call) or a `tell_user` step (produces a user-facing message, no tool call).
- **`capture`** -- optional per-step list of field names to extract from the step's output. Captured values are referenced in later steps via `<field_name>` placeholders.

## Execution rules

Three rules govern how the agent executes a step sequence.

1. **Every step is mandatory.** The agent does not skip steps based on perceived redundancy. Steps that look skippable in context are often the steps whose omission causes the silent failure modes the action exists to prevent.
2. **A failed step stops the action.** The agent reports the failure and does not continue past it. Partial completion of a recipe is rarely useful and often misleading.
3. **`capture` extracts values for later steps.** The contract for a captured field is that it appears in the step's output (typically as a YAML key). Later steps reference captured values via `<field_name>` placeholders.

## Facade script convention

When an action requires 3 or more sequential tool calls that always run together, wrap them in a single facade-script subcommand rather than emitting each tool call separately. The script outputs YAML so the agent can report status cleanly.

Reasons to facade:

- The N tool calls always co-occur; emitting them separately doubles the tool-call cost without efficiency win.
- Intermediate state is internal to the recipe and the user does not need it surfacing.
- A single failure mode at the script level is easier to report than N partial-failure messages.

Reasons to keep raw tool calls:

- The action is exploratory; the next step depends on the previous step's output in a way the recipe cannot pre-declare.
- One-off operations that are not part of an established recipe.

The facade-script form and the action-with-steps form are interchangeable shapes for the same recipe. Pick by whether the user benefits from seeing per-step progress (action shape with `tool:` steps) or only the aggregated result (facade-script shape).

## Narrate single tool calls

For operations that remain as individual tool calls -- lookups, one-off commands, exploratory queries -- always print a single line of prose explaining what the agent is doing and why before making the call. Tool calls are opaque to the user without narration.

Compare:

```
Looking up the configured retention policy.
<tool call: read_config retention.yaml>
```

vs.

```
<tool call: read_config retention.yaml>
```

The first form lets the user follow along without scrolling through tool transcripts; the second leaves the user guessing what the agent is up to.

## Worked example: pre-release check

A document declares a `pre_release_check` action that always runs the same four operations -- lint, test, config-validate, changelog -- in order.

```yaml
actions:
  pre_release_check:
    description: Verify a build is ready for release by running the lint, test, config-validate, and changelog checks in order.
    prerequisite: A build artifact exists at the path the caller passes in.
    inputs:
      build_path: Absolute path to the build artifact directory.
    steps:
      - tool: build_gate.py
        command: build_gate.py lint --path <build_path>
        capture: [lint_status, lint_errors]
      - tool: build_gate.py
        command: build_gate.py test --path <build_path>
        capture: [test_status, test_failures]
      - tool: build_gate.py
        command: build_gate.py config-validate --path <build_path>
        capture: [config_status]
      - tool: build_gate.py
        command: build_gate.py changelog --path <build_path>
        capture: [changelog_present]
      - tell_user: |
          Pre-release check results:
          - lint: <lint_status> (<lint_errors> errors)
          - tests: <test_status> (<test_failures> failures)
          - config: <config_status>
          - changelog: <changelog_present>
```

Because all four steps run unconditionally as a batch, a facade-script form (`build_gate.py pre-release --path <build_path>` returning a single YAML report) is also legitimate. The action shape and the facade-script shape are interchangeable; pick one based on whether the user benefits from seeing per-step progress.

## Worked example: index regeneration

A document maintains a cached index over an authoritative data source. A `regenerate_index` action rebuilds and verifies the cache:

```yaml
actions:
  regenerate_index:
    description: Rebuild the cached index from the authoritative data source and verify the result is well-formed.
    prerequisite: The authoritative data source is readable.
    inputs:
      source_path: Absolute path to the authoritative data source.
      output_path: Absolute path where the rebuilt index is written.
    steps:
      - tool: indexer.py
        command: indexer.py build --source <source_path> --output <output_path>
        capture: [record_count, build_duration_seconds]
      - tool: indexer.py
        command: indexer.py validate --index <output_path>
        capture: [validation_status, validation_errors]
      - tell_user: |
          Index regenerated.
          - records: <record_count>
          - build duration: <build_duration_seconds>s
          - validation: <validation_status>
```

This action has only two tool calls and one user message. It does not warrant a facade script -- the cost of batching is roughly equal to the cost of two narrated tool calls. Facade scripts pay off at 3+ tool calls.

## When to add an action

Add a new action when (a) the recipe is invoked enough that improvising it from memory is a real failure mode, or (b) the recipe has captured-output flow between steps that an ad-hoc invocation would lose. Single-shot operations that are rarely run are better served by direct tool-call invocation with narration.

When an action spans multiple sub-areas, declare it in the area that owns the primary concern and reference tools from other areas by their full path. Avoid duplicating actions across sub-areas.

## Audit hooks

A document that declares actions can be mechanically checked against:

- Every action has a `description` (one sentence).
- Every action has a `steps:` list with at least one entry.
- Each `tool:` step names a tool referenced elsewhere in the document (no dangling tool references).
- `capture:` fields are referenced in later step commands (no captures that go unused).
- `tell_user` messages reference captured fields via `<field_name>` placeholders that match a prior `capture:` entry.

These checks are mechanical; an audit script consuming the action records can assert each invariant.

# Actions pattern

Actions encode deterministic multi-step workflows as YAML step sequences attached to a sub-domain or capability. Each step is either a tool invocation or a user-facing message. The pattern makes execution reliable and repeatable: the agent runs every step in order rather than improvising the sequence from memory.

Use when a skill has a recipe whose correctness depends on running the same N steps in the same order every time -- typically because (a) skipping a step has silent failure modes, (b) intermediate outputs flow into later steps, or (c) the user expects a consistent end-to-end report at the end.

## Structure

```yaml
actions:
  action_name:
    description: What this action achieves.
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

### Field semantics

- `description` -- one sentence stating what the action achieves end-to-end. Read aloud, it should answer "why would I run this action."
- `prerequisite` -- conditions the agent must verify (or assume true) before starting. If a prerequisite is unmet, the action stops and reports rather than producing partial output.
- `inputs` -- per-parameter dictionary. Names appear as `<param_name>` placeholders in step commands.
- `steps` -- ordered list. Each entry is either a `tool` step (produces a tool call) or a `tell_user` step (produces a user-facing message, no tool call).
- `capture` -- optional per-step list of field names to extract from the step's output. Captured values are referenced in later steps via `<field_name>` placeholders.

## Rules

1. Every step is mandatory. The agent does not skip steps based on perceived redundancy.
2. If a step fails, the agent stops and reports the error. It does not continue past a failure.
3. `tool` steps produce tool calls; `tell_user` steps produce text messages with no tool call.
4. `capture` extracts values from a step's output for later steps. The contract for a captured field is that it appears in the step's stdout (typically YAML).
5. `inputs` are provided by the caller -- a user, another action, or a parent skill.

## Facade script convention

When an action requires multiple sequential tool calls (3 or more) that always run as a batch, wrap them in a single facade-script subcommand rather than emitting each tool call separately. The script outputs YAML so the agent can report status cleanly.

Reasons to facade:
- The N tool calls always co-occur; emitting them separately doubles the tool-call cost without efficiency win.
- Intermediate state is internal to the recipe and the user does not need to see it surface.
- A single failure mode at the script level is easier to report than N partial-failure messages.

Reasons to keep raw tool calls:
- The action is exploratory; the next step depends on the previous step's output in a way the recipe cannot pre-declare.
- One-off operations that are not part of an established recipe.

Place the facade script alongside the skill's other scripts (`scripts/<skill_name>_actions.py` or similar). All facade-script output is YAML so the agent's downstream rendering is mechanical.

## Narrate single tool calls

For operations that remain as individual tool calls -- lookups, one-off commands, exploratory queries -- always print a single line explaining what the agent is doing and why before making the call. Tool calls are opaque to the user without narration.

Example:
```
Looking up the configured retention policy.
<tool call: read_config retention.yaml>
```

vs.

```
<tool call: read_config retention.yaml>
```

The first form lets the user follow along without scrolling through tool transcripts; the second leaves the user guessing what the agent is up to.

## Worked example: build-gate pre-release check

A hypothetical capability-skill `build-gate` wraps a release-readiness check that always runs the same four operations. Encoding it as an action lets the agent execute it reliably:

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

Because all four steps run unconditionally as a batch, a facade-script form (`build_gate.py pre-release --path <build_path>` returning a single YAML report) is also legitimate. The action shape and the facade-script shape are interchangeable; pick one based on whether the user benefits from seeing per-step progress (action shape) or only the aggregated result (facade-script shape).

## Worked example: index regeneration

A hypothetical domain-skill that maintains a query-tool index over a static data source has a `regenerate_index` action:

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

## When to extend with new actions

Add a new action when (a) the recipe is invoked enough that improvising it from memory is a real failure mode, or (b) the recipe has captured-output flow between steps that an ad-hoc invocation would lose. Single-shot operations that the agent rarely runs are better served by direct tool-call invocation with narration.

When an action spans multiple sub-domains, declare it in the sub-domain that owns the primary concern and reference tools from other sub-domains by their full path. Avoid duplicating actions across sub-domains.

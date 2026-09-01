---
_schema_version: 1
name: workflow-kit
author: christina
skill-type: domain-skill
description: Use when authoring, validating, or running workflow-kit workflows. Do NOT use for one-off Workflow scripts Claude writes directly.
---

# workflow-kit

A kit of incremental, native-preserving improvements on top of the native
**Workflow tool**. Two ways in: a human authors a durable workflow as
`*.workflow.yaml` (validated and compiled to a native script), or a native
Workflow script uses workflow-kit **node strategies** to run deterministic
scripts and non-Claude (OpenRouter) model calls as cheap, context-light nodes.
Everything compiles to or runs on the native Workflow tool -- workflow-kit never
reimplements execution.

Plugin-venv interpreter (NOT `uv run python`, which resolves the venv from cwd
and breaks from a foreign project root):

- Windows: `~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/Scripts/python.exe`
- macOS/Linux: `~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/bin/python`

```yaml
domain_skill:
  _schema_version: "1"
  identity: workflow-kit owns the authoring and running of workflow-kit workflows -- declarative .workflow.yaml plus the node-strategy infrastructure (script / openrouter executors) layered on the native Workflow tool.
  companions:
    siblings: []
    note: No siblings within plugins-kit.
  scope:
    covers:
      - validating, compiling, and running a declarative .workflow.yaml via the native Workflow tool
      - building native Workflow scripts that use workflow-kit node strategies (script, openrouter)
      - the node contract (file-passing via $OUT/$STATUS) and the workflow-kit-agent executor
    excludes:
      - one-off Workflow scripts Claude writes directly (call the native Workflow tool itself)
      - executing workflows outside the native Workflow tool (there is no other runtime)
      - graph / state-machine orchestration with a custom runtime
  orientation:
    summary: |
      workflow-kit sits ON TOP of the native Workflow tool and never replaces its execution.
      Two surfaces: (1) the declarative .workflow.yaml compiler -- a human authors a workflow,
      deterministic Python compiles it to a native script (workflow-yaml.md); (2) node strategies
      -- a generically-named workflow-kit-agent executor (haiku) runs a shell command that writes
      its output to a file ($OUT) and returns only metadata, so script and OpenRouter payloads
      never bloat the model context (contract.md, node-strategies.md). This SKILL.md is an index;
      load the reference that matches the task.
    behavioral_guardrails:
      - Never run a compiled .js by hand or with node -- only the native Workflow tool executes it.
      - Node payloads travel by file ($OUT), never through the agent's context; the executor must not read or summarize $OUT.
      - The node contract is convention, lightly checked (exit code + file presence). Correctness of a command is the author's responsibility, not the executor's.
      - Nondeterministic-time and random calls are banned inside Workflow scripts (they break resume); vary per-node behavior by index or by args.
  index:
    references:
      - id: workflow-yaml
        path: references/workflow-yaml.md
        keywords: [workflow.yaml, declarative, compile, validate, scaffold, run workflow, steps, pipeline, templating, format]
        summary: The .workflow.yaml format (v1) and the compile / validate / run / scaffold procedures.
      - id: contract
        path: references/contract.md
        keywords: [node contract, OUT, STATUS, exit code, file passing, return schema, sha256, resume, context bloat]
        summary: The node contract -- file-passing via $OUT/$STATUS, the return schema, and why payloads bypass context.
      - id: node-strategies
        path: references/node-strategies.md
        keywords: [node strategy, script node, openrouter node, workflow-kit-agent, shell redirect, haiku executor, command template]
        summary: The workflow-kit-agent executor and the script / openrouter strategies -- command templates and how to wire them into a native workflow.
      - id: preamble
        path: references/preamble.js
        keywords: [preamble, inline helpers, wkScript, wkOpenRouter, wkNode, paste, node schema, hand-written workflow]
        summary: The inlinable JS preamble (paste into a native Workflow script) that builds node-strategy agent() calls.
  capabilities:
    - id: run-workflow
      keywords: [run workflow, execute workflow, compile and run, run the pipeline]
      description: Compile a .workflow.yaml and run it via the native Workflow tool.
      operation: <plugin-venv-python> scripts/compile_workflow.py <yaml> -o <out.js>; then run the compiled path via the Workflow tool
      reference_section: workflow-yaml.md
    - id: validate-workflow
      keywords: [validate workflow, check workflow, lint workflow]
      description: Validate a .workflow.yaml without compiling or running it.
      operation: <plugin-venv-python> scripts/compile_workflow.py <yaml> --validate-only
      reference_section: workflow-yaml.md
    - id: scaffold-workflow
      keywords: [new workflow, scaffold workflow, create workflow, template]
      description: Write a starter .workflow.yaml from the format.
      operation: Write <project>/.claude/workflows/<name>.workflow.yaml from the reference format, then offer to validate
      reference_section: workflow-yaml.md
    - id: build-node-strategy
      keywords: [script node, openrouter node, node strategy, deterministic step, non-claude model]
      description: Add a script or openrouter node to a native Workflow script via the preamble and the workflow-kit-agent.
      operation: inline references/preamble.js into the script; call wkScript(...) / wkOpenRouter(...)
      reference_section: node-strategies.md
  tools:
    - name: compile_workflow
      command: <plugin-venv-python> scripts/compile_workflow.py
      description: Validate and compile a .workflow.yaml to a native Workflow tool script (validate-only mode available).
    - name: openrouter_run
      command: <plugin-venv-python> scripts/openrouter_run.py
      description: One OpenRouter call via llm-scripting-kit's openai runner (make_openai_client); run with workflow-kit's own venv python (has openai declared + llm_scripting_kit via the shared-libs .pth); writes the reply to $OUT. Needs a key via llm-scripting-kit.
```

---
name: orchestrate
description: Use when accomplishing significant multi-part work -- delegate to background agents or a CLI backend to preserve context. Do NOT use for single-step tasks.
skill-type: technique-skill
---

# orchestrate

Take on the orchestrator role: accomplish significant work by delegating it to background
agents, keeping the main agent's context reserved for coordination, judgment, and synthesis.
The economics: an agent's *results* hold most of the value while *generating* them consumes
most of the context and tool calls -- so generation belongs in a background unit and only the
compressed conclusions come home. Agent-tool mechanics are already in the harness prompt every
session; what this skill adds is the economics, the procedure, and -- through the rendered
policy below -- the machine's own dispatch options and their mechanics.

**Policy is configuration, and it is rendered, not remembered.** Which tier suits which unit,
which dispatch backends exist on this machine and how to drive them, and how much usage
capacity is left all vary by user and by machine. Step 3 runs a script that prints the
resolved policy. Do not answer those questions from this file or from memory -- this file has
no tier table, deliberately. Users tune the policy by overriding
[defaults/orchestration.yaml](defaults/orchestration.yaml); see
[references/configuration.md](references/configuration.md) for the schema and layering.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Orchestrate significant work through background agents so the main agent's context holds conclusions, not work product.
  scope:
    covers:
      - decomposing significant work into delegable units and running them via background agents
      - rendering the machine's orchestration policy (tiers, backends, capacity) and dispatching by it
      - keeping the main context clean while agents run, and synthesizing results on completion
    excludes:
      - small or single-step tasks cheaper to do inline than to delegate
      - the Workflow tool's deterministic multi-agent orchestration (use Workflow when the user opts in)
      - subagent authoring (defining new agent types)

  policy:
    keywords: [model choice, model tier, backend, codex, custom orchestrator, usage limit, capacity, rate limit, configurable, override, pool]
    render: |
      Use the plugin venv's Python explicitly -- not `uv run python`, which resolves the
      venv from the cwd and misses this plugin's dependencies when run from another project
      (macOS/Linux path shown; Windows uses .venv/Scripts/python.exe):

        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python \
          ${CLAUDE_PLUGIN_ROOT}/skills/orchestrate/scripts/orchestration_guidance.py

      Add `--project-root <path>` when the project whose policy applies is not the cwd.
    emits: |
      A markdown block covering (a) a DECISION TREE -- shape, backend, tier, agent type,
      effort, announcement -- resolved by ordered elimination, first match wins, (b) every
      dispatch backend detected on this machine -- the Agent tool, Codex CLI, or whatever
      the user configured -- with its exact mechanics, capabilities and gotchas, and (c)
      best-effort usage capacity plus any manual tier overrides.
    when: |
      Once per orchestration, at step 3, before choosing tiers or launching anything. The
      script is deterministic and sub-second -- it is not a delegable unit, run it inline.
      Budget a few thousand tokens for its output; it grows with each installed backend.
    reading_it: |
      Treat the rendered block as authoritative over anything you believe about model
      lineups or dispatch mechanics. Two parts carry decisions rather than description:
      a tier marked UNAVAILABLE or LIMITED must not be dispatched to (route down a tier and
      say so when you relay results), and the tiers and backends listed are the only ones
      that exist here. Anything not installed on this machine is omitted from the output
      entirely rather than shown as unavailable, so the rendered list is exhaustive by
      construction -- do not reach for a backend or tier you remember but cannot see, and do
      not tell the user something is "unavailable" on the strength of its absence. (`--explain`
      reports what was gated and why, if you need to answer that question.)
    capacity_caveat: >-
      The rendered Capacity section states the account-wide-not-per-model limit; heed it
      there rather than restating it here. When the user knows a tier is depleted, that
      belongs in capacity.tier_overrides.

  asset_dependencies:
    - path: defaults/orchestration.yaml
      consumer: scripts/orchestration_guidance.py
      purpose: the shipped policy layer the renderer merges under the user and project overrides
      invariant: >-
        Every key under a backend's `capabilities:` is rendered from an allowlist in
        render_backends(); a key added here that is missing there is silently dropped.
    - path: references/codex-dispatch.md
      consumer: defaults/orchestration.yaml (backends[codex].dispatch)
      purpose: the flag catalog and launch mechanics the rendered summary points at
      invariant: The one-line `command:` in the backend record matches the worked example here.

  techniques:
    - id: orchestrate
      name: Orchestrate work through background agents
      keywords: [orchestrator, background agents, delegate, preserve context, fan-out, parallel agents, synthesize results]
      goal: Complete a significant task with the main context holding coordination state and conclusions, not raw work product.
      checklist:
        - "[ ] 1 warrants orchestration"
        - "[ ] 2 units decomposed and classified"
        - "[ ] 3 policy rendered"
        - "[ ] 4 backend + tier picked per unit"
        - "[ ] 5 agents launched"
        - "[ ] 6 orchestrator-only work while running"
        - "[ ] 7 results synthesized"
        - "[ ] 8 substance relayed to user"
      steps:
        - n: 1
          action: Confirm the task warrants orchestration.
          detail: |
            Delegate by persistent context footprint, not difficulty or duration. Significant =
            multiple independent work units, or units that read a lot / emit a lot (bulk reads,
            diffs, logs, drafts) relative to their conclusions. One small self-contained unit whose
            result feeds the very next decision stays inline -- an agent round-trip there costs as
            much context as the work. Classify by shape in one glance; if classifying takes more
            thought than that, treat it as delegate-shaped. A per-unit "how expensive will this be"
            estimate is itself the context spend this rule avoids.
        - n: 2
          action: Decompose into self-contained units and classify each.
          detail: |
            Per unit note (a) dependencies -- independent units run in parallel, dependent ones
            sequence; (b) compression profile -- does the result compress to a small conclusion?
            High-generation-cost / small-conclusion units are the ideal delegations.
        - n: 3
          action: Render the orchestration policy by running the script in the policy block above.
          detail: >-
            Run it once, inline, and keep the output in view for steps 4-5. It is the source
            of truth for tiers, backends and capacity on this machine.
        - n: 4
          action: Pick a backend per unit, then a tier from that backend's ladder.
          detail: >-
            Backend first: the rendered policy gives one tier ladder per backend, and rungs
            are comparable only within a ladder -- across them the decision is dispatch
            shape, pool, and independence, not model capability. Default backend and default
            tier are stated in the output; deviate per unit when the unit's shape argues for
            it. Honour UNAVAILABLE/LIMITED markings.
        - n: 5
          action: Launch background units -- each prompt a standalone brief (goal, paths, constraints, return shape).
          detail: >-
            Use the launch mechanics the rendered policy gives for the chosen backend; they
            differ materially between backends (a CLI backend has no built-in isolation or
            completion report). Launch independent units in one message so they run
            concurrently.
        - n: 6
          action: While units run, do orchestrator-level work only (plan synthesis, inline units, or wait).
        - n: 7
          action: Synthesize completed results; cross-check units that disagree before accepting either.
          detail: >-
            A report describes what the unit intended, not necessarily what it did -- verify
            file-writing units against the actual diff before treating the work as done.
        - n: 8
          action: Relay the substance -- findings, decisions, verified-vs-reported -- in your final message.
      gotchas:
        - Delegating then redoing the work inline pays both costs; once dispatched, wait for the result.
        - Parallel units editing the same files clobber each other -- use isolation appropriate to the backend, or sequence them.
        - Backends differ in what they can do (isolation, effort, network, tier selection); read the rendered capabilities before assuming parity with the Agent tool.

  anti_patterns:
    - id: top_tier_everywhere
      name: Top-tier agents by default
      keywords: [fable default, model overkill, expensive fan-out]
      why_it_seems_right: The best model should give the best results on every unit.
      why_it_is_wrong: Most delegated units are workhorse-shaped; top-tier agents spend the premium pool on work that doesn't need it.
      alternative: Take the default tier from the rendered policy and escalate per unit against its stated criteria.
    - id: remembered_policy
      name: Choosing models and backends from memory
      keywords: [skipped the script, hardcoded tier table, stale model names, assumed agent tool, ignored override]
      why_it_seems_right: The tier lineup and dispatch mechanics feel like stable background knowledge, so running a script to restate them looks like ceremony.
      why_it_is_wrong: >-
        The policy is per-user and per-machine: a user may have retargeted tiers, added a
        backend this skill has never heard of, disabled one, or marked a tier unavailable
        because its usage is spent. Answering from memory silently ignores every one of
        those and produces confident dispatch to something that is wrong or gone.
      alternative: Run the policy script at step 3; it is deterministic and sub-second.
    - id: orchestrator_does_the_work
      name: Orchestrator absorbs the work product
      keywords: [context bloat, reading everything, inline generation]
      why_it_seems_right: Reading all the raw output yourself feels more thorough than trusting summaries.
      why_it_is_wrong: It defeats the entire point -- the main context fills with generation-cost material whose value was already captured in the agents' conclusions.
      alternative: Ask agents for structured conclusions; pull raw detail only for the specific items you must verify or that agents disagreed on.
    - id: inline_footprint_work
      name: Doing reads-a-lot / emits-a-lot work inline in the orchestrating context
      keywords: [inline work, context footprint, quick edit, difficulty axis]
      why_it_seems_right: "It's quick, I'm already here, and dispatching an agent costs a prompt and a relay."
      why_it_is_wrong: Sessions run hundreds of messages; every file read and diff emitted inline stays in the orchestrating context for all of them. Difficulty and duration are the wrong axis -- persistent context footprint is.
      alternative: Classify by shape in one glance (step 1); reads-a-lot or emits-a-lot goes to a background agent even when it is easy.
    - id: vague_dispatch
      name: Under-specified agent prompts
      keywords: [vague prompt, missing context, wrong question]
      why_it_seems_right: The task is obvious from the conversation, so a one-liner should do.
      why_it_is_wrong: The agent never saw the conversation; it fills gaps with guesses and returns polished answers to a different question.
      alternative: Write each prompt as a standalone brief -- goal, paths, constraints, and the exact shape of the answer you want back.
```

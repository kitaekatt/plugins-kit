---
name: orchestrate
description: Use when accomplishing significant multi-part work -- delegate to background agents or a CLI backend to preserve context. Do NOT use for single-step tasks.
skill-type: technique-skill
---

# orchestrate

Take on the orchestrator role: accomplish significant work by delegating it to background
agents, keeping the main agent's context reserved for coordination, judgment, and synthesis.

See [references/why-delegate.md](references/why-delegate.md) for the economics behind this
procedure and the anti-pattern catalogue.

**Policy is configuration, and it is rendered, not remembered.** Which tier suits which unit,
which dispatch backends exist on this machine and how to drive them, and how much usage
capacity is left all vary by user and by machine. Step 2 runs a script that prints the
resolved policy. Do not answer those questions from this file or from memory -- this file has
no tier table, deliberately. Users tune the policy by overriding
[defaults/orchestration.yaml](defaults/orchestration.yaml); see
[references/configuration.md](references/configuration.md) for the schema and layering,
and [references/tuning-selection.md](references/tuning-selection.md) when a rung is
firing more or less often than you want -- which keys move selection frequency, which
way, and how far. The decision tree itself is compiled from a controlled vocabulary,
[references/lexicon.md](references/lexicon.md), against a maintainer-only criteria
source that is not part of this install.

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
      Once per orchestration, at step 2, BEFORE decomposing or planning anything -- the
      policy's shaping tests govern the plan itself, so rendering after the plan exists
      arrives too late to route its creation. Run it inline, not as a delegable unit.
      Budget a few thousand tokens for its output; it grows with each installed backend.
    reading_it: |
      Treat the rendered block as authoritative over anything you believe about model
      lineups or dispatch mechanics: a tier marked UNAVAILABLE or LIMITED must not be
      dispatched to (route down a tier and say so when you relay results), and the tiers
      and backends listed are the only ones that exist here. Anything not installed on this
      machine is omitted from the output entirely rather than shown as unavailable, so do
      not reach for a backend or tier you remember but cannot see, and do not tell the user
      something is "unavailable" on the strength of its absence. (`--explain` reports what
      was gated and why, if you need to answer that question.)

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
      steps:
        - n: 1
          action: Confirm the task warrants orchestration.
          detail: |
            Delegate by persistent context footprint, not difficulty or duration. Significant =
            multiple independent work units, or units that read a lot / emit a lot (bulk reads,
            diffs, logs, drafts) relative to their conclusions. One small self-contained unit whose
            result feeds the very next decision stays inline -- an agent round-trip there costs as
            much context as the work. Classify by shape in one glance; if classifying takes more
            thought than that, treat it as delegate-shaped.
        - n: 2
          action: Render the orchestration policy by running the script in the policy block above.
          detail: >-
            Run it once, inline, BEFORE decomposing -- its shaping tests govern the plan
            itself, and rendering after the plan exists can only trigger a retrospective
            review, never route the plan's creation. Keep the output in view for steps 3-5;
            it is the source of truth for tiers, backends and capacity on this machine.
        - n: 3
          action: Decompose into self-contained units and classify each -- the plan itself is the first candidate unit.
          detail: |
            The decomposition you are about to author is a unit (the policy's plan-checkpoint
            shaping tests): route it through the rendered tree before briefing anything from
            it, which may mean delegating its creation, or authoring it and delegating its
            review. Then per unit note (a) dependencies -- independent units run in parallel,
            dependent ones sequence; (b) compression profile -- does the result compress to a
            small conclusion? High-generation-cost / small-conclusion units are the ideal
            delegations.
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
          detail: |
            Use the launch mechanics the rendered policy gives for the chosen backend; they
            differ materially between backends (a CLI backend has no built-in isolation or
            completion report). Launch independent units in one message so they run
            concurrently. The return shape must require disclosure of any critical
            infrastructure the unit created, moved, retired, or changed -- generated
            artifacts and their generators, build/commit-time gates, load-bearing paths other
            code or docs resolve against, or the only remaining link/reference to something
            the unit just removed.
        - n: 6
          action: While units run, do orchestrator-level work only (plan synthesis, inline units, or wait).
        - n: 7
          action: Synthesize completed results; cross-check units that disagree before accepting either.
          detail: |
            Synthesize from the reports; pull raw output into this context only for the items
            you must verify or that units disagreed on. A report describes what the unit
            intended, not necessarily what it did -- verify file-writing units against the
            actual diff before treating the work as done. A disclosed critical-infrastructure
            change gets recorded in the appropriate CLAUDE.md as part of this synthesis --
            it must not be left sitting only in the agent's report, which the user never sees.
            Reverting a recorded change later is the responsibility of whichever agent
            decides to reverse it; the record is a signal of intent, not a prohibition.
        - n: 8
          action: Relay the substance -- findings, decisions, verified-vs-reported -- in your final message.
      gotchas:
        - Delegating then redoing the work inline pays both costs; once dispatched, wait for the result.
        - Parallel units editing the same files clobber each other -- use isolation appropriate to the backend, or sequence them.
        - A unit that correctly removes or relocates something can silently destroy the only signpost pointing at it -- a green result and a clean diff will not surface that; only the unit's own disclosure does.
```

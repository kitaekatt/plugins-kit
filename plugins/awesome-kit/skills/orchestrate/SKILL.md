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

**Policy is configuration, and it is rendered, not remembered.** Which routing row suits a
unit, which model entries and Agent-tool models are available, which dispatch backends exist
on this machine and how to drive them, and how much usage capacity is left all vary by user
and by machine. Step 2 runs a script that prints the resolved policy. Do not answer those
questions from this file or from memory. Users tune the policy by overriding
[defaults/orchestration.yaml](defaults/orchestration.yaml); see
[references/configuration.md](references/configuration.md) for the schema and layering,
and [references/tuning-selection.md](references/tuning-selection.md) when a routing row is
firing more or less often than you want. The routing policy is hand-written configuration
stated in the controlled vocabulary of [references/lexicon.md](references/lexicon.md).

```yaml
technique_skill:
  _schema_version: "1"
  identity: Orchestrate significant work through background agents so the main agent's context holds conclusions, not work product.
  scope:
    covers:
      - decomposing significant work into delegable units and running them via background agents
      - rendering the machine's orchestration policy (routing, backends, capacity) and dispatching by it
      - keeping the main context clean while agents run, and synthesizing results on completion
    excludes:
      - small or single-step tasks cheaper to do inline than to delegate
      - the Workflow tool's deterministic multi-agent orchestration (use Workflow when the user opts in)
      - subagent authoring (defining new agent types)

  policy:
    keywords: [model choice, model routing, backend, codex, custom orchestrator, usage limit, capacity, rate limit, configurable, override, pool]
    render: |
      Use the plugin venv's Python explicitly -- not `uv run python`, which resolves the
      venv from the cwd and misses this plugin's dependencies when run from another project
      (macOS/Linux path shown; Windows uses .venv/Scripts/python.exe):

        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python \
          ${CLAUDE_PLUGIN_ROOT}/skills/orchestrate/scripts/orchestration_guidance.py

      Add `--project-root <path>` when the project whose policy applies is not the cwd.
    emits: |
      A markdown block covering (a) a DECISION TREE -- shape, routing, agent type, effort,
      announcement -- resolved by ordered elimination, first match wins, (b) every
      dispatch backend detected on this machine -- the Agent tool, Codex CLI, or whatever
      the user configured -- with its exact mechanics, capabilities and gotchas, and (c)
      best-effort usage capacity.
    when: |
      Once per orchestration, at step 2, BEFORE decomposing or planning anything -- the
      policy's shaping tests govern the plan itself, so rendering after the plan exists
      arrives too late to route its creation. Run it inline, not as a delegable unit.
      Budget a few thousand tokens for its output; it grows with each installed backend.
    reading_it: |
      Treat the rendered block as authoritative over anything you believe about model
      lineups or dispatch mechanics: a model or harness absent from the rendered policy must
      not be dispatched to. A routing row falls through to its next model on a launch or
      transport error. The model entries and backends listed are the only ones that exist
      here. Anything not installed on this machine is omitted from the output entirely, so do
      not reach for a backend or model you remember but cannot see, and do not tell the user
      something is "unavailable" on the strength of its absence. (`--explain` reports what
      was skipped and why, if you need to answer that question.)

      Being LISTED is not the same as being ELIGIBLE. A backend whose block opens with a
      `**Selection.**` line is not a routing target: it is documented so you can drive it
      correctly when its stated condition holds, and it is off the table otherwise. Route
      as though it were absent until that condition is met -- typically the user naming it.

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
            Delegate for one of two reasons: footprint -- the unit reads or emits far more than
            its conclusion -- or parallelism -- the rendered razor yields at least two leaves
            runnable now. Neither means do it inline. One small self-contained unit whose result
            feeds the next decision stays inline; an agent round-trip costs as much context as the
            work. Difficulty and indecision are not reasons to delegate.
        - n: 2
          action: Render the orchestration policy by running the script in the policy block above.
          detail: >-
            Run it once, inline, BEFORE decomposing -- its shaping tests govern the plan
            itself, and rendering after the plan exists can only trigger a retrospective
            review, never route the plan's creation. Keep the output in view for steps 3-5;
            it is the source of truth for routing, backends and capacity on this machine.
        - n: 3
          action: Decompose into self-contained units, apply the rendered parallel-development razor, and classify each -- the plan itself is the first candidate unit.
          detail: |
            The decomposition you are about to author is a unit (the policy's plan-checkpoint
            shaping tests): route it through the rendered tree before briefing anything from
            it, which may mean delegating its creation, or authoring it and delegating its
            review. A delegated plan-creation brief includes the rendered parallel-development
            razor and returns candidate leaves labelled against each of its tests. Then per unit
            note (a) dependencies; (b) whether the razor admits it as a parallel leaf; and (c)
            compression profile -- does the result compress to a small conclusion?
            High-generation-cost / small-conclusion units are the ideal footprint delegations.
        - n: 4
          action: Match the unit to the rendered routing rows and choose the first available model.
          detail: >-
            Evaluate routing rows in declaration order. A row's shape must match the unit;
            its models are tried in declaration order, and a launch or transport error falls
            through to the next model in that row. An unresolvable model or an unavailable
            harness removes that model, and a row with no surviving models disappears. A
            backend carrying a `**Selection.**` restriction is documented for its stated
            condition and is not a routing target. When the user names a backend or model,
            that names the dispatch: take the named one and skip this step.
        - n: 5
          action: Launch background units -- each prompt a standalone brief (goal, paths, constraints, premises, return shape).
          detail: |
            Use the launch mechanics the rendered policy gives for the chosen backend; they
            differ materially between backends (a CLI backend has no built-in isolation or
            completion report). Launch every admitted leaf on the current dependency frontier in
            one message. A leaf whose dependency has not completed waits for the next frontier.
            The return shape must require disclosure of any critical
            infrastructure the unit created, moved, retired, or changed -- generated
            artifacts and their generators, build/commit-time gates, load-bearing paths other
            code or docs resolve against, or the only remaining link/reference to something
            the unit just removed.

            PREMISES, MARKED. The other four fields ask only whether a brief is executable, so
            a confidently wrong brief satisfies all of them. State every load-bearing premise
            and label it `established:` (naming the evidence -- a trace, a diff, a prior unit's
            verified report) or `hypothesis:`. A causal mechanism you inferred, a claim that
            existing work functions, and an inherited parameter you did not derive are all
            hypotheses until evidence is cited. Marking is a classification you perform at
            authoring time; that act is the point, and it is what the field list never asked
            for. Do not grade confidence numerically -- the label is binary.

            A HYPOTHESIS MAY FUND AN INVESTIGATION, NEVER A CHANGE OR A DEPLOYMENT. To brief a
            mutation on a premise, promote it with evidence first, or split the unit: establish,
            then change. Where the two must ride together, say so and require the agent to
            check the premise before its first mutation.

            The return shape must require the agent to report each premise as confirmed,
            refuted or untested, and a REFUTED premise halts the work and reports instead of
            proceeding. This is the half that binds: you will mislabel a guess as a fact, and
            the far side of the dispatch is where that gets caught before it ships.

            Scope a verification unit by what the change under test actually touches -- its
            behavioral effects plus any named shared dependency -- never by the subject area it
            lives in. "Verify the device" re-tests everything the device does; "verify the
            launch path" tests the change. And derive temporal parameters (sampling windows,
            settle times) from the failure being chased, stating the basis; an interval
            inherited from other work is a hypothesis wearing a number.
        - n: 6
          action: While units run, do orchestrator-level work only (plan synthesis, inline units, or wait) -- and keep running units current.
          detail: >-
            A constraint that changes while units run does not reach them by itself. Enumerate
            the affected running units and push the delta through the backend's follow-up
            channel (SendMessage for the Agent tool); a unit with no such channel is cancelled
            and relaunched, or its result treated as pre-change and re-verified. Say which you
            did. Silently letting a unit finish against a superseded constraint spends it twice.
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
        - Parallel units editing the same files clobber each other -- but a shared-file conflict is a PARTITIONING problem before it is a scheduling one. Re-split the work by file ownership first (one owner per file, stated in each brief), then use isolation appropriate to the backend, and sequence only what genuinely remains. Reaching for sequencing first serialises work that had no real dependency.
        - A unit that correctly removes or relocates something can silently destroy the only signpost pointing at it -- a green result and a clean diff will not surface that; only the unit's own disclosure does.
```

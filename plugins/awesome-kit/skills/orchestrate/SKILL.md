---
name: orchestrate
description: Use when accomplishing significant multi-part work -- delegate to background agents or a CLI backend to preserve context. Do NOT use for single-step tasks.
skill-type: technique-skill
---

# orchestrate

Take on the orchestrator role: accomplish significant work by delegating it to background
agents, keeping the main agent's context reserved for coordination, judgment, and synthesis.

**Autonomy is high, and there is one level.** What authorizes the orchestrator is the task the
user set and the authorizations their instructions record -- the CLAUDE.md files on the path,
which it can open. Inside that scope it decides, dispatches, verifies, commits, and reports,
and it does not end a turn proposing work it could do or confirming a call it could make. Four
edges stop it and nothing else does: a `mutating` effect no instruction authorizes -- a push, a
deploy, a message to a third party; an action the user has gated, such as a publish; a
directional question -- what the product is for, what it becomes, whether a thing gets built at
all; and a standing prohibition in those same instructions. A call inside sanctioned work is
not direction, however product-flavoured it is. Everything short of an edge is a decision, and
a decision is a unit: step 3 routes it, step 7 binds its ruling, and the routing is itself a
call with no edge -- make it without stalling. The user adjusts the default in conversation or,
durably, by writing a gate into their instructions; do not re-derive a level, and do not invent
intermediate ones.

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
      - routing a decision the orchestrator would otherwise put to the user, and the autonomy edges that decide when the user is asked at all
    excludes:
      - small or single-step tasks cheaper to do inline than to delegate
      - the Workflow tool's deterministic multi-agent orchestration (use Workflow when the user opts in)
      - subagent authoring (defining new agent types)
      - reviewer fan-out internal to an invoked review skill (N reviewers over one artifact) -- that skill's `SKILL.md` owns its reviewer roster and lane arithmetic; orchestrate still owns and routes the plan-checkpoint cross-check as a separate unit

  policy:
    keywords: [model choice, model routing, backend, codex, custom orchestrator, usage limit, capacity, rate limit, configurable, override, pool, consult seat, independent seat, who to ask, --self, UP, BESIDE]
    render: |
      Use the plugin venv's Python explicitly -- not `uv run python`, which resolves the
      venv from the cwd and misses this plugin's dependencies when run from another project
      (macOS/Linux path shown; Windows uses .venv/Scripts/python.exe):

        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python \
          ${CLAUDE_PLUGIN_ROOT}/skills/orchestrate/scripts/orchestration_guidance.py \
          --self <your endpoint alias>

      Add `--project-root <path>` when the project whose policy applies is not the cwd.
      Pass the registry alias for your own model (`fable`, `opus`, `sonnet`) -- the agent
      knows its model from the system prompt; exact model ids also resolve.
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

      The Consult seats section is who to ask; take the first UP seat, else the first BESIDE.

      Being LISTED is not the same as being ELIGIBLE. A backend whose block opens with a
      `**Selection.**` line is not a routing target: it is documented so you can drive it
      correctly when its stated condition holds, and it is off the table otherwise. Route
      as though it were absent until that condition is met -- typically the user naming it.

  asset_dependencies:
    - path: defaults/orchestration.yaml
      consumer: scripts/orchestration_guidance.py
      purpose: the shipped policy layer the renderer merges under the machine, user, and project overrides
      invariant: >-
        Every key under a backend's `capabilities:` is rendered from an allowlist in
        render_backends(); a key added here that is missing there is silently dropped.
    - path: references/codex-dispatch.md
      consumer: defaults/orchestration.yaml (backends[codex].dispatch)
      purpose: the flag catalog and launch mechanics the rendered summary points at
      invariant: The one-line `command:` in the backend record matches the worked example here.
    - path: references/opencode-dispatch.md
      consumer: defaults/orchestration.yaml (backends[opencode].dispatch)
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
            work. Difficulty and indecision are not reasons to delegate. In `user-present` (the user is watching the prompt) footprint bites harder: prefer the background for anything past one cheap foreground call.
        - n: 2
          action: Render the orchestration policy by running the script in the policy block above.
          detail: >-
            Run it once, inline, BEFORE decomposing -- its shaping tests govern the plan
            itself, and rendering after the plan exists can only trigger a retrospective
            review, never route the plan's creation. Keep the output in view for steps 3-5;
            it is the source of truth for routing, backends and capacity on this machine.
        - n: 3
          action: Decompose into self-contained units, apply the rendered parallel-development razor, and classify each -- the plan itself is the first candidate unit, and every decision you would otherwise put to the user is another.
          detail: |
            The decomposition you are about to author is a unit (the policy's plan-checkpoint
            shaping tests): route it through the rendered tree before briefing anything from
            it, which may mean delegating its creation, or authoring it and delegating its
            review. A delegated plan-creation brief includes the rendered parallel-development
            razor and returns candidate leaves labelled against each of its tests. Then per unit
            note (a) dependencies; (b) whether the razor admits it as a parallel leaf; and (c)
            compression profile -- does the result compress to a small conclusion?
            High-generation-cost / small-conclusion units are the ideal footprint delegations.

            A decision you would otherwise put to the user is a unit of the same kind: work is
            briefed from it, so it is a plan-checkpoint. Make the call first, then route the
            call -- not the question -- through the rendered tree by its own terms, exactly as
            the plan; a ruling that comes back stands (step 7).
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
            one message. A leaf whose dependency has not completed is not admitted. When the
            runnable frontier changes, re-apply the rendered `parallel-development-razor` from
            `defaults/orchestration.yaml` before briefing additional leaves.
            The return shape must require disclosure of any critical
            infrastructure the unit created, moved, retired, or changed -- generated
            artifacts and their generators, build/commit-time gates, load-bearing paths other
            code or docs resolve against, or the only remaining link/reference to something
            the unit just removed.

            RETURN BUDGET, NAMED. The default report is at most about 1,000 tokens and
            contains only the disposition, files changed, premise outcomes, named checks,
            and blockers. Put logs, inventories, source excerpts, and other bulky evidence
            in an artifact. Return its path and the exact parts the join must inspect.
            If the join needs named detail that cannot remain in an artifact, raise the
            budget in the brief. Otherwise, keep the default.

            PREMISES, MARKED. The other four fields ask only whether a brief is executable, so
            a confidently wrong brief satisfies all of them. State every load-bearing premise
            and label it `established:` (naming the evidence -- a trace, a diff, a prior unit's
            verified report) or `hypothesis:`. A causal mechanism you inferred, a claim that
            existing work functions, and an inherited parameter you did not derive are all
            hypotheses until evidence is cited. Marking is a classification performed at
            authoring time -- do not grade confidence numerically; the label is binary.

            EXCLUSIONS HAVE TWO PARTS. A prohibition imposed by the user, repository policy,
            or an authorization boundary is a constraint. The unit never crosses it. A claim
            that an excluded path, component, or artifact does not depend on the change is a
            causal premise. Mark it like any other premise.

            When a mutation removes, moves, or renames an interface, path, or generated
            artifact, establish each causal scope boundary. Name the dependency check before
            dispatch. Run the check inbound: grep the excluded area for references to what is
            being changed. If the check has not run, mark the boundary `hypothesis:`
            and require the unit to check it before its first mutation. A dependency that
            crosses a protected boundary halts the unit and reports the conflict. It does not
            grant authority to edit the excluded area.

            A HYPOTHESIS MAY FUND AN INVESTIGATION, NEVER A CHANGE OR A DEPLOYMENT. To brief a
            mutation on a premise, promote it with evidence first, or split the unit: establish,
            then change. Where the two must ride together, say so and require the agent to
            check the premise before its first mutation.

            The return shape must require the agent to report each premise as confirmed,
            refuted or untested, and a REFUTED premise halts the work and reports instead of
            proceeding. This is the half that binds: you will mislabel a guess as a fact, and
            the far side of the dispatch is where that gets caught before it ships.

            Scope a verification unit by what the change under test actually touches (its
            behavioral effects plus any named shared dependency), never by the subject area it
            lives in -- "verify the launch path", not "verify the device". Derive temporal
            parameters (sampling windows, settle times) from the failure being chased, stating
            the basis; an interval inherited from other work is a hypothesis wearing a number.
        - n: 6
          action: While units run, do orchestrator-level work only -- plan synthesis, inline units, or nothing -- and keep running units current.
          detail: >-
            Waiting is passive. A background task re-invokes the session when it exits. If no
            useful unblocked work remains, end the turn. Waiting is correct in that state. Do
            not invent work to avoid being idle. Never sleep, poll, or re-read an `-o` file
            before completion.

            A constraint that changes while units run does not reach them by itself. Enumerate
            the affected running units and push the delta through the backend's follow-up
            channel (SendMessage for the Agent tool); a unit with no such channel is cancelled
            and relaunched, or its result treated as pre-change and re-verified. Say which you
            did. Silently letting a unit finish against a superseded constraint spends it twice.
            Apply the rendered Review overlap policy from `defaults/orchestration.yaml` when a
            review overlaps candidate units.
        - n: 7
          action: Synthesize completed results; cross-check units that disagree before accepting either.
          detail: |
            Synthesize from the reports; pull raw output into this context only for the items
            you must verify or that units disagreed on. A report describes what the unit
            intended, not necessarily what it did -- verify file-writing units against the
            actual diff before treating the work as done. If output fails verification while
            CONFORMING to its brief, the defect is in the brief OR in the check: validate the
            failing check before changing either. If the check is sound and the brief is
            defective, correct the specification and route that correction as its own unit (the
            plan-checkpoint tests in `defaults/orchestration.yaml` apply), rather than
            relaunching the worker. A wrong decision
            MAY affect sibling briefs cut from the same decomposition, so re-check them after
            confirming the brief is defective. A disclosed critical-infrastructure
            change gets recorded in the appropriate CLAUDE.md as part of this synthesis --
            it must not be left sitting only in the agent's report, which the user never sees.
            Reverting a recorded change later is the responsibility of whichever agent
            decides to reverse it; the record is a signal of intent, not a prohibition.

            A ruling belongs to the seat that made it. A review verdict or cross-check finding
            you disagree with is neither set aside here nor re-asked of another seat: put the
            counter-argument to the seat that ruled -- the backend's follow-up channel (step
            6), or a relaunch of the same seat with the counter-argument in the brief -- and
            take what comes back. Two seats that disagree each see the other's argument once;
            the primary, the seat the matched row named first, then rules. Accepting a risk a
            reviewer rated against is a ruling, and the reviewer's to make.

            Record one machine-readable join line for every completed unit:

            `join <unit-id>: disposition=<accepted|corrected|rejected>; cause=<worker|brief|changed-constraint|integration|unknown>; verified=<named check>`

            `accepted` means the returned artifact passed the named check without a lead-side
            correction. `corrected` means the lead changed it before acceptance. `rejected`
            means none of the returned work was accepted. Disposition and cause are separate.
            Do not use a correction caused by a bad brief as evidence against the worker.

            The dispatch announcement and brief must use the same `<unit-id>` and record the
            target, routing terms, and effort. If an active task folder exists, copy only a
            corrected or rejected outcome whose reason could re-bite a fresh agent into
            `log.md`. Accepted outcomes remain transcript telemetry.
        - n: 8
          action: Relay the substance -- findings, decisions, verified-vs-reported -- in your final message.
      gotchas:
        - >-
          Delegating and then re-doing the same unit inline pays both costs. Once
          dispatched, do not re-do that unit here. Independent work is not blocked
          by it. Waiting for it is passive (step 6).
        - Parallel units editing the same files clobber each other -- but a shared-file conflict is a PARTITIONING problem before it is a scheduling one. Re-split the work by file ownership first (one owner per file, stated in each brief), then use isolation appropriate to the backend, and sequence only what genuinely remains. Reaching for sequencing first serialises work that had no real dependency.
        - A unit that correctly removes or relocates something can silently destroy the only signpost pointing at it -- a green result and a clean diff will not surface that; only the unit's own disclosure does.
        - >-
          Codex dispatch results have durable handles and can be recovered after a restart; see
          [references/codex-dispatch.md](references/codex-dispatch.md) "Dispatch cache". Pass
          the dispatch script's `--no-cache` option when a fresh run is required.
```

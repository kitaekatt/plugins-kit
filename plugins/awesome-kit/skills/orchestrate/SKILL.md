---
name: orchestrate
description: Use when orchestrating work through background agents or CLI, including another skill's invocation, regardless of task size. Do NOT use to author agent types.
skill-type: technique-skill
---

# orchestrate

Take on the orchestrator role whenever this skill is invoked, directly or through a
user-requested workflow such as `$task`. Keep the main agent's context for coordination,
judgment, verification, and synthesis. Delegate every implementation unit, including code,
tests, scripts, configuration, and documentation. A small or unsplittable change is one
delegated unit. Planning venue never licenses main-thread implementation.

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

**Do not delegate when:** user steering, authorization, policy rendering and routing, final
acceptance, and synthesis belong to the main thread. The main thread plans when BOTH it is
the best suited planning model under rendered policy AND it has sufficient verified,
decision-relevant context; otherwise delegate planning. For optional coordination or read-only
investigation, keep the unit here only when BOTH the main model is best suited AND briefing,
dispatch, waiting, joining, verifying, and correcting would cost more than doing it here.
These cases do not permit main-thread implementation. If no eligible worker can implement
to the required quality, report the capability or quality gap and leave that work pending.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Orchestrate invoked work through background agents so the main agent's context holds conclusions, not work product.
  scope:
    covers:
      - decomposing invoked work into delegable units and running implementation via background agents
      - rendering the machine's orchestration policy (routing, backends, capacity) and dispatching by it
      - keeping the main context clean while agents run, and synthesizing results on completion
      - routing a decision the orchestrator would otherwise put to the user, and the autonomy edges that decide when the user is asked at all
    excludes:
      - subagent authoring (defining new agent types)
      - reviewer fan-out internal to an invoked review skill (N reviewers over one artifact) -- that skill's `SKILL.md` owns its reviewer roster and lane arithmetic; orchestrate still owns and routes the plan-checkpoint cross-check as a separate unit

  policy:
    keywords: [model choice, model routing, backend, codex, opencode, custom orchestrator, usage limit, capacity, rate limit, configurable, override, pool, consult seat, independent seat, who to ask, --self, UP, BESIDE]
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
      invariant: >-
        The one-line `command:` in the backend record matches the ARGV-only worked-example
        line here (the one under "backend record's one-line command is:"), not the
        redirect-completed shape below it -- an argv list cannot express a shell redirect.
    - path: references/opencode-dispatch.md
      consumer: defaults/orchestration.yaml (backends[opencode].dispatch)
      purpose: the flag catalog and launch mechanics the rendered summary points at
      invariant: >-
        The one-line `command:` in the backend record matches the ARGV-only worked-example
        line here (the one under "backend record's one-line command is:"), not the
        redirect-completed shape below it -- an argv list cannot express a shell redirect.
  techniques:
    - id: orchestrate
      name: Orchestrate work through background agents
      keywords: [orchestrator, background agents, delegate, preserve context, fan-out, parallel agents, synthesize results]
      goal: Complete an invoked task with the main context holding coordination state and conclusions, not raw work product.
      steps:
        - n: 1
          action: Apply the one orchestration contract to the invoked task.
          detail: |
            Direct invocation and invocation through another user-requested workflow have the
            same effect. Delegate implementation even when it is one short, self-contained unit.
            The main thread may make bounded read-only checks for routing, planning context, and
            the join. Compare model fit and total delegation cost for OPTIONAL coordination or
            read-only investigation only. For mandatory implementation, use that cost to choose
            an eligible worker, one compact unit when splitting adds overhead, and a tight return
            contract. If the main model is best suited to implement, dispatch a background
            instance of it when eligible; otherwise choose the best eligible worker and make the
            brief and verification proportionate.
        - n: 2
          action: Render the orchestration policy by running the script in the policy block above.
          detail: >-
            Run it once, inline, BEFORE decomposing -- its shaping tests govern the plan
            itself, and rendering after the plan exists can only trigger a retrospective
            review, never route the plan's creation. Keep the output in view for steps 3-5;
            it is the source of truth for routing, backends and capacity on this machine.
        - n: 3
          action: Place planning by model fit and context, then decompose and apply the rendered parallel-development razor.
          detail: |
            After policy render, identify the best suited planning model under the rendered
            routing rows and the plan's actual demands. Separately check whether this context
            has sufficient verified, decision-relevant user rulings, constraints, repository
            facts, interfaces, dependencies, and load-bearing premises. If BOTH the main model
            is best suited and that context is sufficient, author the plan here. If EITHER is
            false, delegate planning to the most appropriate eligible model. A few bounded
            read-only checks may close a small context gap; substantial investigation travels
            with the planning unit, possibly to a background instance of the main model when
            it is best suited and eligible. A requested plan deliverable follows the same test.
            Carry known context and exact user rulings in the planning brief. Name unknowns as
            `hypothesis:`; require the planner to establish them with evidence or report a
            blocker before implementation briefs rely on them. Check the returned plan's
            premises and fit to user intent; send needed revisions back to the planner rather
            than silently rewriting it. A delegated planning brief includes the rendered
            parallel-development razor and returns candidate units, dependencies, premise
            evidence, verification boundaries, and blockers. Apply the plan-checkpoint review
            route before implementation dispatch.

            For each unit, note (a) dependencies; (b) whether the razor admits it as a parallel leaf; and (c)
            compression profile -- does the result compress to a small conclusion?
            High-generation-cost / small-conclusion read-only units are strong optional
            footprint delegations.

            ORDER BY VALUE, NOT BY ARCHITECTURE. A decomposition comes out in
            dependency order by default, which puts the foundation first and the
            payoff last. Before briefing from it, ask which single unit most
            directly delivers the goal and what it ACTUALLY depends on -- often
            less than its position implies. Promote that unit. The check is cheap
            and the payoff is asymmetric: a plan whose first increment delivers
            the goal can be abandoned at any later point and still leave things
            better, while one ordered by structure is all cost until the end.
            State plainly what the promoted unit does NOT cover, so stopping
            early is an informed choice rather than a surprise.

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
            condition and is not a routing target. When the user names an eligible backend
            or model, it selects the dispatch target, not planning venue or implementation
            permission. If no eligible worker remains, report the capability gap.
        - n: 5
          action: Launch every delegated unit -- each prompt a standalone brief (goal, paths, constraints, premises, return shape).
          detail: |
            Use the launch mechanics the rendered policy gives for the chosen backend; they
            differ materially between backends (a CLI backend has no built-in isolation or
            completion report). If the razor does not admit a parallel split, launch one coherent
            delegated implementation unit. Launch admitted parallel leaves on the current
            dependency frontier in one message. A leaf whose dependency has not completed is
            not admitted. Once dispatched, each worker owns its files until the join; the main
            thread may read them but sends any work-product correction back to a worker. When the
            runnable frontier changes, re-apply the rendered `parallel-development-razor` from
            `defaults/orchestration.yaml` before briefing additional leaves.
            Each brief names the goal, paths and exclusive file ownership, constraints,
            premises and dependency checks, named verification, authorized side effects,
            and return shape.
            The return shape must require disclosure of any critical
            infrastructure the unit created, moved, retired, or changed -- generated
            artifacts and their generators, build/commit-time gates, load-bearing paths other
            code or docs resolve against, or the only remaining link/reference to something
            the unit just removed.

            RETURN BUDGET, NAMED. The default report is at most about 1,000 tokens and
            contains only the disposition, changed-file list or diff stat, material decisions
            and reasons, premise outcomes, named checks and results, blockers, and critical
            infrastructure changed. Put logs, inventories, source excerpts, and other bulky evidence
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

            A USER'S RULING GOES IN VERBATIM. When a brief rests on a decision the user
            made -- a constraint they set, a question they answered, a scope they fixed --
            quote their words, and do not restate the ruling anywhere else in the brief. A
            restatement is authored by you, so it carries your reading of it, and it runs
            WIDE: it generalizes a specific answer into a principle they did not state. The
            unit then halts on your sentence instead of reading the artifact, and that halt
            reads as a defect in the work under review. Extract the ruling from wherever it
            is recorded and carry it as a quoted block.

            A MECHANICAL EDIT NAMES ITS FALSE POSITIVES. When a brief is a
            find-and-replace in shape -- renumbering, renaming, migrating a call
            site -- the pattern it matches will also match text that must NOT
            change. Enumerate those in the brief as an explicit do-not-touch
            list, quoting enough of each to identify it, and require the unit to
            confirm them unchanged in its report. A blind pass corrupts them
            silently, and the corruption reads as correct precisely because
            every other instance moved.

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

            A UNIT'S AUTHORITY STOPS AT SIDE EFFECTS, NOT JUST AT FILES. File ownership above
            settles which unit owns which path inside the work; it says nothing about what a
            unit may create, modify, move, or delete anywhere else, and silence there is not
            permission to reach beyond it. A unit touches nothing outside the checkout it
            was given to work in and its own session scratchpad. An experiment against a HOME-relative path
            exports `HOME` and `USERPROFILE` into a scratch directory first and verifies both
            resolve there before it runs anything. `git stash` is refused: the stash ref is
            shared with every worktree of the repository, so a stash one unit writes is
            visible to, and poppable by, another running at the same time.

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
          action: While units run, do coordination and read-only join work, or wait, and keep running units current.
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
            plan-checkpoint tests in `defaults/orchestration.yaml` apply). Then send a
            work-product correction brief to the same worker or an eligible replacement.
            A wrong decision
            MAY affect sibling briefs cut from the same decomposition, so re-check them after
            confirming the brief is defective. A disclosed critical-infrastructure
            change is briefed to a worker for recording in the appropriate CLAUDE.md --
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

            `accepted` means the returned artifact passed the named check without a revision.
            `corrected` means a delegated revision passed the named check before acceptance. `rejected`
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
          dispatched, send corrections back to a worker. Independent coordination is not
          blocked by it. Waiting for it is passive (step 6).
        - Parallel units editing the same files clobber each other -- but a shared-file conflict is a PARTITIONING problem before it is a scheduling one. Re-split the work by file ownership first (one owner per file, stated in each brief), and sequence only what genuinely remains. Reaching for sequencing first serialises work that had no real dependency.
        - A unit that correctly removes or relocates something can silently destroy the only signpost pointing at it -- a green result and a clean diff will not surface that; only the unit's own disclosure does.
        - >-
          Codex dispatch results have durable handles and can be recovered after a restart; see
          [references/codex-dispatch.md](references/codex-dispatch.md) "Dispatch cache". Pass
          the dispatch script's `--no-cache` option when a fresh run is required.
```

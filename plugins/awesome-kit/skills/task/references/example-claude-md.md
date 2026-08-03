# Example: a produced CLAUDE.md

This is a worked example of the CLAUDE.md a filled-in task folder carries (the hand-off template that `task init` scaffolds; see `handoff-template.md`). It is generalized from a real project hand-off; the structural skeleton (eight `##` sections under `# Project Overview`, the `###` and `####` subsections, and the seam between them) is verbatim. Project-specific bullets have been replaced with generic placeholders so this reads as a *template*, not as one project's snapshot.

Read this when the template in `handoff-template.md` feels abstract. The shape below is the contract; substitute the substance.

----

```markdown
# Project Overview

## Where we are today

The live state of the project.

### Environment

- cwd: project root `<path>`.
- Source control: `<P4 client + stream, OR git remote + branch>`.
- Platform: `<OS>`; `<primary shell>` native; Bash via the Bash tool.
- LLM / external service: `<model id via provider>`. Validate at session start: `<one-line validation command>`.
- Disk: `<expected free space>` on `<volume>`.
- Pass / cycle runner: `<path to runner script> <arg shape>`. <Note any positional-arg quirks the next agent will trip on.>
- Feature flags / depot CLs in flight: `<feature name (CL number)>` is in depot. `<symbol or path>` should be reused for `<purpose>`; the new <phase> needs a new <thing> (no LLM prompts; instead <shape>). On-disk audit directories from prior passes use the historical name `<old-name>/` -- the directory names stay literal; the prose names <things> by their descriptive names.
- Tool wrappers: `<canonical wrapper invocation, e.g. python.bat path>` (per `<source of the rule, e.g. ~/.claude/CLAUDE.md>`).

## Where we want to get to

The goal that the work above is converging on.

<One paragraph stating the goal falsifiably -- the next agent should be able to tell when it's done. Where applicable, name the phases / sub-stages and the success metric.>

Example shape:
- Phase 1 (<name>): <description>. Output: <artifact + shape>. <Any constraint -- e.g. NO LLM, NO template selection.>
- Phase 2 (<name>): <description>. Output: <artifact + shape>.
- Phase 3 (<name>): <description>. Output: <artifact + shape>.

REPLACE the existing <thing>; do NOT keep both. Validate on `<scope of validation>` across `<N> passes`. Produce `<artifact>` so the user can audit `<which slice>` manually while `<other slices>` run in background.

## Immediate Priorities

Live menu: `task items` (plan.md's task_items block is the source of truth).

Near-term blockers, pending decisions, and concrete next actions framed against the state above and the goal above. Work is named by backticked item id from plan.md's `task_items` block -- never with its state restated (state has one home):

- Resume `<item-id-1>` (<one clause of framing>).
- `<item-id-2>` and `<item-id-3>` are blocked on the question below.

### Open questions for the user

Surface this BEFORE any code work. Explain in detail; let the user choose:

**<Question title -- name the trade-off in one sentence>**

<2-4 sentence framing of why this matters. Reference the work-unit; explain what is at stake if we pick wrong.>

Three options:
- **A. <Option name>.** <One-line description.> <Cost + value.>
- **B. <Option name>.** <One-line description.> <Cost + value.>
- **C. <Option name>.** <One-line description.> <Cost + value.>

Recommend <X> for <scope> (preserves <property> while we validate <other property>; can revisit after). The user wants to make the call themselves.

## Project vocabulary

The N <pipeline stages / phases / domains>, named consistently throughout this file:
- **<term-1>** -- <one-line definition>. <Old name in prose / on-disk artifacts, if relevant.>
- **<term-2>** -- <one-line definition>.
- **<term-3>** -- <one-line definition>.
- **<term-4>** -- <one-line definition>.

**Trial / version naming -- <new name> vs <old name>.** The in-flight <thing> is named **<new>** ("<expansion>"), with <N> <sub-units>: `<new>.<sub-1>`, `<new>.<sub-2>`, `<new>.<sub-3>`. The historical <other things> keep their original names (`<old-1>`, `<old-2>`, ...) -- those names match the on-disk paths under `<path>` and `<other path>` and are stable historical references. **On-disk paths for the new thing also retain the literal `<old shape>` naming** (`<path with literal old-name>`, etc.) and the input file is `<filename retaining old token>` -- prose uses <new name> while paths stay literal. When both names appear together, qualify on first mention: e.g. "`<new>.<sub-1>` (on-disk path: `<literal old-name path>`)".

## Protocols

Time-boxed procedures with a defined trigger (e.g. "turn 1", "end of every turn") and a defined output shape. Each subsection below has the form *when X happens, do Y in this order/format.*

### Always-invoke skills (BEFORE any doc reads)

On turn 1, BEFORE reading `plan.md` / `log.md` / any sibling reference doc, invoke every `Skill(...)` line `task work` emitted, in the order printed. They load the project vocabulary and overview that the rest of CLAUDE.md assumes; a skill invocation is one tool call, so do them first and every later read has the right context.

That emitted block is the complete required set -- this task's `skills_to_invoke` plus the always-required baseline (`awesome-kit:orchestrate`). There is no second list here to consult or maintain; to add a skill, run `task update --skill-to-invoke <name>` (repeatable; REPLACES the stored list).

Skill invocations are pre-authorized; do not ask. The opening response protocol's "Read plan.md and log.md" check still applies AFTER the skills load.

### Required reads on turn 1

1. `<project-folder>/plan.md` -- accomplished + next concrete actions.
2. `<project-folder>/log.md` -- prior outcomes and rationale for `<current direction>`.

On-demand siblings (do NOT pre-read; load only when relevant):
- `<path>` -- <one-line purpose>.
- `<path>` -- <one-line purpose>.

### Opening response protocol

After invoking the always-invoke skills AND reading the required docs above, BEFORE any tool use, end the first turn with:

> "Invoked: <the skills `task work` emitted>. Read plan.md and log.md. Current goal: <restated in own words>. Prerequisites verified: <state of dependencies>. Starting with: <first concrete action>, dispatched to <sub-agent type / inline, with the reason>. Unclear / blocked on: <issue, or 'none'>."

The `Invoked:` and `dispatched to` clauses are not filler -- they surface a skipped initialization or an un-dispatched build in turn 1, instead of at end of session.

AND surface the question described under "Open questions for the user" above. User explicitly wants you to explain the trade-off and let them decide -- do NOT proceed past Step 1 of plan.md until they pick.

### Communication protocol

`/verbose-updates` three-part end-of-turn template:
> What changed: <action + paths>.
> Where it sits: <relation to in-flight work>.
> Required user action: <one decision OR "All requested work complete - ready to end session">.

**End-of-turn updates must be self-contained.** A user reading only the end-of-turn message should be able to understand it without re-reading CLAUDE.md or the project plan. Concretely:
- Name files by their absolute or project-relative path; do not refer to "the runner" or "the prompt yaml" without a path.
- Restate what the current goal is in one short clause.
- Do not refer to "Step 2" or "Phase 1" by index alone -- include the descriptive name.
- The plan and CLAUDE.md exist for fresh agents reading cold; the verbose-update exists for a distracted user reading the live conversation.

## Behaviors

Standing principles, rules, and gates that apply continuously regardless of which protocol is firing. They constrain *how* I act between/within protocol firings.

### Autonomy status

<One paragraph describing the user's current attention pattern -- at-desk-doing-parallel-work, AFK, reviewing-every-turn -- and what that implies for posture.> Optimize for:
- <One concrete rule that follows from the attention pattern.>
- <Another, e.g. "user wants to start auditing <X> as soon as it finishes, while <Y> and <Z> are still running.">

### Authorizations

- <File-authoring permission under a path>: proceed.
- <Source-control action>: proceed.
- <Submit / publish action with conditions>: PERMITTED after `<local review gate>`.
- **<Spending or quota authorization>**: proceed, ONE process at a time. The `<budget cap>` IS the standing authorization -- launch any task whose estimated cost stays under the cap without asking. Only request explicit authorization when a single action would push cumulative spend OVER the cap, or when the user has signaled a freeze. Cost estimates: <per-unit estimate>; <typical session total>. Track spend as you go.
- **Spawning background agents to do independent work**: proceed. Independent means the agent's task does not block the main thread on a decision the user has not yet made, and the agent's task does not write to the same files the main thread is currently editing. Use `run_in_background: true` so the main thread can continue; verify on-disk artifacts after the agent completes. Do NOT stop the turn to ask permission for an independent background-agent dispatch.

### Rules to follow

- <ONE-instance constraint, e.g. ONE LLM-emitting process at a time>. Verify via `<command>` before launching.
- <Background-vs-sub-agent rule for long-running work>.
- `<source-control pre-edit incantation>` before any tool-modification of a tracked file.
- Tool wrapper: `<canonical Python / build / test wrapper>`. Never bare `<other forms>`.
- <Character-set policy: ASCII in source / configs; carve-outs for worked examples or domain-specific exceptions>.
- <Working-data path>: <one-line description and the production-data avoidance rule>.
- <Concurrency tunings>: <stage>-<value>, <stage>-<value>. For the new <phase>, start with `<starting value>` per chunk so a single test scope runs in <expected shape>.
- **Don't end your turn while meaningful work remains.** When you finish a unit of work, look at the plan's next concrete action and either start it or surface a concrete decision blocking it. Do not hand back early. The plan itself is the authorization to continue -- if the next step is named there, you do not need fresh approval to begin it.
- **Consolidate repeated work into scripts.** When a body of work requires several tool calls AND is likely to repeat, author a script file in `<project-folder>/scripts/` (or invoke an existing one) rather than re-running the tool-call sequence by hand. The script is the durable artifact; future agents (and the user) invoke it in one tool call.

### Sub-agent orchestration -- main-context preservation

The main agent's job is orchestration, decision-routing, and surfacing concrete actions to the user. Heavy reading, code-drafting, file-authoring, and corpus-analysis should be pushed into sub-agents whenever possible; main reads the sub-agent's report instead of the raw inputs. This keeps main's context window available for the orchestration decisions only main can make.

Three concrete rules:

1. **Prefer offloading work to sub-agents to preserve main context.** When a unit of work is bounded (e.g. "draft this module", "summarize these audit dumps", "scan the prior trial's denial signals and produce a short list of challenging atoms"), spawn a sub-agent with a tight task brief and have it report back. Main reads the report, not the inputs. Do this even when main could do the work directly -- the context savings compound across the session.

2. **Main launches long-running processes itself.** Builds, test suites, cycle runners, servers, anything whose output the workflow needs back: main launches it (background bash, foreground tooling). Then main feeds the result into a sub-agent if analysis is needed. Reason: sub-agents are single-tier and cannot launch their own sub-agents, so "sub-agent launches a build that feeds another sub-agent" is impossible. Main is the only place that can tie launch + analysis together.

3. **If the work requires sub-agents, main orchestrates them.** Sub-agents are single-tier; they cannot spawn further sub-agents. When the work naturally fans out (e.g. "do A, then based on A do B and C in parallel and synthesize"), main coordinates: spawns sub-agent A, reads the result, then spawns B and C (in parallel where independent), then synthesizes. Do NOT write a sub-agent task brief that asks the sub-agent to spawn further sub-agents -- it cannot, and the chain breaks silently.

The principle behind these rules is main-agent context preservation, NOT the addition of per-launch gates that the standing authorizations (LLM budget, background-agent dispatch, plan-as-authorization) already cover. If an action is pre-authorized elsewhere in this file, orchestration does not re-gate it -- just launch and continue.

### Anti-patterns to avoid

- **Silent-go-to-work.** Always do the opening-response protocol AND surface the open question before tools.
- **<Submitting / publishing without local review>.** <Project-specific submit-auth condition>.
- **Polluting <production data>.** All <work-stream> edits land on `<test-side path>` (the test side-copy); production stays untouched.
- **Treating <preview artifact> as <depot / live>.** `<preview path>` is a PREVIEW; the live `<depot path>` is unchanged.
- **A "safe" test scope.** Inputs MUST be deliberately challenging -- sourced from `<prior denial / failure source>`. A clean-baseline test that converges trivially is worthless.
- **Parallel passes.** ONE <emitting process> at a time, even when the user is auditing one pass and wants the others in flight. Sequence them.
- **Asking permission to spawn a background agent for independent work.** Background-agent dispatch on independent work is pre-authorized (see Authorizations). Just spawn the agent with `run_in_background: true` and move on; stopping to ask "OK to spawn an agent for X?" wastes a round-trip.
- **Asking the user to review a CL when the CL does not need to be submitted for downstream work to continue.** The CL is a workspace bucket, not a gate. Continue downstream work; only stop for review when the CL is genuinely ready for submit and the user's decision controls whether to ship. Mid-pipeline CLs do not need pre-submit review just to keep cycling.
- **Stopping after one work-unit completes when the plan has the next unit named.** The plan is the authorization to transition; do not re-ask permission for the next step on the plan. If the next step is genuinely blocked on a decision the user has not made, surface that decision -- otherwise, start the next step.
- **Asking for authorization on spending work whose estimated cost is under the budget cap.** The session budget IS the authorization. Asking adds a round-trip; estimate the cost, confirm it's under the cap, launch. Only stop to ask when the planned action would push cumulative spend past the cap, OR when the user has explicitly frozen spending.

## Relevant files

### Project folder

Contents of `<project-folder>/` -- this hand-off's own working tree.

Required-read (turn 1):
- `CLAUDE.md` -- self (this file).
- `plan.md` -- accomplished + next 1-3 actionable steps.
- `log.md` -- prior outcomes + rationale + on-demand context.

Other files:
- `<step-N-details.md>` -- implementation spec for `<step name>`.
- `references/<topic>-index.md` -- project-local quick-index of `<external dependency>` relevant to this work.
- `agent-briefs/` -- prompts dispatched to background agents (audit trail). Today: `<brief-1.md>`, `<brief-2.md>`.
- `scripts/` -- per the "Consolidate repeated work into scripts" rule, this directory holds project-local scripts when a body of work needs to repeat. <Note any current contents, or "Empty today; the canonical per-pass runner currently lives at `<external path>`.">

### External files

Files / scripts / directories outside `<project-folder>/` that this project depends on. Link to summary documents when the underlying data is large; the body below this section is the project-local quick index, not a full re-enumeration.

For `<external domain summary -- pipeline overview, target languages, entry points, key directories, Materialized Insights, override aggregators>`, see `<canonical skill / doc>`. The entries below are PROJECT-SPECIFIC to this work; they do not restate skill content.

#### <project-specific subsection -- e.g. core library / package>

Canonical module index lives in `<canonical path>` (loaded into context when work touches the package). A distilled project-local quick-index of the modules most relevant to this work is at `<project-folder>/references/<topic>-index.md`.

#### <project-specific subsection -- e.g. prompt / config files>

The N `<glob>` files this work touches plus their roles.

The new authoring surface for this work:
- `<file-1>` -- <role>. Authored in <CL / commit>.
- `<file-2>` -- <role>. Authored in <CL / commit>.

Shared substrate (the files multiple phases reference):
- `<file-A>` -- <role>.
- `<file-B>` -- <role>.

Legacy <X> scheduled for deletion in <CL / commit>:
- `<file-old-1>` -- the prior <thing>.
- `<file-old-2>` -- the prior <thing>.

#### <project-specific subsection -- e.g. production-data warning>

`<production data path>` -- never touch in this project. All edits land on the test-side at `<test-side path>` (the test side-copy; filename retains the historical token).

#### <project-specific subsection -- e.g. audit-directory shapes>

The `<dir pattern>` paths this project writes, and what each holds.

Per-pass audit dumps under `<path pattern>/` (path segment retains the historical `<old shape>`):
- `<dir-1>/` -- <what it holds>.
- `<dir-2>/` -- <what it holds>.
- `<dir-3>/` -- <what it holds>.

Directory names retain the historical `<old tokens>`; the prose throughout this project names the stages by their descriptive names (see ## Project vocabulary).

#### <project-specific subsection -- e.g. runners>

`<runner script path>` -- the per-<unit> runner. Sequences `<phases>`. <Note any positional-arg names that still read as the old vocabulary until updated.> Invocation shape: `<command-line shape>`.

#### <project-specific subsection -- e.g. prior-trial artifacts>

Substrate for this work: prior outcomes and source data.

`<prior project>/` outcomes:
- `<file-1>` -- <one-line purpose>.
- `<file-2>` -- <one-line purpose>.
- `<file-3>` -- <one-line purpose>.

Per-<unit> directory layout (`<path>/`):
- `<dir-1>/`, `<dir-2>/` -- <one-line purpose>.
- `<dir-3>/`, `<dir-4>/` -- <one-line purpose>. <New-work directories live alongside under literal names -- the on-disk path segment retains the historical shape even though prose names them by the new convention.>

Sibling project root files (`<sibling-path>/`):
- `CLAUDE.md` -- sibling project's hand-off (older, but its <X> section is still authoritative for <Y>).
- `<file-pattern>` -- per-<thing> substrate.
- `<analysis-notes-pattern>` -- analysis notes (one-offs).
- `<scratchpad-pattern>` -- numerous one-off utilities. Treat the whole subtree as a historical scratchpad; reach for individual files only when chasing a specific signal.
```

----

## How to use this template

1. Copy the structure verbatim.
2. Fill in every angle-bracketed placeholder. If a placeholder has no project-specific answer, the section is probably under-populated -- think again before deleting it.
3. Add project-specific `###` subsections where the work demands them (e.g. `### Open questions for the user` under `## Immediate Priorities` in the example). The eight `##` sections are the contract; what hangs off them is project-specific.
4. Run the workflow's Step 5 self-verify before declaring done.

## What this example demonstrates

- **The seam between `## Where we are today` and `## Immediate Priorities`.** State is static description (env values, what's in depot, what's wired up). Priorities are decisions / actions queued against the state (open question for the user, next concrete action).
- **The seam between `## Protocols` and `## Behaviors`.** Protocols name a trigger (turn 1 -> invoke skills + read docs + say the opening response sentence; end of every turn -> emit the three-part template). Behaviors apply continuously (autonomy status, authorizations, rules, anti-patterns) regardless of which protocol is firing.
- **Vocabulary capture with a path decoder.** When prose names diverge from on-disk literals, `## Project vocabulary` names both AND explains the mapping. Saves the next agent from asking the user to translate.
- **In-flight triage surfaced under `## Immediate Priorities`.** Blocked-on-user decisions get an explicit "Open questions for the user" subsection. The next agent surfaces these before tool use.
- **Priorities as references.** The section names work by `task_items` id and never restates item state -- a pointer cannot drift from the item it points at (the task-items contract; see the task skill's handoff-template.md).
- **One-line purpose per file under `## Relevant files`.** No "Files: a.md, b.md, c.md" lists. Every entry tells the next agent whether to open it.

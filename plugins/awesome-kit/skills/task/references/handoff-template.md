# The hand-off template (task folder authoring guide)

How to fill in -- and keep healthy -- the documents of a `hand-off`-type task
folder: `CLAUDE.md`, `plan.md`, and `log.md`. `task init` scaffolds these
files with placeholders; this reference is the contract for the content an
agent writes into them, whether on first fill or on every later `update`.

This guidance descends from the original `/hand-off` skill (the task folder
is the generalized hand-off folder). The shared vocabulary -- `work-unit`,
`auto-loaded vs on-demand context`, `three-part end-of-turn template`,
`State A / State B`, `orientation moment`, `self-contained briefs`,
`hand-off baton`, `provenance triad` -- is canonical at
`communication-framework.md`. A worked example of a fully-populated CLAUDE.md
is at `example-claude-md.md` -- read it if the template below feels abstract.

## The core idea: two gates

A task folder is a context-management strategy, not a filing system. The two
gates -- `auto-loaded` vs `on-demand` -- are defined in the framework.
Applied here:

- **Auto-loaded** -- `CLAUDE.md` (Claude Code auto-loads when cwd is the
  folder), plus anything `CLAUDE.md` directs the next agent to read on turn 1
  (notably `plan.md`).
- **On-demand** -- everything else (log.md, parent-plan.md, design notes,
  archived step details).

The discipline is keeping the auto-loaded set tight. Anything that does not
need to be in the agent's head on turn 1 of the next session belongs in an
on-demand doc.

The folder slug should be the natural scope of the work-unit (phase or
project), not session-scoped. Example: `atoms-17-phase2` (phase) or
`loc-pipeline` (project) -- not `atoms-22` (one session within the phase).

## `CLAUDE.md` -- continuation prompt + guide

Auto-loaded every session. This is the agent's first 60 seconds of
orientation. Everything they need in those 60 seconds goes here; everything
else gets indexed and lives elsewhere.

### Required template (eight `##` sections under a single `# Project Overview`)

The eight `##` sections below are template-required. Project-specific `###`
subsections (or `####` deeper) are additional -- include them where the work
needs them. The produced CLAUDE.md does NOT need to annotate
template-vs-additional; the consistent eight `##` shape is the contract.

```
# Project Overview
## Where we are today
### Environment
## Where we want to get to
## Immediate Priorities
## Project vocabulary
## Protocols
### Always-invoke skills (BEFORE any doc reads)
### Required reads on turn 1
### Opening response protocol
### Communication protocol
## Behaviors
### Autonomy status
### Authorizations
### Rules to follow
### Sub-agent orchestration -- main-context preservation
### Anti-patterns to avoid
## Relevant files
### Project folder
### External files
```

### Section semantics

- **`## Where we are today`** -- live state. Static descriptive snapshot:
  environment values, in-flight processes, what's wired up right now. Things
  that are simply *true* of the project as of this hand-off. Includes a
  `### Environment` subsection with cwd, platform, key tool versions, env
  quirks.
- **`## Where we want to get to`** -- the goal the work is converging on.
  State it falsifiably so the next agent can tell when it's done.
- **`## Immediate Priorities`** -- decisions and actions queued *against* the
  snapshot in "Where we are today." Near-term blockers, pending decisions the
  user needs to make, the concrete next 1-2 actions. **Seam test**: if it
  would still be true after the agent acts (a fact of the project), it
  belongs in *Where we are today*; if acting on it changes it (a decision or
  next step), it belongs in *Immediate Priorities*.
- **`## Project vocabulary`** -- terms, stage names, trial names, domain
  conventions used in the rest of the file. Includes naming evolutions that
  left on-disk paths with old names (e.g. "renamed `cycle` to `pass` in
  prose; on-disk paths retain `cycle`"). The next agent reads the rest of the
  file fluently because vocabulary is established here.
- **`## Protocols`** -- time-boxed procedures with a defined trigger and a
  defined output shape. **Test**: if you can name the trigger ("turn 1", "end
  of every turn", "before any tool use") AND the output shape (a checklist, a
  sentence template, a sequence of skill invocations), it is a Protocol.
  Otherwise it is a Behavior. Subsections:
  - **`### Always-invoke skills (BEFORE any doc reads)`** -- one tool call
    per listed skill, before reading `plan.md` or any other doc. Skills load
    vocabulary that the rest of CLAUDE.md assumes; reading docs first means
    reading them without the right vocabulary in context.
  - **`### Required reads on turn 1`** -- explicit list of docs to read
    before acting. At minimum `plan.md`. Add `log.md` only if the next agent
    needs prior rationale to act on turn 1; otherwise leave it on-demand.
  - **`### Opening response protocol`** -- the `orientation moment` for
    session resume. What the agent says after reading the required docs,
    before tool use. Example template (project-specific text varies):
    > "Read plan.md (+ any other required reads). Current goal: <restated in
    > own words>. Starting with: <first concrete action>. Unclear / blocked
    > on: <issue, or 'none'>."
  - **`### Communication protocol`** -- default to `/verbose-updates`'s
    three-part end-of-turn template (see framework). Note project-specific
    overrides here (audit-log shape, domain terminology, what NOT to say).
- **`## Behaviors`** -- standing principles and gates that apply continuously
  regardless of which protocol is firing. **Test**: if it has no trigger --
  it constrains *how* you act between/within protocol firings -- it is a
  Behavior. Subsections:
  - **`### Autonomy status`** -- whether the user is reading every turn or
    returning cold; how aggressive to be; what the standing posture is.
  - **`### Authorizations`** -- explicit, named pre-authorized actions. The
    point is to enumerate the standing yes-list so the next agent does not
    round-trip for already-authorized work.
  - **`### Rules to follow`** -- project-specific operational rules
    ("background long-running work, never inline"; "ASCII-only in source
    files"; concurrency settings; tool wrappers).
  - **`### Sub-agent orchestration -- main-context preservation`** -- when
    work should be pushed to sub-agents, when main launches things itself
    (long-running processes), the single-tier sub-agent constraint
    (sub-agents can't spawn sub-agents). Main's job is orchestration; heavy
    reading goes to sub-agents.
  - **`### Anti-patterns to avoid`** -- explicit don'ts for this work. "Be
    careful with X" is vague; anti-patterns make boundaries concrete.
- **`## Relevant files`** -- file index. Split into:
  - **`### Project folder`** -- contents of the task folder (this work's own
    tree). One-line purpose per file. Indicate which are auto-loaded (the
    required-reads list) vs on-demand.
  - **`### External files`** -- files, scripts, directories outside the task
    folder that this work depends on. One-line purpose per file. Group under
    `####` subsections where natural. Link out to summary documents when the
    underlying data is large.

**One-line purpose per file.** Every doc listed under `## Relevant files`
carries a one-line description of what it is and why the agent would read it.
"Files: a.md, b.md, c.md" is the anti-pattern -- the next agent should not
have to open a file to discover whether to open it.

### Length

**Soft target: up to 400 lines.** The shape is "every section has what it
needs, nothing has accumulated unnecessarily." A section that keeps growing
is a signal its content belongs in a referenced doc and CLAUDE.md should just
link to it. CLAUDE.md is auto-loaded every session, so length is a context
budget; 400 lines is the comfortable ceiling, not a goal. Land lower when the
work allows.

## `plan.md` -- the plan

Read on turn 1 because CLAUDE.md tells the agent to. Two sections only:

1. **Accomplished** -- one line per completed step. No implementation detail.
2. **Forward overview** -- the next 1-3 steps in actionable detail; later
   steps as one-line summaries only.

**Soft target: up to 400 lines** (verify with `wc -l plan.md`). plan.md is
read on turn 1 of every session, so length is a context budget; 400 lines is
the comfortable ceiling, not a goal.

Apply rotation by default to stay under this target -- it is not a fallback
that activates only when over the limit, it is the standing discipline.

**Rotation strategy: history first, optional second.** When reducing, walk
the file with this priority:

1. **Primary -- move historical content out.** Completed-step detail,
   retrospective context, accumulated session-by-session log -- these go to
   `log.md` (or `step-N-completed.md`). A completed step keeps a one-line
   summary plus a link to log.md; no implementation detail.
2. **Secondary -- move not-always-required forward content out.** Optional
   branch detail, "alternative approach" subsections, grown parked-decision
   lists, far-future-step explanations -- these go to referenced docs
   (`alternatives.md`, `parked.md`, `step-N-details.md`) with a one-line
   pointer in plan.

Removing future work from plan is more expensive (the next agent has to
follow a link to know what's coming) than removing past work (the next agent
does not care). The plan is a moving window, not a record.

**In-flight triage.** Before writing the forward overview, classify every
in-flight item into one of four buckets:

- **Blocked on user decision** -- surface under `## Immediate Priorities` in
  CLAUDE.md; do not list as a forward step in plan.md until decided.
- **Blocked on prior step** -- list in plan.md as the prior step's
  continuation, not as a standalone item.
- **Done but uncommitted** -- note in plan.md so the next agent does not
  re-do.
- **Queued** -- ready to start; the next-1-3-steps actionable detail covers
  these.

## `log.md` -- history

On-demand only. Holds:

- Approaches tried that didn't work, with reasons -- **but only if the reason
  would re-bite a fresh agent.** A superseded approach whose reasoning is now
  obvious goes nowhere; the log is not a session diary.
- Decision rationale (only if not currently actionable; current-decision
  rationale belongs in the plan). Use the `provenance triad` shape from the
  framework -- surface / finding / follow-up -- where it fits.
- Surprises about the codebase that informed direction.
- Completed-step details rotated out of the plan.
- Notes that would help someone six months from now reconstruct reasoning.

Multiple log files (`log-decisions.md`, `log-dead-ends.md`) are fine when
volume justifies the split. (`task update` appends a mechanical dated entry
to log.md; the substantive content above is the agent's to write.)

## Optional referenced docs

Named per the work; CLAUDE.md indexes them with one-line purposes. Common
patterns: `parent-plan.md` (multi-phase context), `step-N-details.md`,
`step-N-completed.md`, design docs, glossaries, snapshots. No prescribed
names. The rule: every doc in the folder is named in CLAUDE.md's index with a
one-line purpose.

## Rotation discipline (the update passes)

The plan should always answer "where are we now and what's coming." On every
substantive update of an existing folder, run three passes:

- **Rotation pass.** Step completed -> move its detailed instructions from
  plan.md to log.md (plan keeps one line: "Step N: done [link]"). Future step
  too far away -> move its implementation detail to a referenced doc.
  Decision made -> if currently-actionable, one line in plan; if not, into
  the log.
- **Stale-state pass.** Anything now untrue under current scope (e.g. "ready
  to ship" when scope just expanded) gets fixed in place. Untrue text is
  worse than missing text.
- **Vocabulary pass.** Compare the vocabulary the session evolved (names that
  drifted, stage names, paths that retain old names) against what is
  currently in `## Project vocabulary`. Update so the next agent reads
  CLAUDE.md in the same dialect the session ended in. If you can't name three
  vocabulary items the session leaned on, you are under-capturing.

Rule: if it's not actionable for the next session's work, it does not belong
in the auto-load surface.

## Anti-patterns

- **Silent-go-to-work.** Next agent reads docs, picks up tools, makes
  changes. No orientation check, no question if confused. The
  opening-response protocol in CLAUDE.md is the antidote.
- **Implicit communication protocol.** "Behave well" -- vague. The
  Communication protocol subsection must name `/verbose-updates` (or another
  explicit protocol) as the end-of-turn default.
- **Rules without anti-patterns.** "Be careful with X" -- agent fills in
  their own definition. Anti-patterns make the boundary concrete.
- **Index-without-purpose.** "Files: a.md, b.md, c.md" -- agent has to read
  each to know what they're for. One-line purpose per doc.
- **Separate must-read decisions doc.** False economy. If must-read, it
  belongs in the plan. If not must-read, it belongs in the log.
- **Plan as record.** Plan accumulates every step's full detail forever.
  Auto-load surface bloats. Rotate.
- **Session-scoped slug.** `atoms-22` outlived by `atoms-17-phase2`. Pick the
  scope that outlives the session -- the work-unit, not the conversation.
- **Vocabulary loss across the hand-off.** Mid-session, terms evolve.
  Without a `## Project vocabulary` section, the next agent reads the file in
  a different dialect from the one that wrote it. Capture the final names AND
  the decoder for paths that retain old names.
- **Session diary in log.md.** Every superseded approach dumped in regardless
  of whether the reasoning still matters. The filter is "would this rationale
  re-bite a fresh agent if not recorded?" -- if no, discard.
- **Conversation-context references in CLAUDE.md or plan.md.** "As we
  discussed", "the user just clarified that...". A cold reader cannot resolve
  these. The fact in question goes in the artifact; the conversation pointer
  goes nowhere.

## Self-verify (before declaring the folder ready)

Read CLAUDE.md and plan.md as if you were the next agent. Verify:

- Can you identify the current goal without reading anything else?
- Do you know what to do next, concretely?
- Do you know what to say back after reading the docs (the opening-response
  protocol)?
- Are the working directory and any non-obvious operational rules stated?
- **Cold-reader self-containment.** Does anything reference conversation
  context a cold reader cannot resolve? If yes, restate the fact in the
  artifact and drop the conversation pointer.
- **Vocabulary coverage.** If a fresh agent read CLAUDE.md and then opened a
  path in `## Relevant files`, would the path's literal name match the prose
  name? If not, `## Project vocabulary` is missing the decoder.

When the folder is being packaged for a fresh session, end the turn with the
`hand-off baton` (see framework): the literal line
`Paste into a new session to continue:` followed by a short actionable
instruction -- typically `Read <task-folder>/CLAUDE.md and proceed per its
protocol.`

## Principles specific to hand-off folders

- **One must-read doc per concern.** Plan for the work; CLAUDE.md for the
  operation. No third must-read.
- **Don't duplicate.** Each fact lives in exactly one doc.
- **Prefer editing over creating.** Update existing artifacts unless there is
  a genuinely new concern.
- **Sibling folders.** If parallel-stream folders exist for related work,
  CLAUDE.md mentions them so the next agent doesn't re-claim.
- **ASCII only** in all files (no smart quotes, em dashes, or other Unicode
  look-alikes).
- **No absolute paths** in the artifacts -- use project-root-relative paths
  so they work across machines.
- **Fit the work to the template.** The eight `##` sections cover most
  hand-offs. Resist adding more required sections to suit a specific work
  shape; let `###` subsections fill them with what the work needs.

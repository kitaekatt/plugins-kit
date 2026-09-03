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

## Document size budgets (enforced by `validate`)

Length is a context budget, and it is enforced, not advisory: `validate`
(and every verb that re-validates -- `update`, `reopen`, the `work` gate)
applies role-based line budgets to every top-level `*.md` in the folder.
Budgets are measured in lines so you can self-check with `wc -l`:

| Document           | Healthy target (note) | Ceiling (warning) |
|--------------------|-----------------------|-------------------|
| CLAUDE.md          | 250                   | 400               |
| plan.md            | 300                   | 400               |
| log.md, log-*.md   | exempt                | exempt            |
| any other `*.md`   | (none)                | 800               |

Two tiers:

- **Note** (advisory: not a finding, exit code unaffected) at the healthy
  target -- "approaching budget": rotate now while it is cheap.
- **Warning** (a real finding: warnings gate `work` like any other) at the
  ceiling -- "oversized document": the fix is decomposition per the rotation
  strategy below (rotate history to log.md, split detail into referenced
  docs), not trimming.

Every length finding names the doc, its line count, the threshold, and the
largest `##` sections with their individual line counts -- it tells you what
to move. Two further advisory notes:

- **Dominant section** (CLAUDE.md/plan.md, docs of 150+ lines): a single
  `##` section over half the document -- the "Where we are today" /
  "Accomplished" accretion pattern, caught long before the file ceiling.
- **Session diary** (CLAUDE.md only): more than 3 dated narrative markers
  (paragraphs opening with a bold date, `**YYYY-MM-DD`). CLAUDE.md is live
  state, not history; dated narrative belongs in log.md.

Why log.md is exempt: it is the append-only history sink that rotation
TARGETS -- a big log is the system working, and it is only loaded on demand.
Why other docs get a loose 800-line ceiling and no note tier: decomposition
needs somewhere cheap to put content; a tight gate on reference docs would
just chase displaced content around.

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

  **A version, branch or publish claim written here is a SNAPSHOT, not a
  fact.** It was true when written and nothing updates it. In a workspace
  where other sessions commit, publish, or bump concurrently, it can go stale
  within the hour. Re-read the source -- the manifest, the remote ref, the
  installed artifact -- before relying on one, and especially before passing
  one to a delegated agent as an established premise, where a stale claim is
  laundered into a fact the agent will not re-check. Prefer writing the CHECK
  next to the claim ("skills-kit was 0.66.0 at hand-off; confirm with
  `git show origin/master:<manifest>`") so a reader inherits the way to
  falsify it rather than only the value.
- **`## Where we want to get to`** -- the goal the work is converging on.
  State it falsifiably so the next agent can tell when it's done.
- **`## Immediate Priorities`** -- decisions and actions queued *against* the
  snapshot in "Where we are today." Near-term blockers, pending decisions the
  user needs to make, the concrete next 1-2 actions. **Seam test**: if it
  would still be true after the agent acts (a fact of the project), it
  belongs in *Where we are today*; if acting on it changes it (a decision or
  next step), it belongs in *Immediate Priorities*.

  **Reference view, not a content list.** When this section names work, it
  names it by backticked item id from plan.md's `task_items` block (see the
  plan.md section below), optionally with one clause of framing -- it never
  restates an item's content at length and NEVER restates its state. Item
  state has exactly one home (the block); a priority that is a pointer
  cannot drift from the item it points at. Prose stays for genuinely
  non-item content: open questions for the user, standing warnings, the
  seam-test facts above. Open the section with the standing line:
  "Live menu: `task items` (plan.md's task_items block is the source of
  truth)." (`validate` warns on a backticked hyphenated id here that matches
  no item.)
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
    per skill, before reading `plan.md` or any other doc. Skills load
    vocabulary that the rest of CLAUDE.md assumes; reading docs first means
    reading them without the right vocabulary in context.

    **A pointer, not a second list.** The required-skill set has ONE home:
    `task.yaml`'s `skills_to_invoke`, which `task work` merges with the
    always-required baseline and emits as a single initialization block of
    `Skill(...)` lines. This section says so and stops -- it does not
    restate the names. A maintained copy here would be a second source that
    drifts from `task.yaml` the first time either changes, and the copy is
    the one nobody updates. Standing text:
    > Invoke every `Skill(...)` line `task work` emitted, in the order
    > printed, before reading any doc. That block is the complete required
    > set (`task.yaml`'s `skills_to_invoke` plus the baseline); there is no
    > second list to consult. Skill invocations are pre-authorized -- do not
    > ask.

    To ADD a skill to the set, edit `task.yaml`
    (`task update --skill-to-invoke <name> ...`, repeatable, REPLACES the
    stored list), not this section.
  - **`### Required reads on turn 1`** -- explicit list of docs to read
    before acting. At minimum `plan.md`. Add `log.md` only if the next agent
    needs prior rationale to act on turn 1; otherwise leave it on-demand.
  - **`### Opening response protocol`** -- the `orientation moment` for
    session resume. What the agent says after reading the required docs,
    before tool use. Example template (project-specific text varies):
    > "Invoked: <skills, as emitted by `task work`>. Read plan.md (+ any
    > other required reads). Current goal: <restated in own words>.
    > Starting with: <first concrete action>, dispatched to <sub-agent /
    > inline, with the reason>. Unclear / blocked on: <issue, or 'none'>."

    The `Invoked:` and `dispatched to` clauses are load-bearing: they make a
    skipped initialization or an un-dispatched build visible to the user in
    the first turn, rather than surfacing at end of session as "I invoked
    the task skills but implemented everything inline." Stating the dispatch
    decision out loud is also what forces it to BE a decision.
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

**Enforced budget: note at 250 lines, blocking warning at 400** (see
"Document size budgets" above). The shape is "every section has what it
needs, nothing has accumulated unnecessarily." A section that keeps growing
is a signal its content belongs in a referenced doc and CLAUDE.md should just
link to it. CLAUDE.md is auto-loaded every session, so length is a context
budget; 400 lines is the ceiling `validate` warns at (and warnings gate
`work`), not a goal. Land at or under the 250-line healthy target when the
work allows.

## `plan.md` -- the plan

Read on turn 1 because CLAUDE.md tells the agent to. Two sections only:

1. **Accomplished** -- one line per completed step. No implementation detail.
2. **Forward overview** -- opens with the `task_items` block (below), the
   index of open work; then the next 1-3 items in actionable detail (prose
   sections keyed by item id, e.g. `### nano-swipe-controls -- <title>`);
   later items as one-line summaries or just their block entry.

### The `task_items` block (the open-item menu)

An **item** (accepted synonym: "work item") is the enumerable unit of next
work within the task -- a goal, a chore, a blocked decision, a watch item.
The `task_items:` typed unit, a fenced YAML block at the top of the Forward
overview, is the SINGLE home for the task's open items; `task items`
enumerates it and `validate` checks it. Item state lives there and only
there -- everything else (CLAUDE.md priorities, prose) references items by
id.

```yaml
task_items:
  items:
    - id: nano-swipe-controls        # kebab-case, unique within the task
      title: "Nano swipe-gesture controls"
      state: in-flight               # available | in-flight | blocked-user | deferred
      priority: P1                   # optional; same P1-P3 scale as task.yaml
      note: "resume: see log 2026-07-09"   # optional one-liner
```

The four states are the in-flight triage buckets (below) promoted to
contract: `available` (queued, ready to start), `in-flight` (under way,
including paused mid-step), `blocked-user` (needs a user decision or the
user's hands), `deferred` (parked deliberately). There is **no `done`
state**: completion is REMOVAL from the block at the rotation pass -- one
line in Accomplished, detail to log.md. The block enumerates open work only,
a moving window like the rest of the plan. Within equal priority (and among
items with none), list order means sooner. An item that outgrows the block
(own plan, own sessions) is promoted to a task of its own (`init` + a
`task_list` reference); it is an item exactly as long as it needs no
identity outside this plan.

### Converting a pre-contract folder (one-time)

A folder created before the task-items contract has no `task_items` block;
`validate` warns ("no task_items block") and the warning gates `work`. The
conversion is the FIRST act of the next update/hand-off pass on such a
folder -- do it before any other rotation work, and do it completely. The
correct end state is: **the block is the only carrier of open-work state
anywhere in the folder.** A half-conversion (block added, but old forms left
behind still carrying state) recreates the drift this contract exists to
kill.

1. **Sweep all three documents for open work.** The legacy forms it hides
   in: CLAUDE.md Immediate Priorities entries and dated "RESUME HERE" /
   status banners; plan.md Forward-overview prose steps, `GOAL:` heading
   blocks, and `[ ]/[x]` checkbox lists; log.md `(OPEN)` tags and the
   watch-list / carry-forward fragments at the tail of recent entries.
2. **Dedupe into distinct items.** One entry per unit of work, however many
   documents and wordings mention it. Pick kebab-case ids; keep the freshest
   wording (the log and dated banners usually beat a stale priority list).
3. **Map states via the triage buckets above**; put blockers, reasons, and
   resume pointers in `note:` (one line each).
4. **Write the block** at the top of the Forward overview.
5. **Rewrite CLAUDE.md Immediate Priorities as the reference view** (ids +
   the standing line; state stated nowhere).
6. **Retire the superseded forms.** A `GOAL:` block becomes an id-keyed
   detail section (`### <id> -- <title>`, state parenthetical dropped); done
   checkbox entries collapse to Accomplished one-liners; open log items are
   now in the block (promotion rule) -- leave the log prose as history, but
   nothing outside the block may READ as a live open-work list.
7. **Re-run `validate` until clean.** Clean validate -- no missing-block
   warning, no stale item references -- is the definition of conversion
   done; do not end the pass with findings outstanding.

**Enforced budget: note at 300 lines, blocking warning at 400** (verify with
`wc -l plan.md`; see "Document size budgets" above). plan.md is read on turn
1 of every session, so length is a context budget; 400 lines is the ceiling
`validate` warns at (and warnings gate `work`), not a goal.

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
in-flight item into one of four buckets -- each maps to a `task_items`
state:

- **Blocked on user decision** (`state: blocked-user`) -- reference the id
  under `## Immediate Priorities` in CLAUDE.md; record what is needed in the
  item's `note:`.
- **Blocked on prior step** -- list in plan.md as the prior step's
  continuation, not as a standalone item (a soft ordering note like "after
  `<id>`" fits in `note:` when it must stand alone).
- **Done but uncommitted** -- note in plan.md so the next agent does not
  re-do.
- **Queued** (`state: available`) -- ready to start; the next-1-3-items
  actionable detail covers the top of the list. Deliberately parked work is
  `state: deferred` with the reason in `note:`.

## `log.md` -- history

On-demand only. Exempt from the document size budgets -- it is the sink
rotation targets, and growth here is the system working (split logs,
`log-*.md`, inherit the exemption). Holds:

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
one-line purpose. Budget: an 800-line ceiling (warning), no note tier -- see
"Document size budgets" above.

## Durable outputs (does this belong in the folder at all?)

The size budgets ask *where a fact goes within the folder*. This asks one axis
over: **does the document belong in the folder in the first place?**

Apply the test in one breath, at the moment you create the document:

> **If this folder were deleted today, would someone still need this document?
> If yes, its home is the repo it describes. The task folder references it; it
> never owns it.**

**Why authoring time, not archive time.** Archiving a `dev/tasks` folder
commits the final state, deletes the folder, and commits the removal --
version control is the record. A load-bearing document that lives only in the
folder therefore becomes a deleted file: not discoverable, not indexed,
findable only by someone who already knows it existed. But archive is the
*wrong moment to fix that*. A spec that only reaches its durable home when the
task ends was undiscoverable for the entire life of the task -- exactly when
people needed it. Extraction belongs at authoring time; the archive check is a
backstop for what slipped, not the mechanism.

**Declare what is durable.** A task records its durable outputs as data:

```bash
task update <ref> --durable-output docs/architecture/env-json.md \
                  --durable-output docs/reference/software-set.md
```

This writes `durable_outputs:` into `task.yaml` -- an optional list of paths
**relative to the owning repo's root**. Repeatable, and it REPLACES the stored
list (the same convention as `--depends-on` / `--skill-to-invoke`, not an
append). **A document not declared is task-local by declaration** -- that is
the point: the judgment is explicit and recorded, not inferred later by
someone with less context.

**What archive verifies.** Mechanically, and without asking you anything,
every declared path must:

1. be **relative**, and resolve **inside** the project root -- an absolute or
   `../`-escaping path names a home the repo does not carry, and version
   control is what makes the home durable;
2. **exist**; and
3. live **outside** the task folder. This is the load-bearing one -- the
   folder is about to be parked or deleted, so a document inside it has no
   durable home at all.

Any failure **refuses the archive, naming every offender**, while the
documents can still be moved. Nothing is parked, committed, or removed first.

**Deliberately not enforced elsewhere.** This is *not* a `validate` warning:
findings gate `work`, and no script can tell an architecture spec from a
throwaway analysis, so every folder with an extra `.md` would be blocked.
There is no content classifier guessing which documents are "architectural" --
false precision is worse than an explicit declaration. And a folder with no
`durable_outputs` field (every folder predating the rule) archives normally
with a reminder note; manifests here stay backwards-readable.

**Where the durable home is** is a lookup, not an assessment: the owning
repo's conventions say where such documents live. If the repo has no
convention for it, that gap is the thing to fix -- not a reason to leave the
document in the folder.

One honest outcome worth naming: **declaring nothing durable is a valid
result.** If the folder's findings were already absorbed into as-built docs
elsewhere, the document is historic and relocating it would create a second
source of truth -- the failure mode one step removed from the one this rule
fixes. Establish which case you are in *before* moving anything.

## Rotation discipline (the update passes)

The plan should always answer "where are we now and what's coming." On every
substantive update of an existing folder, run three passes:

- **Rotation pass.** Step completed -> move its detailed instructions from
  plan.md to log.md (plan keeps one line: "Step N: done [link]") AND remove
  the completed item from the `task_items` block. Future step too far away
  -> move its implementation detail to a referenced doc. Decision made -> if
  currently-actionable, one line in plan; if not, into the log.
  **Promotion rule: the block is the only place open work may live.** Any
  open item surfaced mid-session in log prose or CLAUDE.md banners --
  `(OPEN)` tags, watch-lists, carry-forwards -- is either promoted into the
  block (usually `blocked-user` or `deferred`, with a `note:`) or
  deliberately discarded, at this same pass.
- **Stale-state pass.** Anything now untrue under current scope (e.g. "ready
  to ship" when scope just expanded) gets fixed in place. Untrue text is
  worse than missing text. Check that every backticked item id referenced in
  CLAUDE.md still resolves to a `task_items` entry (`validate` warns on
  strays).
- **Durable-outputs pass.** For every non-scaffold doc in the folder (anything
  beyond CLAUDE.md / plan.md / log.md), apply the one-breath test above. If it
  would outlive the folder, move it to its home in the owning repo now and
  declare it (`--durable-output`); the folder keeps a reference, not the
  document. See "Durable outputs" above.
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
- **Item state restated outside the block.** A priorities list that copies
  item titles and states ("RESUME POINT -> ...", "(not started)") is a
  second copy that WILL drift -- the mined failure mode behind the
  task-items contract (see design/task-items-design.md). Reference the id;
  let the block carry the state. Same disease: `DONE`-tagged items accreting
  in the block or a checkbox list instead of being removed at rotation.
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

This self-verify, and the baton below, belong to a hand-off turn ONLY -- one
the user opened with `/task hand-off <ref>` (the task skill's `hand_off`
capability). Do not run them, or offer to, on your own initiative: a stopping
point that looks like a session boundary is not a hand-off, and the user
decides when a session ends. In that turn, end with the `hand-off baton` (see
framework). For a task folder the baton is a two-line
block giving the current working directory and the resume command:

```
CWD: <project root directory>
Continue: /task work <CWD-relative task-folder path>
```

The user copies the `Continue:` line into a new session; `/task work <CWD-relative task-folder path>` works the explicitly named folder and loads its working context (better than a bare
`Read <folder>/CLAUDE.md`, which does neither). The `CWD:` line names the
directory that line is relative to; the task-folder path is CWD-relative, never
absolute, so the baton works across machines.

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

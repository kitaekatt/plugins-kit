# Task Items -- Design Proposal

**Status:** Ratified and implemented (awesome-kit 0.9.0, 2026-07-09;
vocabulary ratified by the user: canonical term "item", accepted synonym
"work item"). Extends `task-system-design.md` (the v1 spec, amended --
sections 2.7 / 6 / 7.1 / 9). Where code and this document disagree, the code
is authoritative; the operating contract lives in
`references/handoff-template.md`.

**Date:** 2026-07-09

**Deficiency addressed.** The system models the work-unit (folder, task.yaml,
one current pointer) and its lifecycle, but everything below the work-unit --
the enumerable menu of next work -- exists only as loose documentary
convention spread across CLAUDE.md, plan.md, and log.md. There is no verb, no
contract, and no user-visible vocabulary for "enumerate this task's open
items, with their states." Surfaced 2026-07-09 in a real session on
`dev/tasks/homeassistant` (christina-norman): the user asked for the menu of
available sub-work and the agent had to hand-assemble it from three
differently-shaped documents, then field a second question about what these
units are even called.

**Design constraints (user guidance, 2026-07-09):**

- Enumerate as data (embedded YAML) for unambiguous reference.
- Fewer types of data -- priorities reference sub-units of work rather than
  being a separate data structure.
- Validate against real usage: the task folders in `../christina-norman` and
  `../env-config` were mined as evidence and serve as test cases below.

---

## 1. Evidence: how items exist in the wild today

Two background agents mined every task folder in
`~/Dev/christina-norman/dev/tasks/` (homeassistant, network-setup) and
`~/Dev/env-config/dev/tasks/` (bootstrap-env-refactor,
guide-hp-rma-for-defective-rtx-5090). Findings that drive this design:

1. **Scale is small.** A rich long-running task carries 5-10 simultaneously
   open items (homeassistant at 2026-07-09: 1 in-flight, 1 paused, ~4
   priorities, 5 GOAL blocks, 5 checkbox items, 1 `(OPEN)` log item --
   deduping overlaps, 8-10 distinct open items). A flat YAML list handles
   this; per-item folders or lifecycle verbs would be machinery without a
   customer.

2. **Six overlapping representations, no shared state model.** Numbered
   Immediate Priorities (state as bold prose: "RESUME POINT", "not started",
   "Optional"); `GOAL:` H2 blocks (state in a heading parenthetical: "DO NOT
   WORK ON YET", "deliberately deferred"); `[ ]/[x]` checkbox lists (the only
   machine-readable state, used in one section of one task); `#N` external
   issue refs (network-setup); `(OPEN)` tags in log prose (used exactly once,
   for a real item that lives nowhere else); and accreting "carry-forward /
   watch-list" fragments at the tail of log entries (env-config's largest
   bucket of open work, with no closure mechanism -- a reader must diff log
   entries to learn what is still open).

3. **The dominant drift is a stale priority list.** In all three rich tasks,
   completed or abandoned decisions landed in log.md and a dated CLAUDE.md
   top banner but were NOT propagated into the structured priority list:
   homeassistant's Immediate Priorities #1 still presents an integration the
   same folder's log declares a dead end; the RMA task's top priority ("file
   the claim") is three milestones stale against its own plan.md. The cause
   is structural: the priority list RESTATES item content and state, so it is
   a second copy that must be manually synchronized. Duplicated state drifts;
   this is the disease the folder-is-SoT principle already cures one level up
   (references carry no status).

4. **The reference pattern already occurs naturally.** network-setup's
   priorities are pointers into an external issues.md ("#2 My Office closet,
   #3/#4 Bart office..."); env-config's priorities are pointers into plan.md
   ("plan.md forward overview has the per-machine items"). Where priorities
   were written as references they did not drift; where they restated content
   they did.

5. **The embedded-YAML instinct predates this design.** A non-task plan
   (`christina-norman/projects/career/strategy/plan.md`) already hand-rolled
   a fenced `phases:` YAML block with `n/name/effort/what/items/blocked_by`
   fields -- independent evidence that a structured block inside plan.md is
   the ergonomic shape.

---

## 2. The design in one breath

One new typed unit -- **`task_items:`**, a flat list embedded in `plan.md` --
is the single home for a task's open work. Item state lives there and only
there. CLAUDE.md's Immediate Priorities section becomes references to item
ids (never restated content, never restated state) -- priority is a field on
the item, not a separate structure. One new script verb -- **`items <ref>`**
-- parses the block and prints the menu. `validate` gains checks for the
block. Completion is removal: a done item leaves the block at the rotation
pass and the existing record-keeping (plan.md Accomplished line + log.md
detail) is unchanged. No per-item folders, no per-item verbs, no stored
master list -- the document IS the registry, exactly like `task_list`.

## 3. Vocabulary

The unit is an **item** (long form, when context needs qualifying: **task
item** -- the same way "task folder" qualifies "folder"). This is a
deliberate promotion of the system's existing language, not a new coinage:
the hand-off template's triage rule already says "classify every in-flight
**item**", and "open items" was one of the three convention terms the
friction report found users and agents reaching for.

- "Item" names the honest superset: a goal, a chore, a blocked user decision,
  and a watch item are all comfortably "items". Subset names considered and
  rejected: **goal** (outcome-shaped only; collides with the task-level goal
  in `## Where we want to get to`, and would drag the retired `GOAL:` block's
  recorded-only semantics onto in-flight chores), **deliverable**
  (artifact-shaped only; contradicts completion-is-removal; the right
  altitude for the word is a field inside an item's detail section, where the
  old GOAL blocks used it), **step** (implies sequence; collides with the
  rotation discipline's "completed-step detail"), **todo** (collides with the
  native TodoWrite system the skill explicitly disclaims).
- **Accepted synonym: "work item"** (ratified 2026-07-09). It denotes exactly
  the same unit; the skill's dispatch surface routes it identically (SKILL.md
  keywords for the `items` capability include both, plus "open items"). The
  synonym is routing vocabulary only -- contracts, schemas, and template text
  use "item" / `task_items:`.
- It is deliberately NOT "sub-task": an item has no folder, no task.yaml, no
  lifecycle verbs, cannot be referenced from outside its task, and cannot be
  `current`. In this system there is no sub-task entity -- there are tasks
  and there are items, and the boundary is whether the unit needs identity
  and lifecycle outside its parent's plan. An item that outgrows the block is
  promoted to a task (`init` a folder, link via `task_list` /`depends_on`,
  remove the item).
- The existing convention terms map onto it rather than surviving alongside
  it: "immediate priorities" = the highest-priority open items (a view, not a
  structure); "recorded goals" (the `GOAL:` convention) = items in state
  `available` or `deferred` with a detail section; "open items" = the block's
  contents.

The term stays in the task skill (handoff-template.md + SKILL.md), not in
communication-framework.md -- per the framework's own extension rule, a
concept used by one skill lives in that skill.

## 4. The `task_items` typed unit

A fenced YAML block in `plan.md`, at the top of the Forward overview section.
Same mechanics as `task_list`: a top-level key with a registered schema,
extracted by `skills_kit_lib.document_walker.iter_yaml_blocks`, validated by
`skills_kit_lib.schema_engine`, schema dict living in awesome-kit
(`schemas.py`, CCP). The root key is `task_items` (not bare `items`) because
discovery recognizes typed units by root key, and a naked `items:` root is
too generic to claim without false positives; prose still just says "item".

```yaml
task_items:
  items:
    - id: nano-swipe-controls          # required; unique within the task; kebab-case
      title: "Nano swipe-gesture controls"   # required; one line
      state: in-flight                 # required; see vocabulary below
      priority: P1                     # optional; same pattern as task.priority
      note: "resume: gesture mapping half-wired, see log 2026-07-09"  # optional; one line
```

**Field contract:**

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | Unique within the task. Kebab-case (`^[a-z0-9][a-z0-9-]*$`). The handle CLAUDE.md, prose, and the user reference. |
| `title` | string | yes | One-line human title. Non-empty. |
| `state` | enum | yes | `available` / `in-flight` / `blocked-user` / `deferred`. |
| `priority` | string | no | Same pattern as the task type's priority (`^P[1-3]$` for hand-off). NOT a new scale -- one priority vocabulary system-wide. |
| `note` | string | no | One line of state context (what it is blocked on, why deferred, where to resume). Detail beyond one line belongs in a plan.md section keyed by the id. |

**State vocabulary** -- four states, mapping one-to-one onto the in-flight
triage buckets the hand-off template already prescribes (promotion of
convention to contract, not invention):

| State | Triage bucket (handoff-template.md) | Mined prose it replaces |
|---|---|---|
| `available` | Queued -- ready to start | "not started", "RECORDED ONLY", queued forward steps |
| `in-flight` | (the work under way) | "RESUME POINT", "paused mid-Phase-1", "in progress" |
| `blocked-user` | Blocked on user decision | "NEEDS FROM CHRISTINA", "husband-gated", "AWAITS USER GO-SIGNAL", hands-on physical work |
| `deferred` | (parked deliberately) | "DO NOT WORK ON YET", "deliberately deferred", "buy only WHEN..." |

Deliberate omissions:

- **No `done` state.** Completion is REMOVAL from the block at the rotation
  pass; plan.md's Accomplished section keeps the one-line record and log.md
  the detail, exactly as today. The mined accretion anti-pattern (checkbox
  lists and forward overviews padded with `DONE <date>` items that also
  appear in Accomplished -- three copies of one completion fact) is
  structurally prevented: the block enumerates open work only, a moving
  window like the rest of plan.md.
- **No `blocked-on-item` state / no `after:` field.** The triage discipline
  already rules that work blocked on a prior step is listed as that step's
  continuation, not as a standalone item. A soft ordering note ("after
  nano-swipe-controls") fits in `note:`. Adding dependency edges would be
  lifecycle machinery the granularity does not want (and `depends_on` /
  `blocked_by` at the task level already exist for real dependencies).
- **List order is meaningful**: within equal `priority` (and among items with
  no priority), earlier = sooner. Priority + order replaces any separate
  ranking structure.

## 5. Placement: plan.md, and the amended invariant

The block lives in **plan.md**, not task.yaml. Reasoning:

- **CCP.** Items change exactly when the plan rotates -- same author (the
  agent), same moment (the update passes), same document. Putting them in
  task.yaml would split one change across two files every rotation.
- **No parallel bookkeeping.** plan.md's Forward overview already describes
  the next work. A task.yaml item list would be a second description of the
  same thing -- the CLAUDE.md-priorities drift disease reproduced one file
  over. Instead the block IS the forward overview's index (section 7).
- **Documents are the registry.** The system's governing idea (from
  task-system-design.md provenance) is embedded-YAML typed units in markdown,
  discoverable by script. `task_list` set the precedent; the scanning and
  validation machinery already exists.
- **task.yaml stays script-owned.** All current task.yaml writes go through
  verbs. Items are agent-authored prose-adjacent content; routing their edits
  through CLI flags would be a worse authoring surface than editing the
  document the agent is already rotating.

**Spec amendment** (task-system-design.md section 6): "the folder's task.yaml
is authoritative" becomes "the folder is authoritative: task.yaml for the
task-level record, plan.md's `task_items` unit for the item-level
enumeration." References remain inert; nothing outside the folder ever
carries item state. One `task_items` block per task, in plan.md -- validate
enforces singularity.

## 6. Priorities reference items (the CLAUDE.md contract change)

CLAUDE.md's `## Immediate Priorities` section is redefined from a content
list to a **reference view**:

- When it names work, it names it by item id (backticked: `nano-swipe-controls`),
  optionally with one clause of framing. It never restates an item's title
  text at length and NEVER restates its state -- state has exactly one home.
- Prose remains for genuinely non-item content: open questions for the user
  (the existing `### Open questions for the user` subsection), standing
  warnings, the seam-test facts that belong there today.
- The section may open with the standing line: "Live menu: `task items`
  (plan.md `task_items` is the source of truth)."

This is the "priorities reference sub-units of work" guidance made concrete,
and it kills the flagship drift by construction: a priority that is a pointer
cannot disagree with the item it points at. It is the same cure the system
already applies to task references ("references never carry status").

No new data structure is introduced for the priority view -- ids in prose are
enough, the same way task ids appear in prose today. (A typed `item_refs`
unit was considered and rejected: more data types for no query the `items`
verb does not already answer.)

## 7. plan.md contract changes (handoff-template.md)

- **Forward overview opens with the `task_items` block** -- the index of open
  work. Per-item actionable detail follows as prose sections keyed by id
  (`### nano-swipe-controls -- <title>`), with the existing next-1-3-steps
  depth rule: detailed for the top items, one line (or just the block entry)
  for the rest.
- **The `GOAL:` convention retires.** A recorded goal becomes an item
  (`available` or `deferred`) plus, when the spec content warrants it, a
  detail section keyed by its id. The mined GOAL headings' parenthetical
  states map onto the state vocabulary.
- **Rotation additions** (the update passes):
  - Completed item -> remove from the block; one line in Accomplished; detail
    to log.md. (Unchanged discipline, now with a crisper trigger.)
  - **Promotion rule: the block is the only place open work may live.** Any
    open item surfaced mid-session in log prose or CLAUDE.md banners --
    today's `(OPEN)` tags, watch-lists, carry-forwards -- is either promoted
    into the block (usually `blocked-user` or `deferred`, with a `note:`) or
    deliberately discarded, at the same rotation pass. This is the fix for
    the lose-able-log-item failure (homeassistant's Bravia item; env-config's
    accreting watch-lists that no one can enumerate).
  - The stale-state pass now includes: does every CLAUDE.md id reference
    still resolve to a block item? (Cheap to check by eye; also validated,
    section 9.)

`task init`'s plan.md scaffold gains an empty `task_items: {items: []}` block
so every new task starts under the contract.

## 8. The `items` verb

The 15th verb. Script-driven, no inference -- the enumeration the friction
report asked for:

```
task items <ref> [--state S] [--priority P] [--root PATH]
```

- Resolves the ref (same rules as `show`), reads plan.md, extracts the
  `task_items` unit, prints one parseable line per item, sorted by priority
  then block order:

  ```
  nano-swipe-controls    in-flight     P1  Nano swipe-gesture controls
  camera-voice-casting   blocked-user  -   Voice-driven camera casting
  hue-scene-automation   available     -   Hue scene automation
  ...
  ```

- `--state` / `--priority` filter (mirrors `list`). Absent fields print `-`.
  Exit 0 even when empty; notes (malformed block, etc.) to stderr, matching
  `list` conventions.
- Non-zero with a reason when the ref is unresolvable or the folder is not
  locally readable (archived / orphaned / remote) -- matching `show`.
- **Ref defaults to the current task** -- the friction moment is "what next?"
  mid-session, where naming the task is ceremony. (`show`/`status` may later
  adopt the same default; out of scope here.)

`status` (the inference verb) adds the parsed items to its printed substrate,
so the background summarizer sees the menu without re-parsing -- its summary
can and should lead with it. `list` is unchanged (task-level enumeration).

Dispatch vocabulary: the SKILL.md capability entry's keywords route "items",
"task items", "work items", "open items", "goals", "priorities", and
what-next phrasings ("what can I work on", "what's available on this task")
to this verb.

Answering the original friction in vocabulary and mechanism: "what other
work is available on this task?" -> `task items` -> the menu with states.

## 9. Validation additions

In validate.py, for types whose scaffolding includes plan.md (hand-off):

**Errors** (task is `invalid`):

- `task_items` block present but unparseable YAML, or failing the schema
  (missing id/title/state, wrong types).
- `state` outside the vocabulary; `priority` not matching the type's pattern;
  `id` not matching the kebab pattern.
- Duplicate `id` within the block; more than one `task_items` block in the
  folder's documents.

**Warnings** (gate `work`, per the existing rule):

- plan.md has no `task_items` block. A warning, not an error: the folder is
  structurally sound, but pre-contract. Consistent with the system's
  no-back-compat posture, existing tasks are converted forward on first
  `work` after the update -- and the conversion is exactly the enumeration
  work the agent previously did by inference, done once and persisted.
- A backticked kebab token in CLAUDE.md's Immediate Priorities section that
  matches no item id (the stale-reference check). Heuristic and narrow (that
  section only) to avoid false positives; if noise emerges in practice, this
  drops to a rotation-pass instruction rather than a finding.

Schema dict `TASK_ITEMS_SCHEMA` joins `TASK_SCHEMA` / `TASK_LIST_SCHEMA` in
schemas.py; the state vocabulary and id/priority patterns are post-walker
checks in validate.py, same layering as task.yaml's.

## 10. Test case: homeassistant converted

The real 2026-07-09 state of `dev/tasks/homeassistant` (christina-norman),
expressed under this contract -- every currently-live open item from all four
documents, deduped, in one block:

```yaml
task_items:
  items:
    - id: nano-swipe-controls
      title: "Nano swipe-gesture controls"
      state: in-flight
      priority: P1
      note: "resume point per 2026-07-09 banner"
    - id: google-assistant-cameras
      title: "Manual google_assistant integration for Reolink cameras"
      state: deferred
      note: "2026-07-09 verdict: native path dead end; pivot TBD, see log"
    - id: camera-voice-casting
      title: "Voice-driven camera casting"
      state: blocked-user
      note: "architecture TBD -- discuss before starting"
    - id: hue-scene-automation
      title: "Hue scene automation"
      state: available
    - id: hue-dedup
      title: "Hue de-duplication"
      state: deferred
      note: "deliberately deferred 2026-07-07"
    - id: tidbyt-notifications
      title: "Tidbyt notifications"
      state: deferred
      note: "recorded 2026-07-06; do not start yet"
    - id: bravia-google-home
      title: "Move the Bravia into the right Google Home"
      state: blocked-user
      note: "needs Bart's phone: remove + re-add; was an (OPEN) log item"
    - id: pixel-watch-ha-app
      title: "HA app on the Pixel Watch"
      state: blocked-user
      note: "Christina's hands; optional Bort->Bart rename is cosmetic"
    - id: zbt1-radio-stick
      title: "ZBT-1 radio stick: buy + integrate"
      state: deferred
      note: "only when adding more sensors; NUC 2TB reclaim rides along"
```

What the conversion demonstrates against the mined defects:

- The session's actual friction question ("what's available?") becomes one
  verb call with an unambiguous answer: 1 in-flight, 1 available, 3
  blocked-user, 4 deferred.
- The stale-priority defect cannot recur: Immediate Priorities #1 (the
  dead-end google_assistant work) is now a pointer to
  `google-assistant-cameras`, whose single state field was flipped to
  `deferred` the moment the log recorded the verdict -- one edit, one home.
- The Bravia `(OPEN)` log item and the checkbox/GOAL/priority overlaps
  (Nanoleaf et al. appearing in three documents in three wordings) collapse
  into single entries; the done ones simply are not in the block.
- Every GOAL block's heading state ("DO NOT WORK ON YET", "deliberately
  deferred", "architecture TBD") maps cleanly onto the four-state vocabulary
  with the nuance preserved in `note:`.

The env-config folders exercise the other edge: bootstrap-env-refactor's
machine-gated E7 items become `blocked-user` items with per-machine notes,
and its log-only watch-lists become the promotion rule's first real workout
(promote the still-relevant carry-forwards, discard the resolved ones --
today no one can even enumerate them). The RMA task's five forward steps map
to 1 `in-flight` + 4 `available`/`blocked-user` items, and its flagship stale
priority ("file the claim") disappears into a reference.

## 11. What this deliberately does not add

- **No item lifecycle verbs** (no item-close/item-work): editing plan.md IS
  the write path; the agent is already there every rotation.
- **No stored master list, no cross-task item queries**: items are meaningful
  only within their task; `list` remains the task-level surface.
- **No done/history state in the block**: Accomplished + log.md already are
  the record; the block is a moving window.
- **No new priority scale, no dependency graph, no per-item files.**
- **No `update` CLI flags for items** (revisit only if hand-editing proves
  error-prone in practice; validate catches malformed edits either way).

## 12. Implementation sketch

1. `schemas.py`: add `TASK_ITEMS_SCHEMA`.
2. New `task_system/task_items.py`: block extraction (reuse
   `iter_yaml_blocks` + the discovery.py recognition pattern), parse to
   records, singularity/duplicate-id checks. Consumed by the verb and by
   validate.
3. `validate.py`: the section 9 checks.
4. `task.py`: the `items` verb (ref defaulting to current); `status` substrate
   addition.
5. `init.py` scaffold: empty block in plan.md template.
6. Docs: handoff-template.md (sections 6-7 changes: template text, triage
   mapping, rotation additions, promotion rule, GOAL retirement),
   example-claude-md.md (Immediate Priorities as references),
   task-system-design.md (spec amendments: entity, verb table, validation),
   SKILL.md (items capability entry with the synonym keywords per section 8,
   data-model breath update, and a vocabulary gotcha: canonical "item",
   synonym "work item", never "sub-task").
7. Tests in `tests/awesome-kit/`: schema, extraction (including
   multiple-block and duplicate-id), verb output/filters/exit codes, validate
   findings, init scaffold, homeassistant-shaped fixture from section 10.
8. Publish as an awesome-kit minor bump (0.9.0) after `claudx` smoke.

Conversion of the existing real task folders (homeassistant, network-setup,
bootstrap-env-refactor, RMA) happens lazily in their own repos on first
post-update `work` -- the missing-block warning gates work and prompts the
one-time enumeration, which is section 10's content for homeassistant.

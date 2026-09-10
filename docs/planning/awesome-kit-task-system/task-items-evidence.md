# Task Items Design -- Evidence Archive (maintainer-only)

**What this is.** The private-task-folder evidence behind the task-items
design proposal: the mined findings, the homeassistant conversion worked
example, and the implementation sketch. It names real user task-folder
repositories and home paths, so it lives here rather than in the shipped
`task-items-design.md`, which is copied into every consumer's plugin cache.

**Maintainer-only -- this does not ship.** It is plugins-kit development
history, not guidance for a consumer of the `task` skill.

**Extracted on 2026-09-10** from
`plugins/awesome-kit/skills/task/design/task-items-design.md` (the preamble
evidence sentences, the body of section 1, the body of section 10, and the
body of section 12). The design document itself continues to ship as the
contract; each extracted spot there now points back here instead of
restating the content. Content below is verbatim from the source at the time
of extraction.

---

## Preamble evidence

Surfaced 2026-07-09 in a real session on
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

## Section 1: Evidence: how items exist in the wild (observed 2026-07-09)

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

## Section 10: Test case: homeassistant converted

The real 2026-07-09 state of `dev/tasks/homeassistant` (christina-norman),
expressed under this contract -- every currently-live open item from all
four documents, deduped, in one block:

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
as of 2026-07-09 no one could enumerate them). The RMA task's five forward steps map
to 1 `in-flight` + 4 `available`/`blocked-user` items, and its flagship stale
priority ("file the claim") disappears into a reference.

## Section 12: Implementation sketch

1. `schemas.py`: add `TASK_ITEMS_SCHEMA`.
2. New `task_system/task_items.py`: block extraction (reuse
   `iter_yaml_blocks` + the discovery.py recognition pattern), parse to
   records, singularity/duplicate-id checks. Consumed by the verb and by
   validate.
3. `validate.py`: the section 9 checks.
4. `task.py`: the `items` verb with an explicit ref; `status` substrate
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

## Section 7 worked examples (extracted 2026-09-10)

The section 7 sentence on the lose-able-log-item failure carried two examples
that named private task folders, so the published file cites this document
instead. The examples are: homeassistant's Bravia item, and env-config's
accreting watch-lists that no one can enumerate.

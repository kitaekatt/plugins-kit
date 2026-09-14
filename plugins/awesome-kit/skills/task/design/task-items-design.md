# Task Items -- Design Proposal

**Status:** Ratified and implemented (awesome-kit 0.9.0, 2026-07-09;
vocabulary ratified by the user: canonical term "item", accepted synonym
"work item"). Extends `task-system-design.md` (the v1 spec, amended --
sections 2.6 / 6 / 7.1 / 9). Where code and this document disagree, the code
is authoritative; the operating contract lives in
`references/handoff-template.md`.

**Date:** 2026-07-09

**Deficiency addressed.** The system models the work-unit (folder, task.yaml)
and its lifecycle, but everything below the work-unit --
the enumerable menu of next work -- exists only as loose documentary
convention spread across CLAUDE.md, plan.md, and log.md. There is no verb, no
contract, and no user-visible vocabulary for "enumerate this task's open
items, with their states." Evidence and design constraints for this
deficiency are recorded in
`docs/planning/awesome-kit-task-system/task-items-evidence.md` (maintainer-only, in the plugins-kit repository; not shipped with this
plugin).

---

## 1. Evidence: how items exist in the wild (observed 2026-07-09)

The evidence supporting this section is recorded in
`docs/planning/awesome-kit-task-system/task-items-evidence.md` (maintainer-only, in the plugins-kit repository; not shipped with this
plugin).

---

## 2. The design in one breath

The design makes a task's open-work menu enumerable without parallel
bookkeeping. For operating behavior, invoke `/task`; block authoring is
defined in [handoff-template.md](../references/handoff-template.md),
"The `task_items` block (the open-item menu)."

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
   a task-level lifecycle state. In this system there is no sub-task entity -- there are tasks
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

For block authoring, state meanings, completion, and ordering, see
[handoff-template.md](../references/handoff-template.md), "The `task_items`
block (the open-item menu)" and "In-flight triage." The validating field
contract is documented in the module contract of
[task_items.py](../scripts/task_system/task_items.py) and
[TASK_ITEMS_SCHEMA](../scripts/task_system/schemas.py).

The design chose `task_items` as the root because typed-unit discovery
recognizes root keys; a generic `items` root would invite false positives.
It reused the `task_list` mechanics: fenced-YAML extraction by
`skills_kit_lib.document_walker.iter_yaml_blocks`, validation by
`skills_kit_lib.schema_engine`, and a schema owned by awesome-kit.

The state model promoted existing triage rather than introducing an item
lifecycle. The shipped legacy design context motivating that promotion was:

- "not started", "RECORDED ONLY", queued forward steps
- "RESUME POINT", "paused mid-Phase-1", "in progress"
- "NEEDS FROM CHRISTINA", "husband-gated", "AWAITS USER GO-SIGNAL", hands-on physical work
- "DO NOT WORK ON YET", "deliberately deferred", "buy only WHEN..."

Deliberate omissions and their reasons:

- The design omitted `done`: checkbox lists and forward overviews padded
  with `DONE <date>` items that also appear in Accomplished accumulate
  three copies of one completion fact. The open-work window avoids that
  accretion.
- It omitted `blocked-on-item` and `after:` because prior-step continuations
  already handle this granularity. Item dependency edges would introduce
  lifecycle machinery, while task-level `depends_on` / `blocked_by` already
  serve work needing real dependencies.
- It rejected a separate ranking structure because priority and document
  order already supply the ranking.

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

For the Immediate Priorities authoring contract, see
[handoff-template.md](../references/handoff-template.md), "Section semantics."

Item pointers apply the same drift prevention as inert task references:
a pointer cannot disagree with the item it points at. The design rejected
a typed `item_refs` unit because prose ids supplied the view and the `items`
verb already answered the needed query; another type added no query value.

## 7. plan.md contract changes (handoff-template.md)

For Forward overview detail, pre-contract conversion, completion rotation,
promotion, and stale-reference checks, see
[handoff-template.md](../references/handoff-template.md), "`plan.md` -- the
plan," "Converting a pre-contract folder (one-time)," and "Rotation discipline
(the update passes)."

Retiring `GOAL:` blocks removed a competing state carrier. Promotion at
rotation prevented work recorded only in log prose or CLAUDE.md banners
from disappearing from the next session's menu. The worked examples for
that loss-prevention rationale remain recorded in
`docs/planning/awesome-kit-task-system/task-items-evidence.md`
(maintainer-only, in the plugins-kit repository; not shipped with this
plugin).

The implemented empty-block scaffold is in
[init.py](../scripts/task_system/init.py), `_PLAN_MD_TEMPLATE`.

## 8. The `items` verb

For the `items` invocation, output, filtering, and failure behavior, invoke
`/task items`; its capability contract is in [SKILL.md](../SKILL.md), `items`.
Status summary behavior is owned by the `status` capability there.

The design added mechanical enumeration because determining available work
should not require an agent to infer a menu from three documents. Including
that parsed menu in status supplied the summarizer with the same substrate:
"what other work is available on this task?" reaches one mechanical menu.

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

## 10. Test case: a converted task folder

The full worked conversion for this test case is recorded in
`docs/planning/awesome-kit-task-system/task-items-evidence.md` (maintainer-only, in the plugins-kit repository; not shipped with this
plugin).

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

The implementation sketch is recorded in
`docs/planning/awesome-kit-task-system/task-items-evidence.md` (maintainer-only, in the plugins-kit repository; not shipped with this
plugin).

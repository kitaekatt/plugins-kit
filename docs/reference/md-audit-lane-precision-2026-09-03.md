# md-domain audit lane: precision measurement, 2026-09-03

**This document is a dated RECORD, not standing guidance.** Every claim below
describes the audit lane and the `docs/reference` tree as they stood on
2026-09-03, against skills-kit 0.67.0. Its `path:line` citations are evidence
about that state. Do not update them to match a later tree: repointing a
citation destroys the observation it records. Supersede this document with a
newer measurement rather than editing it.

**Read it when** deciding whether the `audit project-doc jobs` lane earns its
review time, or when re-measuring after a criteria change and needing a
before-picture to compare against.

**What it measured.** A full jobs-lane run over `docs/reference` emitted 11 jobs;
job-kit returned 11 accepted, 0 rejected, 0 failed. That proved the MECHANISM.
This document judges the SUBSTANCE of the 23 findings it produced, which the
acceptance contract does not check -- the contract verifies that a finding cites
a real criterion id and an in-range line, and nothing more.

**What happened next.** Two of the three failure modes below were repaired in
skills-kit 0.68.0: PD-2 gained three exclusions plus a trigger-shape
discriminator, and PD-6 gained a discriminating test. A blind re-audit of the
six affected documents returned PASS on all six previously-wrong findings. The
third failure mode -- criteria applied to a dated record as though it were live
guidance -- is tracked separately.

---

VERDICT: **11 CORRECT / 3 ARGUABLE / 9 WRONG out of 23. `testing.md`'s PASS does not hold: MISS.**

## 1. Findings

| Document | Line | Criterion | Bucket | Classification | Reason |
|---|---:|---|---|---|---|
| [agent-directive-standards.md](agent-directive-standards.md:141) | 141 | `crp_unitary_reading_task` | IMPROVE | ARGUABLE | The register could split, but it also supplies precedents needed when applying the criteria. |
| [agent-directive-standards.md](agent-directive-standards.md:1) | 1 | `hygiene_thresholds` | IMPROVE | CORRECT | Its approximately 3,856 tokens exceed 3,000, although this duplicates the CRP evaluation. |
| [claude-code-plugin-platform.md](claude-code-plugin-platform.md:3) | 3 | `mechanical_convention_hygiene` | FIX | CORRECT | U+2014 triggers the built-in `O` check, although this is cosmetic and the claimed ancestor quote does not exist. |
| [claude-code-plugin-platform.md](claude-code-plugin-platform.md:5) | 5 | `crp_unitary_reading_task` | IMPROVE | CORRECT | Hook-output contracts and cache-registry formats fire on distinct development tasks. |
| [claude-code-plugin-platform.md](claude-code-plugin-platform.md:1) | 1 | `placement_maturation` | IMPROVE | WRONG | The controlling cohesion razor says static lookup text does not become a skill. |
| [first-run-experience.md](first-run-experience.md:208) | 208 | `hygiene_thresholds` | FIX | CORRECT | The current path is absent and the identifiable prefixed target exists; the literal is on line 209. |
| [plugin-opinion-razor.md](plugin-opinion-razor.md:1) | 1 | `hygiene_thresholds` | IMPROVE | CORRECT | Its approximately 5,919 tokens exceed 3,000, although this is only a redundant signal. |
| [plugin-opinion-razor.md](plugin-opinion-razor.md:277) | 277 | `crp_unitary_reading_task` | IMPROVE | ARGUABLE | A separate register is plausible, but the audit procedure needs prior verdicts to prevent re-litigation. |
| [publish-reconcile.md](publish-reconcile.md:6) | 6 | `crp_unitary_reading_task` | IMPROVE | CORRECT | The five stated triggers include genuinely independent pre-commit, reconcile, sync, and preview procedures. |
| [publish-reconcile.md](publish-reconcile.md:136) | 136 | `adp_no_claude_md_back_reference` | IMPROVE | WRONG | The rule and reason are already inline; the parenthetical section citation is orientation. |
| [reusable-libraries.md](reusable-libraries.md:1) | 1 | `placement_maturation` | IMPROVE | WRONG | This is an on-demand cross-plugin task index, not knowledge every reader under `plugins/` needs ambiently. |
| [shared-tree-git-discipline.md](shared-tree-git-discipline.md:34) | 34 | `crp_unitary_reading_task` | IMPROVE | CORRECT | Root `CLAUDE.md` reaches this file from separate staging and branch-policy triggers, though splitting 48 lines has little practical value. |
| [task-folders.md](task-folders.md:39) | 39 | `crp_unitary_reading_task` | IMPROVE | ARGUABLE | The resolved incident can split, but understanding it depends heavily on the preceding link model. |
| [task-folders.md](task-folders.md:54) | 54 | `placement_maturation` | IMPROVE | WRONG | The passage is repo-maintainer bug history, which must not move into a published skill reference. |
| [rename-spec.md](terminology/rename-spec.md:1) | 1 | `adp_discoverability` | IMPROVE | CORRECT | No permitted load-graph surface cites the filename, so the orphan signal is real. |
| [rename-spec.md](terminology/rename-spec.md:795) | 795 | `adp_no_claude_md_back_reference` | IMPROVE | WRONG | The submit gate is applied immediately inline and no reread of `CLAUDE.md` is required. |
| [rename-spec.md](terminology/rename-spec.md:188) | 188 | `crp_unitary_reading_task` | IMPROVE | CORRECT | The document explicitly defines five disjoint, parallel-safe implementation packets. |
| [rename-spec.md](terminology/rename-spec.md:1) | 1 | `hygiene_thresholds` | IMPROVE | CORRECT | Its 829 lines exceed the mechanical 500-line threshold, albeit redundantly. |
| [rename-spec.md](terminology/rename-spec.md:453) | 453 | `hygiene_thresholds` | FIX | CORRECT | The executable Packet D item spans lines 452-453 and names a target that no longer exists. |
| [terminology-audit.md](terminology/terminology-audit.md:127) | 127 | `hygiene_thresholds` | FIX | WRONG | The document already dates all `path:line` evidence to the pre-rename tree. |
| [terminology-audit.md](terminology/terminology-audit.md:338) | 338 | `hygiene_thresholds` | FIX | WRONG | The deleted skill is likewise a dated blast-radius observation, not a current outbound target. |
| [terminology-audit.md](terminology/terminology-audit.md:22) | 22 | `ccp_no_skill_content_duplication` | IMPROVE | WRONG | The framework is recorded as the audit's historical input, not asserted as a competing current SSOT. |
| [terminology-audit.md](terminology/terminology-audit.md:417) | 417 | `placement_maturation` | IMPROVE | WRONG | The retained audit is durable maintainer provenance, not immature skill content awaiting migration. |

**`testing.md` PASS  --  MISS.** At [line 16](testing.md:16), "a file the suite has since dropped" violates the ancestor convention at [CLAUDE.md line 502](../../CLAUDE.md:502) forbidding temporal deixis and requiring absolute dates when dates matter. `tests/bootstrap/test_cache.py` is absent. Its absence alone need not be a broken-link finding because the sentence expressly identifies it as historical; the unanchored "since" is the decisive violation.

## 2. The wrong ones

**`claude-code-plugin-platform.md:1`, maturation.** The finding claims the hook and registry lookup tables should graduate into a capability/reference skill. The document actually calls itself a collection of static platform facts. `cohesion-principles.md`, which expressly wins over the derived standards, says static text that reads the same whenever loaded is never a skill. The proposed skill also contradicts the report's own conclusion that the two halves have different triggers.

**`publish-reconcile.md:136`, CLAUDE.md back-reference.** The finding claims the document depends on rereading the root "Anti-pattern: creating a branch" section. Lines 135-138 already state the complete instruction and its reason: use a master worktree because checkout redirects concurrent sessions. The section name is provenance/orientation, which PD-6 permits; the document has no missing dependency to repair.

**`reusable-libraries.md:1`, maturation.** The finding calls the index location-shaped knowledge needed whenever anything under `plugins/` is built. Lines 3-5 instead give a narrower task trigger: consult it before creating project-local machinery, to choose among shared libraries spanning several plugins. Folding 77 lines into `plugins/CLAUDE.md` would charge every plugin-touching session for an API catalog it usually does not need.

**`task-folders.md:54`, move into an existing skill.** The finding says the `validate.py`/`location_ops.py` bug history belongs in `awesome-kit:task`. Lines 29-32 explicitly separate the generic contract owned by that skill from the repo-specific linked-root consequence owned here; lines 39-60 document the resulting maintainer incident. Moving it into the published plugin would also violate the root convention that maintainer-only material stays in `docs/`, and cohesion principles place decision provenance in `CLAUDE.md`, not skill references.

**`rename-spec.md:795`, CLAUDE.md back-reference.** The finding treats "Per the root CLAUDE.md submit gate" as an instruction to reread upstream content. The document actually follows that phrase with the complete three-item opinion analysis and its conclusions. This is an allowed orientation statement, not a load dependency.

**`terminology-audit.md:127`, broken `authoring-lane.md` paths.** The finding treats the old filenames as live navigation and asks that they be rewritten. Lines 3-6 already say every claim is a `path:line` observation against the 2026-08-09 working tree and name the then-current versions. Line 127 is specifically evidence that the old filename was part of the rename's blast radius. Replacing it with the post-rename filename would falsify the audit record.

**`terminology-audit.md:338`, deleted `claude-explorer` path.** The finding again reads historical evidence as a live link. Line 338 is a row in the dated break list recording that `claude-explorer` was then a cross-plugin consumer. The document's opening already supplies the historical qualification the remediation asks it to add.

**`terminology-audit.md:22`, skill-content duplication.** The finding claims section 1 duplicates the later `dec_20` record and should collapse to a pointer. The owner's framework is actually recorded at lines 8-18 as the input against which the audit reasons; section 1 then explains how the conclusions were derived. `dec_20` is the current durable decision, while this file is evidence for how that decision was reached. Replacing the analysis with a pointer would destroy the document's self-contained provenance; line 22 is not even where the supposedly verbatim definitions begin.

**`terminology-audit.md:417`, maturation.** The finding claims the audit should now be trimmed because its durable conclusions exist in the skill provenance and `CLAUDE.md`. The document distinguishes those current decisions from its maintainer-only divergence table and remediation analysis, and lines 459-464 record that the complete working document was deliberately promoted to tracked `docs/reference/` storage. It is historical provenance with a different change cadence, not nursery content still waiting to graduate.

## 3. Pattern

There is a strong pattern, not line-number drift:

- All four `placement_maturation` findings are wrong. They infer a destination from "stable content" without correctly identifying trigger shape, static-reference status, reader set, or the repo's published-plugin boundary.
- Both `adp_no_claude_md_back_reference` findings are wrong. They equate naming a `CLAUDE.md` section with depending on it, even when the full rule is already inline.
- The terminology-audit errors treat dated evidence as live navigation or live duplicate authority.
- The CRP lane is mixed rather than uniformly bad: three findings are clear, while three turn on whether decision registers or tightly coupled incident history justify separate files.

The citations generally land on the relevant paragraph. The few one-line wrapping offsets are not a systematic problem.

## 4. Does the lane earn its keep?

Not in its current form. Strict precision is **11/23 = 47.8%**; counting the three arguable findings as useful leads gives **14/23 = 60.9%**. Four of the eleven correct findings are low-information output -- three mechanical size signals and one cosmetic punctuation finding -- and the lane missed a binding ancestor convention in the only document it passed.

Reading 23 findings to recover roughly seven potentially useful substantive findings, while rejecting nine incorrect remediations and discovering a false PASS independently, costs more judgment than this lane saves. The placement, back-reference, and historical-document heuristics need material correction before another run would justify review time.

## 5. Premises

- **Report inventory  --  CONFIRMED.** The directory exists; 11 `*.mdfull1.1.json` reports contain 23 findings, with ten FAIL reports containing one to five findings and one zero-finding PASS for `testing.md`.
- **Acceptance is structural, not substantive  --  CONFIRMED.** The checker also validates exact report keys, taxonomy/bucket agreement, subject/verdict consistency, and nonempty remediation, but it never tests truth, applicability, context, or improvement.
- **The findings are broadly correct  --  REFUTED.** Fewer than half are unambiguously correct, and the only PASS is false.
- **Every `criterion` string exists  --  CONFIRMED.** All seven distinct criterion IDs used by the reports occur in the governing criteria table.

## 6. Infrastructure disclosure

I created, moved, retired, or changed nothing. I performed only read-only file inspection, searches, existence checks, and Git history/status queries. The initially clean worktree acquired an untracked `scripts/review-bakeoff/` directory concurrently during the review; I did not create, read, or modify it.
# The audit lane

The ONE audit procedure, parameterized by artifact. It replaces the four
near-identical per-member procedures the folded audit skills each carried.

Load this together with exactly one standards doc, selected by the dispatch
table:

| Lane id | Standards doc | Discover / scan script | Detect / classify lane | Remediate lane |
|---|---|---|---|---|
| `audit_skill` | `../standards/skill-standards.md` | `scripts/discover_skill.py` | `workflow/skill-detect.js` | `workflow/skill-remediate.js` |
| `audit_claude_md` | `../standards/claude-md-standards.md` | `scripts/discover_claude_md.py` | `workflow/claude-md-detect.js` | `workflow/claude-md-remediate.js` |
| `audit_project_doc` | `../standards/project-doc-standards.md` | `scripts/discover_project_doc.py` | `workflow/project-doc-detect.js` | `workflow/project-doc-remediate.js` |
| `audit_references` | `../standards/references-standards.md` | `scripts/references_audit.py` | `workflow/references-classify.js` | `workflow/references-remediate.js` |

Script paths are relative to the skill root (`skills/md-domain/`). At runtime
prefix them with `${CLAUDE_PLUGIN_ROOT}/skills/md-domain/`.

`audit_references` is the structural outlier -- a whole-corpus scanner rather
than a per-file auditor. Its differences are collected in "The references-lane
special case" at the end; everything before that section applies to the three
per-file lanes.

## Parameters the lane reads from its artifact binding

Nothing in this procedure is artifact-specific except these, all supplied by the
lane record in the dispatch table:

- **standards doc** -- the criteria applied in the DETECT phase.
- **taxonomy** -- the finding-shape catalog for the artifact (rule ids and
  taxonomy ids are preserved verbatim from the folded members: `C-*`, `R-*`,
  `A-*`, `H-*`, `PD-*`, `CD-*`, `DD-*`, and the per-lane `A..N` / `..O` / `..S`
  letter taxonomies including the inconsistent ancestor-convention ids `M_` /
  `R_` / `S_`).
- **discover script** -- target enumeration plus the artifact's mechanical signals.
- **detect lane / remediate lane** -- the two workflow scripts.
- **verdict set** -- `COMPLIANT` / `NON-COMPLIANT` / `DIFF-CLEAN` /
  `NOT-AUDITED` for the three per-file lanes.
- **flags supported** -- `--review` (all three per-file lanes), `--density`
  (`audit_claude_md` only).

## Model pinning (not negotiable)

Every fan-out lane pins BOTH model and effort explicitly; nothing is inherited
from the session.

- **detect / classify lanes: model `opus`, effort `high`.** Detection is the
  audit's judgment core (criteria application); a low-effort session must not
  silently under-power it.
- **remediate lanes: model `sonnet`, effort `low`.** Remediation applies
  already-decided edits -- the judgment happened at the Q&A gate.

## The pipeline

```
resolve (main loop)
  -> DETECT  (before-Q&A)  : 1 file inline | 2+ files via <artifact>-detect.js  -> structured findings
                             (review mode: ALWAYS via the workflow lane, any file count)
  -> render report (main loop)
  -> Q&A GATE (main loop)  : interactive decisions | inferred when non-interactive
  -> REMEDIATE (after-Q&A) : 1 file inline | 2+ files via <artifact>-remediate.js -> edits applied
  -> final summary + "re-run to verify"
```

**Invoking this lane authorizes the Workflow-tool calls described below.** The
lane's instructions are the opt-in; do not re-prompt the user for permission to
orchestrate.

### Step 1 -- Resolve

Resolve the target set from the arguments. Empty -> the cwd artifact if present,
else stop and surface the cwd; do not improvise a target. `list` -> emit a
numbered list via the lane's discover script and stop. Integers -> map to paths
from the last list output. Path -> use directly.

Strip the flags first:

- non-interactive token (`fast`, `--fast`, `--yes`, `-y`) -> set
  `non_interactive`; also set it when the user's prose expresses the intent
  ("just apply everything, don't ask").
- review token (`review`, `--review`) -> set `review`; also set it on prose
  intent ("review my changes before I submit", "audit the diff"). FALSE by default.
- density token (`density`, `--density`) -> `audit_claude_md` only.

**Reject `review` + `non_interactive`.** "Propose instead of applying, but do not
ask" resolves to doing nothing. Tell the user the two are mutually exclusive and stop.

For each target, also capture `ancestorClaudeMdPaths` (the ancestor-convention
check input): starting from the target's PARENT directory, walk up one directory
at a time until and including the workspace root (the cwd when there is no
enclosing project, otherwise the nearest ancestor containing a `.git` entry),
stat `<dir>/CLAUDE.md` at each level, and collect every one that exists,
ordered NEAREST-ANCESTOR FIRST, EXCLUDING the target itself. Empty when nothing
sits above it.

ONCE per run (not per file), resolve the configurable standards via the plugin venv:

```
(cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> scripts/resolve_standards.py \
   --project-root <workspace root> --primitive <artifact primitive>)
```

Parse its JSON `{ disabled, thresholds, standards }`. Keep run-level
`disabledCriteria` = `disabled`, and per target `standardsPaths` =
`standards.<primitive>`. Both thread into DETECT. An empty or absent config
yields empty lists, so default behavior is unchanged.

### Step 2 -- DETECT (before-Q&A)

Choose execution mode by file count; the threshold equalizes the Workflow tool's
per-run overhead.

- **ONE file (non-review)** -- audit inline in the main loop.
- **TWO OR MORE files, or ANY count in review mode** -- call the Workflow tool
  with `scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/md-domain/workflow/<artifact>-detect.js`
  and `args = { files:[{path, ...artifact signals, ancestorClaudeMdPaths, standardsPaths, preImagePath}], disabledCriteria, review, refs }`.
  One lane per file; returns `{ perFile, totals, review }`.

  The `refs` object carries `pluginRoot` and `venvPython` (every lane) plus
  the lane's document paths, all absolute. Per lane: `audit_skill` needs no
  doc refs (its prompt cites skill-standards.md by repo path);
  `audit_claude_md` takes `criteria`, `codeDirFilter`, and `densityCriteria`
  -- all three the claude-md-standards.md path (the prompt cites its
  sections 3 and 4 for the latter two); `audit_project_doc` takes
  `criteria` (project-doc-standards.md); `audit_references` (classify)
  takes `standardsDoc` (references-standards.md) and `taxonomyDoc`
  (`../references-finding-taxonomy.md` -- the A-K detection signals and
  remediation defaults; standardsDoc is the fallback when absent).

**REVIEW MODE OVERRIDE: the threshold is 1.** Always use the Workflow path, even
for a single file. A review-mode verdict gates a submit, so it must not depend on
whatever model the session happens to be running; only the lane pins model +
effort and enforces the schema. Never run a review-mode detect inline.

**Fallback when the Workflow tool is not exposed** (subagent environments do not
have it): run the 1-file inline procedure sequentially per file -- detection for
all files first, then remediation, keeping the two as separate passes with the
Q&A gate between them. **This fallback does NOT apply in review mode.** Inline
detection inherits the session's model, which is exactly the property review
mode's threshold-1 override exists to eliminate. If review mode is requested
where the Workflow tool is unavailable, either stop and tell the user review mode
needs a main-session run, or run inline and label the result explicitly as
advisory-and-unpinned -- never as a passed gate.

Whichever mode: the inline path applies exactly what the lane applies -- the
artifact's standards doc, the mechanical validator where one exists, the
user-authored standards, and the ancestor-convention check. Detection only: **no
file is edited in this phase.**

Mechanical validator (skill and claude-md lanes):

```
(cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> -m skills_kit_lib.audit <path> --json --config)
```

`--config` makes the validator honor the resolved config (drop disabled
mechanical rows, overlay thresholds). If the validator is unavailable, mark the
Schema group JUDGMENT ("validator unavailable") and continue with judgment
criteria only -- never fail a file for that.

User-authored standards (all lanes, when `standardsPaths` is non-empty): read
each standards file, apply ONLY criteria whose statement is quotable VERBATIM,
SKIP any criterion whose `enforcement` is `mechanical` (the validator owns those
under `--config`), and emit each violation with severity from the criterion's
declared severity (`fail` -> FAIL, `info` -> INFO, `judgment` -> JUDGMENT), the
message carrying the verbatim statement + criterion id + source path. Suppress
any finding whose criterion/rule id is in `disabledCriteria` (never an
architectural or integrity id).

Ancestor conventions (all lanes, when `ancestorClaudeMdPaths` is non-empty): read
each ancestor CLAUDE.md and flag a violation ONLY when the declared rule is
quotable VERBATIM from the ancestor -- no inferred or generic conventions. Emit
under the Hygiene group with the verbatim quote + ancestor source path.
**Exception awareness** (applies to the ancestor check AND to the built-in
non-ASCII / hardcoded-absolute-path convention FIX): when an ancestor EXPLICITLY
declares a scoped exception covering the specific instance -- right file scope
AND right content kind -- do NOT flag that instance under either check; demote it
to PASS/INFO and cite the verbatim exception quote + source path. The one
declared rule plus its exception governs both checks so they never contradict.
No inferred or stretched exceptions; when in doubt the check STILL fires.

Classify each finding into taxonomy + bucket.

### Step 2a -- The decline contract (MANDATORY, every detect lane)

**Every detect lane self-applies its artifact shape test when `kind` is absent,
and declines a non-matching file with `NOT-AUDITED` plus an IMPROVE routing
finding.** This generalizes the PD-1 contract to all four lanes.

The PD-1 three-branch logic is the pattern all lanes follow:

1. **The caller supplied an explicit `kind` that is NOT this lane's artifact**
   -> DECLINE. Verdict `NOT-AUDITED`. Emit the routing finding. No criteria run.
2. **The caller supplied `kind` equal to this lane's artifact** -> APPLY the
   criteria normally. The caller's classification is authoritative.
3. **`kind` is ABSENT** -> the lane SELF-APPLIES its artifact shape test against
   the file, and declines on a non-match exactly as in branch 1.

Branch 3 is the load-bearing one: a kind-less call (a review-mode invocation from
a code-review caller, a bare path from a user) must not be treated as an implicit
"yes, this is my artifact". Absence of a classification is not a classification.

Per-lane shape tests:

- `audit_skill` -- the file is named `SKILL.md`. A `references/*.md` under a
  skill, a `CLAUDE.md`, or a standalone doc is not a SKILL.md.
- `audit_claude_md` -- the file is named `CLAUDE.md` or `CLAUDE.local.md`.
- `audit_project_doc` -- the file is a project-level document: NOT inside a
  `*/skills/*/references/` folder (that is a `skill_reference`) and NOT a
  `CLAUDE.md` / `SKILL.md` (those are `other_claude_artifact`).
- `audit_references` -- no decline branch: the scanner's subject is the corpus,
  not a nominated file.

The routing finding:

- Bucket **IMPROVE**, severity INFO. It is a routing conclusion, not a finding
  against the file, and there is **no edit**.
- It names the correct lane (`/md-domain audit skill`, `/md-domain audit claude-md`,
  `/md-domain audit project-doc`).
- It is **never suppressed**, in either mode. In review mode it skips the
  attributability filter entirely -- it is the record that nothing was read.
- Taxonomy id: the project-doc lane keeps its documented
  `A_misclassified_skill_ref`; the skill and claude-md lanes emit
  `taxonomy: "none"` (criteria ids `artifact_shape_not_skill_md` /
  `artifact_shape_not_claude_md`) -- deliberately unlettered, so the folded
  taxonomy tables stay byte-identical to their pre-fold sources. It is the
  one INFO finding whose bucket is not NONE.

The verdict rule:

- **`NOT-AUDITED` is checked FIRST and overrides everything else**, in BOTH
  modes. A verdict is a claim about criteria that were applied, and none were.
- `NOT-AUDITED` is **not a passing verdict** and is **never folded into the
  COMPLIANT / DIFF-CLEAN counts**. A caller gating on those must be able to tell
  "clean" from "nobody read it".
- The review reducer passes `NOT-AUDITED` through relabel UNTOUCHED and counts it
  APART from `diffClean`.

### Step 3 -- Render the report

Render the per-file report (output template below), then the REPORT CONTRACT
summary in three visible sections IN THIS ORDER, no hedging:

1. **SERIOUS** -- "Found `<N>` serious issue(s) that require fixing" plus a
   one-line summary each. Never auto-fixed, never buried.
2. **FIX** -- normally the count auto-applied and landing in the reviewable
   remediation CL. **In REVIEW MODE nothing is auto-applied**, so render it as
   the count PROPOSED and awaiting the step-4 decision, never as applied.
3. **IMPROVE** -- "Audit found `<N>` improvement opportunit(ies). Do you want to
   discuss them?" plus one one-line pitch each.

SILENT findings do NOT appear. Omit a section whose count is zero.

Output template (group headings come from the artifact's standards doc; the
verdict block and report block are identical across lanes):

```
## <subject name> -- <file path>

Lines: <N> / Tokens: <N> / Findings: <count by bucket>

### <criteria group, e.g. Schema | CCP | CRP | ADP | Hygiene>
[PASS|FAIL|INFO|JUDGMENT] <criterion>: <message>

### Compliance verdict

<P> PASS / <F> FAIL / <I> INFO / <J> JUDGMENT-REQUIRED
Verdict: COMPLIANT | NON-COMPLIANT | DIFF-CLEAN | NOT-AUDITED

## Report (SERIOUS -> FIX -> IMPROVE; SILENT omitted, no hedging)

### SERIOUS -- Found <N> serious issue(s) that require fixing
- <one-line summary per issue>   (never auto-fixed)

### FIX -- <N> applied (in the reviewable remediation CL)   [review mode: "<N> proposed" -- nothing is applied]
- <criterion>: <what was corrected>

### IMPROVE -- Audit found <N> improvement opportunit(ies). Do you want to discuss them?
- <criterion>: <one-line pitch>
```

### Step 4 -- Q&A GATE

- **If `review` is TRUE:** NOTHING is auto-applied. FIX is demoted from
  auto-apply to PROPOSED and goes to the user alongside IMPROVE/SPECIAL. Present
  proposals with AskUserQuestion offering accept-all / reject-all / custom
  instruction (use multiSelect when accept-some is the natural shape). Use
  judgement on grouping: batch fixes across files into ONE question when they are
  small and the files related; split when the fixes are large or the files
  unrelated. SERIOUS is surfaced at the top as always and still never auto-fixed.
  Review-mode declines write NOTHING to the `md-audit-declined:` ledger -- that
  ledger is IMPROVE-scoped and per-file-permanent, whereas a review decline
  usually means "not in this change", and once the change lands the finding is in
  the next pre-image and stops being attributable anyway. Offer an explicit
  "never flag this again for this file" only if the user asks, and only then
  write the ledger.
- **If `review` is FALSE and `non_interactive` is FALSE (the default):** SERIOUS
  findings are surfaced summarized at the top, never auto-fixed. For each IMPROVE
  and SPECIAL finding the user opted to discuss, ask for a decision (apply
  as-proposed / skip / a refined instruction). Surface a tight grouped set; do
  not dump a giant list. A declined IMPROVE is recorded in the target's
  `md-audit-declined:` frontmatter so a re-audit does not re-pitch it.
- **If `non_interactive` is TRUE:** apply FIX findings, surface SERIOUS, and
  infer each IMPROVE/SPECIAL decision from the taxonomy's `default_remediation`
  plus the file content -- record every inferred decision in the final summary so
  the user can see and reverse it. FIX findings need no decision (they apply by
  definition); SILENT findings are never surfaced. FAIL findings are still gated
  by the verdict; non-interactive changes only how the *decisions* are obtained,
  never the audit contract.

For the skill lane's wrong-skill-type category, this is where `classify.py` runs
to confirm the suggestion before any type change.

### Step 5 -- REMEDIATE (after-Q&A)

Assemble per-file remediation lists from the decided findings (FIX = apply;
IMPROVE/SPECIAL = per decision; SERIOUS never auto-applied; drop skips). Choose
mode by how many FILES carry remediation work.

- **ONE file** -- apply inline with Edit.
- **TWO OR MORE files** -- call the Workflow tool with
  `scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/md-domain/workflow/<artifact>-remediate.js`
  and `args = { perFile:[{path, remediations:[{criterion, taxonomy, bucket, line, instruction, decision}]}] }`.
  One lane per file (disjoint files never conflict).

Remediation lanes do not classify -- they apply edits from the decided list.
Carry exact before/after text for a before/after FIX; a lane records "failed"
(not a guess) when the before-text no longer matches.

### Step 6 -- Final summary

Render: FIX applied per file, IMPROVE decisions (including inferred ones),
SERIOUS still-open (never auto-applied), any failures. Remind the user that
re-running the audit should now reproduce a clean (or reduced-FAIL) verdict --
detection and remediation are separate passes, so the re-run IS the verification
step. Scope the verification re-run to the files remediation actually MODIFIED;
results for untouched files stand, and re-auditing them wastes runs.

## Decision rules (verdict)

- **The file was DECLINED** (the shape test failed, so no criteria ran) ->
  `NOT-AUDITED`, in BOTH modes. Checked FIRST; overrides everything below.
- Any FAIL finding -> `NON-COMPLIANT`.
- Only PASS / INFO / JUDGMENT findings -> `COMPLIANT`.
- INFO findings are advisory improvements, not compliance failures, and do not
  escalate to FAIL on subsequent runs.
- **Review mode:** any *attributable* FAIL -> `NON-COMPLIANT`; otherwise
  `DIFF-CLEAN`. Non-attributable FAILs do not gate -- they predate the change --
  but a non-attributable SERIOUS is still reported above the verdict.

## Review mode

Normal mode audits a FILE. Review mode audits a CHANGE: same criteria, same
lanes, but findings the change did not cause are suppressed and nothing is
auto-applied. It exists to gate a submit / publish / handback, where a report
full of pre-existing findings would either bloat the change with unrelated
remediations or train the author to skim past the gate.

Three behavioral differences, and nothing else:

1. **Attributability filter.** Each finding is marked `attributable` by the lane;
   the caller drops the ones the change did not cause. **SERIOUS always survives
   regardless** -- a secret or a violated invariant is not the author's doing and
   is still the most important thing on the page.
2. **Nothing is auto-applied.** FIX is demoted to a proposal at the Q&A gate.
   Mutually exclusive with `fast`, which would mean "propose instead of applying,
   but do not ask" -- i.e. nothing. Reject the combination rather than guessing.
3. **Verdict is `DIFF-CLEAN`, not `COMPLIANT`.** A weaker and more honest claim:
   *this change introduced no failure*, not *this file is clean*. A DIFF-CLEAN
   file may still carry a surviving SERIOUS.

Also: the fan-out threshold drops to 1 (always the Workflow path).

Review-reducer invariants (preserved verbatim): `NOT-AUDITED` passes through
relabel untouched and is counted apart from `diffClean`; the fan-out threshold is
1 under review; the attributable/SERIOUS keep-rule holds.

**Not available on `audit_references`.** If `--review` reaches that lane, say so
and stop -- see the references special case below.

### You materialize the pre-images; the workflow never does

skills-kit is VCS-agnostic and must stay that way -- do not teach a detect lane
about Perforce or git. Before calling the workflow, write each target's
pre-change content to a temp path and pass it as `preImagePath`:

- Perforce: `p4 print -q -o <tmp> //depot/path/FILE#have`
- git: `git show <base>:<path> > <tmp>`, base = `merge-base(HEAD, origin/main)`,
  with the diff spanning `base..worktree` so committed-but-unpushed work is
  INSIDE the change under review rather than part of its baseline.

Infer which from the local repo. Two cases that are easy to get wrong:

- **Adds have no pre-image.** Pass `preImagePath: null` and every finding is
  attributable, which is correct -- the whole file is new. `p4 diff` emits
  nothing for an add, so detect adds via `p4 opened` rather than concluding the
  diff is unavailable.
- **Moves need the source.** `p4 print //new/path#have` fails for a `move/add`;
  resolve the pre-image through the move source.

For a CHILD CLAUDE.md, also pass `parentPreImagePath` so cross-file duplication
the change introduced *in the parent* is not misattributed to the untouched child.

**If you cannot obtain a pre-image, do not silently fall back to a whole-file
audit.** Say the pre-image was unavailable and label the output as unfiltered, so
nobody mistakes a normal audit for a change-scoped gate.

Two limits worth stating rather than hiding:

- **Findings outside the audited file can escape.** Only the nominated file gets
  a pre-image; its members (`references/*.md`, scripts, a child CLAUDE.md) are
  read at their CURRENT state in both passes, so a change confined to one of them
  fires identically against the pre-image and reads as pre-existing.
- **Attributability is judgment, not arithmetic.** It rests on re-detection, so a
  pre-existing finding the pre-image check happens to miss can resurface as
  attributable. Generous structural matching mitigates this; nothing eliminates it.

## The density lens (`audit_claude_md` only)

Runs only when `--density` (or equivalent prose intent) is given; never otherwise.
Applies the DD-1..DD-4 criteria from `../standards/claude-md-standards.md`.
Advisory only -- every finding is JUDGMENT, disposition IMPROVE, never FAIL, so a
density-only run is always COMPLIANT. If the flag arrives on any other lane, say
it does not apply there and continue without it.

## The references-lane special case

`audit_references` preserves the folded references-audit behavior verbatim; only
its location moved. It differs from the three per-file lanes in five ways:

1. **Whole-corpus scanner, not a per-file auditor.** The subject is the corpus.
   Step 1 parses scope intent into `--scope skills|references|md|all` (comma-
   combinable, default `skills`) plus optional repeatable `--path PATH`, and step
   2 runs the scanner ONCE via the plugin venv:

   ```
   uv run python "${CLAUDE_PLUGIN_ROOT}/skills/md-domain/scripts/references_audit.py" \
     --project-dir .claude/skills --user-dir $HOME/.claude/skills $ARGUMENTS
   ```

   Re-run with `--json` when remediation will follow, so the classify phase can
   consume structured findings. The scan itself is never fanned out.
2. **CLASSIFY, not DETECT.** Findings are grouped by containing file, then
   classified -- inline for one file, otherwise via
   `workflow/references-classify.js` with
   `args = { files:[{file, findings:[{severity,line,ref}]}], refs:{standardsDoc} }`.
   Same 2+ threshold, same opus/high pinning, no edits in the phase.
3. **AUTO / DISCUSS / SPECIAL buckets.** The lane retains the legacy dispositions
   alongside the four-disposition model: AUTO = the FIX categories applied in the
   REMEDIATE phase; DISCUSS = the SERIOUS (a `skill:` hard-dep invocation to a
   genuinely-gone skill with no surviving mechanism -- surfaced at the top, never
   auto-fixed) and IMPROVE categories; SPECIAL = the `K` escape hatch.
4. **No `--review`, and no decline branch.** The lane does not implement review
   mode. `--review` on this lane is refused at the router;
   never pass it through and never let a whole-corpus scan be reported as a
   change-scoped gate. There is likewise no shape test to self-apply -- the
   scanner nominates its own subjects.
5. **No per-file COMPLIANT verdict.** The lane emits a scan summary plus the
   report contract; its gate is a merge gate (no ERRORs, i.e. broken hard
   dependencies, in a changelist submitted for merge; WARNINGs and INFOs are
   permissible but should be minimized).

Its Q&A gate batches every IMPROVE + SPECIAL finding into ONE foreground question
round -- a numbered list with category, options, and a recommendation. Do NOT
per-finding round-trip. Remediation fans out per file via
`workflow/references-remediate.js`, and the scan is re-run afterwards to verify:
newly-surfaced findings are common (a backticked literal can reveal another
broken ref nearby that was previously masked).

## Gotchas

- The mechanical validator is canonical for what it covers. Do not re-implement
  its checks in a lane; consume its JSON and present it under the Schema group.
  The cohesion judgment (CCP / CRP / ADP) is what the lane adds on top.
- Hygiene thresholds (line / token count) are CRP-evaluation signals, not
  verdicts. An over-threshold file is not automatically NON-COMPLIANT; apply the
  CRP test before proposing a split, and offer the split only with a NAMED
  extraction candidate.
- Idempotency: criteria, taxonomy, and bucket assignments are fixed. The same
  input produces the same verdict; do not re-rank or re-order findings
  session-to-session.
- Detection and remediation are ALWAYS separate passes, even in workflow mode.
  The Q&A gate sits between them and a background workflow cannot ask the user
  anything. This split is what makes a re-run reproduce the same findings.
- Do not reclassify findings the taxonomy has already settled. A classify lane's
  job is the category match plus judgment on OPTIONS within a category, not
  second-guessing the taxonomy.

## Anti-patterns

- **Audit and remediate in the same pass.** It seems efficient -- one call, fewer
  round trips. It is wrong: conflating the phases invalidates idempotency, lets
  the agent silently mutate the subject before the user has decided the
  IMPROVE/SPECIAL cases, and prevents a re-run from reproducing the findings.
  Instead: run detection to completion, render the verdict, gate, then remediate,
  then re-run to verify.
- **Treat a hygiene threshold as a FAIL verdict.** The threshold is a
  CRP-evaluation signal. Splitting a file whose sections all serve the same
  reading task is a tool-call doubling, not a context-efficiency win.
- **Per-finding user round-trips.** Batch every IMPROVE + SPECIAL finding into
  one foreground round. The cost of being wrong is one revert; the cost of
  friction is the user abandoning the audit.
- **Reporting a declined file as a pass.** A `NOT-AUDITED` file folded into the
  clean count is a fake gate -- the same failure as passing `--review` through to
  the references lane.

## Cross-references

- Where a fact lives (the placement spine) -- `../cohesion-principles.md`.
- Audit vocabulary and the four-disposition bucket model --
  `../audit-framework.md` and `../audit-framework.yaml`.
- Configuring which rules run and their thresholds -- `../configuring-standards.md`.
- Authoring an additive standards file -- `../authoring-standards.md`.
- Producing a compliant artifact instead of judging one -- `authoring-lane.md`.
